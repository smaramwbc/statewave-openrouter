"""OpenAI-compatible proxy that puts Statewave memory in front of OpenRouter.

Point any OpenAI/OpenRouter client at this proxy and pass a subject
(``X-Statewave-Subject`` header, or ``statewave_subject`` in the body):

  * before forwarding, a Statewave context bundle for that subject is
    prepended to ``messages`` as a system message;
  * after the reply, the turn is written back as an episode.

No subject on the request means plain pass-through, memory untouched.
Everything else under ``/`` is proxied verbatim, so this is drop-in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

log = logging.getLogger("statewave_openrouter")

OPENROUTER_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
STATEWAVE_URL = os.getenv("STATEWAVE_URL", "http://localhost:8000").rstrip("/")
STATEWAVE_KEY = os.getenv("STATEWAVE_API_KEY", "")
# HS256 secret for verifying inbound `X-Statewave-Token` JWTs. Set it and every
# chat call needs a valid token (closes the open-port credit drain), and the
# subject comes from the token `sub` - the client can no longer assert it.
JWT_SECRET = os.getenv("PROXY_JWT_SECRET", "")
# Opt back into trusting a caller-supplied subject: single-tenant deploys with
# no token, or a gateway that authenticates itself but manages many subjects.
TRUST_CLIENT_SUBJECT = os.getenv("STATEWAVE_TRUST_CLIENT_SUBJECT", "").lower() in ("1", "true", "yes")
CONTEXT_TOKENS = int(os.getenv("STATEWAVE_CONTEXT_TOKENS", "1500"))
COMPILE_AFTER_TURN = os.getenv("STATEWAVE_COMPILE_AFTER_TURN", "").lower() in ("1", "true", "yes")
EPISODE_SOURCE = os.getenv("STATEWAVE_EPISODE_SOURCE", "openrouter-proxy")
# Statewave policy rules match on the caller; an absent caller_type is the
# least-privileged one, so always send both.
# `or` not a getenv default: an empty value in a .env file must not become an
# empty caller_id, which reads as "no caller identity" server-side.
CALLER_TYPE = os.getenv("STATEWAVE_CALLER_TYPE") or "openrouter-gateway"
CALLER_ID = os.getenv("STATEWAVE_CALLER_ID") or CALLER_TYPE
# A pinned tenant. When set it overrides a client X-Tenant-ID header: on a
# Statewave tenant that enforces require_caller_identity or a policy_mode,
# letting any caller name the tenant is a cross-tenant read and write. Unset,
# one deployment is one tenant and the header is the only source.
STATEWAVE_TENANT = os.getenv("STATEWAVE_TENANT_ID", "")
TIMEOUT = httpx.Timeout(float(os.getenv("PROXY_TIMEOUT", "120")), connect=10.0)

# Statewave subject/session charset: letters, digits, _ . - : - no "/" or
# whitespace. Both ids share one constraint server-side.
ID_RE = re.compile(r"^[A-Za-z0-9_.\-:]{1,256}$")
# Server-side cap on the retrieval query; over it is a 422, not a truncation.
TASK_MAX = 4000

client = httpx.AsyncClient(timeout=TIMEOUT)
_background: set[asyncio.Task] = set()

# Headers we must not copy from the upstream response: httpx has already
# decoded the body, and Starlette recomputes length/framing itself.
_DROP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}


def _relay_headers(upstream: httpx.Response) -> dict[str, str]:
    """Upstream headers worth keeping.

    Without this every path dropped all but content-type, so every
    ``x-ratelimit-*`` value and the OpenRouter request id vanished at the proxy.
    """
    return {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS}


def _relay(upstream: httpx.Response) -> Response:
    """Rebuild an upstream response, keeping its status and headers."""
    return Response(
        upstream.content, status_code=upstream.status_code, headers=_relay_headers(upstream)
    )


def _error(status: int, message: str, error_type: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": error_type}})


async def _warn_if_statewave_outdated() -> None:
    """README pins Statewave >= 1.0.0 (caller-identity + tenant-config surface).
    Best-effort nudge on boot; a missing endpoint or field is silently fine."""
    try:
        # /healthz answers {"status": "ok"} and nothing else, so reading a
        # version from it never found one. /v1/version carries it and is public.
        response = await client.get(f"{STATEWAVE_URL}/v1/version", timeout=5.0)
        version = (response.json() or {}).get("version", "")
    except Exception:  # noqa: BLE001 - never let a probe stop the proxy booting
        return
    digits = re.findall(r"\d+", version)[:3]
    if digits and tuple(int(d) for d in digits) < (1, 0, 0):
        log.warning(
            "statewave server reports version %s; this proxy expects >= 1.0.0", version
        )


@asynccontextmanager
async def _lifespan(_: FastAPI):
    await _warn_if_statewave_outdated()
    yield
    # Drain before closing: in-flight episode writes are the only place a
    # turn exists before Statewave has it.
    if _background:
        await asyncio.gather(*_background, return_exceptions=True)
    await client.aclose()


app = FastAPI(title="statewave-openrouter", lifespan=_lifespan)


@app.middleware("http")
async def _require_token(request: Request, call_next):
    """One choke point, so no route can forget the token - pass-through included.

    Gating inside the three completion handlers left `passthrough` asking for
    nothing: dropping `/v1` from the path routed the same request there, and it
    reached OpenRouter on `OPENROUTER_API_KEY`. Off entirely when no secret is
    configured; `_resolve_subject` still derives the subject from the token it
    verifies again (S3).
    """
    if not JWT_SECRET or request.url.path == "/health":
        return await call_next(request)
    token = request.headers.get("x-statewave-token", "").strip()
    if token[:7].lower() == "bearer ":
        token = token[7:].strip()
    if not token:
        return _error(
            401, "statewave requires a token in X-Statewave-Token", "statewave_auth_required"
        )
    try:
        jwt.decode(token, JWT_SECRET, algorithms=["HS256"], options={"require": ["exp"]})
    except jwt.InvalidTokenError as exc:
        return _error(401, f"invalid statewave token: {exc}", "statewave_bad_token")
    return await call_next(request)


def _spawn(coro) -> None:
    """Run a best-effort side task without blocking the response."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _statewave_headers(request: Request, claims: dict | None = None) -> dict[str, str]:
    headers = {}
    if STATEWAVE_KEY:
        headers["X-API-Key"] = STATEWAVE_KEY
    if STATEWAVE_TENANT:
        tenant = STATEWAVE_TENANT
    elif JWT_SECRET:
        # JWT mode: the tenant comes from the verified token, never the client
        # header - otherwise any caller with a valid token still picks the
        # tenant. No claim on the token means no tenant.
        tenant = (claims or {}).get("tenant", "")
        tenant = tenant if isinstance(tenant, str) else ""
    else:
        tenant = request.headers.get("x-tenant-id", "")
    if tenant:
        headers["X-Tenant-ID"] = tenant
    return headers


def _upstream_headers(request: Request) -> dict[str, str]:
    auth = request.headers.get("authorization")
    if not auth and OPENROUTER_KEY:
        auth = f"Bearer {OPENROUTER_KEY}"
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    # OpenRouter attribution headers, from the caller or from config.
    for header, env in (("HTTP-Referer", "OPENROUTER_SITE_URL"), ("X-Title", "OPENROUTER_SITE_NAME")):
        value = request.headers.get(header.lower()) or os.getenv(env, "")
        if value:
            headers[header] = value
    return headers


def _sse_event(line: bytes, pick) -> str:
    """Text fragment carried by one SSE line, "" for anything else.

    `pick(event) -> str` pulls the fragment out of one parsed `data:` object;
    its shape differs per endpoint. Comments, blank lines and `[DONE]` all fail
    to parse as an event, which is the same as carrying no text.
    """
    if not line.startswith(b"data:"):
        return ""
    try:
        event = json.loads(line[5:])
    except ValueError:
        return ""
    if not isinstance(event, dict):
        return ""  # a scalar/array `data:` line carries no text either
    return pick(event) or ""


def _choice_text(event: dict, container: str | None, field: str) -> str:
    for choice in event.get("choices") or []:
        src = (choice.get(container) or {}) if container else choice
        if src.get(field):
            return src[field]
    return ""


# Per-endpoint shape adapters. Each endpoint differs in where the prompt lives,
# where context can be injected, and how the reply is shaped:
#   chat/completions - `messages` + a prepended system message
#   completions      - a `prompt` string
#   responses        - `input` + top-level `instructions`

def _chat_prompt(body: dict) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # multimodal parts
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _chat_inject(body: dict, context: str) -> None:
    body["messages"] = [{"role": "system", "content": context}, *(body.get("messages") or [])]


def _chat_json_reply(payload: dict) -> str:
    for choice in payload.get("choices") or []:
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
    return ""


def _chat_sse_pick(event: dict) -> str:
    return _choice_text(event, "delta", "content")


def _legacy_prompt(body: dict) -> str:
    prompt = body.get("prompt")
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        return " ".join(p for p in prompt if isinstance(p, str))
    return ""


def _legacy_inject(body: dict, context: str) -> None:
    prompt = body.get("prompt")
    if isinstance(prompt, list):
        # A list prompt is a batch of independent completions. Joining it into
        # one string returns one choice where the caller asked for len(prompt),
        # and glues unrelated prompts together - so prefix each element instead.
        body["prompt"] = [f"{context}\n\n{p}" if isinstance(p, str) else p for p in prompt]
    else:
        body["prompt"] = f"{context}\n\n{_legacy_prompt(body)}"


def _legacy_json_reply(payload: dict) -> str:
    for choice in payload.get("choices") or []:
        if isinstance(choice.get("text"), str):
            return choice["text"]
    return ""


def _legacy_sse_pick(event: dict) -> str:
    return _choice_text(event, None, "text")


def _responses_prompt(body: dict) -> str:
    value = body.get("input")
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in reversed(value):
            if isinstance(item, dict) and item.get("role") == "user":
                content = item.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _responses_inject(body: dict, context: str) -> None:
    existing = body.get("instructions")
    body["instructions"] = f"{context}\n\n{existing}" if existing else context


def _responses_json_reply(payload: dict) -> str:
    parts = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "output_text":
                parts.append(block.get("text") or "")
    return "".join(parts)


def _responses_sse_pick(event: dict) -> str:
    return event.get("delta") if event.get("type") == "response.output_text.delta" else ""


async def _fetch_context(subject: str, task: str, session_id: str | None, headers: dict) -> str:
    body = {
        "subject_id": subject,
        "task": (task or "chat")[:TASK_MAX],
        "max_tokens": CONTEXT_TOKENS,
        "caller_id": CALLER_ID,
        "caller_type": CALLER_TYPE,
    }
    if session_id:
        body["session_id"] = session_id
    try:
        response = await client.post(f"{STATEWAVE_URL}/v1/context", json=body, headers=headers)
        response.raise_for_status()
        return response.json().get("assembled_context") or ""
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            # Not a transient failure: either the API key is wrong, or the tenant
            # requires caller identity and is rejecting ours.
            log.error(
                "statewave rejected the gateway for %s (401) - check STATEWAVE_API_KEY "
                "and STATEWAVE_CALLER_ID/STATEWAVE_CALLER_TYPE",
                subject,
            )
        else:
            log.warning("statewave context fetch failed for %s: %s", subject, exc)
        return ""
    except Exception as exc:  # noqa: BLE001 - memory is an enhancement, never a hard dependency
        log.warning("statewave context fetch failed for %s: %s", subject, exc)
        return ""


async def _write_turn(subject, session_id, user_text, reply, model, headers) -> None:
    episode = {
        "subject_id": subject,
        "source": EPISODE_SOURCE,
        "type": "chat_turn",
        # The compiler reads `messages`; any other shape ingests fine and
        # compiles to an empty string.
        "payload": {
            "messages": [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": reply},
            ],
            "model": model,
        },
    }
    if session_id:
        episode["session_id"] = session_id
    try:
        response = await client.post(f"{STATEWAVE_URL}/v1/episodes", json=episode, headers=headers)
        response.raise_for_status()
        if COMPILE_AFTER_TURN:
            await client.post(
                f"{STATEWAVE_URL}/v1/memories/compile",
                json={"subject_id": subject, "async": True},
                headers=headers,
            )
    except Exception as exc:  # noqa: BLE001 - a failed write must not break the chat
        log.warning("statewave episode write failed for %s: %s", subject, exc)


def _resolve_subject(
    request: Request, body: dict
) -> tuple[str | None, str | None, dict | None, JSONResponse | None]:
    """Decide which subject this request is allowed to touch.

    Validate any supplied ids, then apply the trust rule: with `JWT_SECRET` set
    the subject comes from a verified token; without it a client-supplied
    subject is honoured only when `TRUST_CLIENT_SUBJECT` is on. Returns
    ``(subject, session_id, claims, error)`` - `claims` is the verified token
    (for `_statewave_headers` to pull a tenant from), `error` is a response to
    return as-is.
    """
    # Pop first, then prefer the header: `header or body.pop(...)` skips the pop
    # whenever a header is set, leaving our field in the body sent upstream (F4).
    body_subject = body.pop("statewave_subject", None)
    body_session = body.pop("statewave_session", None)
    hdr_subject = request.headers.get("x-statewave-subject") or body_subject
    session_id = request.headers.get("x-statewave-session") or body_session
    for field, value in (("subject", hdr_subject), ("session", session_id)):
        if value and not ID_RE.fullmatch(value):
            return None, None, None, _error(
                400,
                f"statewave {field} must be 1-256 chars of letters, digits, "
                "underscore, dot, dash or colon",
                "statewave_bad_request",
            )

    if JWT_SECRET:
        token = request.headers.get("x-statewave-token", "").strip()
        if token[:7].lower() == "bearer ":
            token = token[7:].strip()
        if not token:
            return None, None, None, _error(
                401, "statewave requires a token in X-Statewave-Token", "statewave_auth_required"
            )
        try:
            claims = jwt.decode(
                token, JWT_SECRET, algorithms=["HS256"], options={"require": ["exp"]}
            )
        except jwt.InvalidTokenError as exc:
            return None, None, None, _error(
                401, f"invalid statewave token: {exc}", "statewave_bad_token"
            )
        subject = hdr_subject if (TRUST_CLIENT_SUBJECT and hdr_subject) else claims.get("sub")
        if subject and not ID_RE.fullmatch(subject):
            return None, None, None, _error(
                400, "statewave token 'sub' is not a valid subject id", "statewave_bad_request"
            )
        return subject, session_id, claims, None

    if hdr_subject and not TRUST_CLIENT_SUBJECT:
        return None, None, None, _error(
            400,
            "statewave subject supplied but not trusted: set PROXY_JWT_SECRET to derive it "
            "from a token, or STATEWAVE_TRUST_CLIENT_SUBJECT=1 to trust the header",
            "statewave_untrusted_subject",
        )
    return hdr_subject, session_id, None, None


async def _memory_proxy(request: Request, path: str, *, get_prompt, inject, json_reply, sse_pick):
    """Shared body for the three memory-aware endpoints. `path` is the upstream
    route; the four callables adapt this endpoint's request/response shape."""
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "request body is not valid JSON", "statewave_bad_request")
    if not isinstance(body, dict):
        # A JSON array or scalar body used to reach `_resolve_subject`'s
        # `body.pop(...)` and blow up as a TypeError/AttributeError -> 500.
        return _error(400, "request body must be a JSON object", "statewave_bad_request")
    subject, session_id, claims, error = _resolve_subject(request, body)
    if error:
        return error
    sw_headers = _statewave_headers(request, claims)
    prompt = get_prompt(body)

    if subject:
        context = await _fetch_context(subject, prompt, session_id, sw_headers)
        if context:
            inject(body, context)

    url = f"{OPENROUTER_URL}/{path}"
    headers = _upstream_headers(request)
    model = body.get("model", "")

    def record(reply: str) -> None:
        # F14: an empty reply is not a turn worth remembering - both paths skip it.
        if subject and reply:
            _spawn(_write_turn(subject, session_id, prompt, reply, model, sw_headers))

    if not body.get("stream"):
        try:
            upstream = await client.post(url, json=body, headers=headers)
        except httpx.RequestError as exc:
            return _error(502, f"openrouter request failed: {exc}", "openrouter_unreachable")
        if subject and upstream.status_code < 400:
            try:
                payload = upstream.json()
            except ValueError:
                payload = None  # non-JSON 200, e.g. an HTML maintenance page
            # Relay whatever came back either way; only a JSON object can be
            # read for a reply, so anything else just skips the episode write.
            if isinstance(payload, dict):
                record(json_reply(payload))
        return _relay(upstream)

    # Entered here rather than inside the generator: the upstream status and
    # headers have to be known before the response object exists, and that is
    # what kept the stream path from relaying them (O3).
    stream = client.stream("POST", url, json=body, headers=headers)
    try:
        upstream = await stream.__aenter__()
    except httpx.RequestError as exc:
        return _error(502, f"openrouter request failed: {exc}", "openrouter_unreachable")

    async def relay():
        # One line at a time as it flows. Buffering the whole SSE body to
        # rebuild the reply made memory O(body) per in-flight stream (R5); only
        # the reply text and one partial line are held now.
        parts: list[str] = []
        pending = b""
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
                if not subject:
                    continue
                lines = (pending + chunk).split(b"\n")
                pending = lines.pop()  # last element is the unterminated tail
                parts += [_sse_event(line, sse_pick) for line in lines]
        finally:
            await stream.__aexit__(None, None, None)
        parts.append(_sse_event(pending, sse_pick))  # a body with no final newline
        record("".join(parts))

    return StreamingResponse(
        relay(),
        status_code=upstream.status_code,
        headers=_relay_headers(upstream),
        media_type="text/event-stream",
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    return await _memory_proxy(
        request, "chat/completions",
        get_prompt=_chat_prompt, inject=_chat_inject,
        json_reply=_chat_json_reply, sse_pick=_chat_sse_pick,
    )


@app.post("/v1/completions")
async def completions(request: Request):
    return await _memory_proxy(
        request, "completions",
        get_prompt=_legacy_prompt, inject=_legacy_inject,
        json_reply=_legacy_json_reply, sse_pick=_legacy_sse_pick,
    )


@app.post("/v1/responses")
async def responses(request: Request):
    return await _memory_proxy(
        request, "responses",
        get_prompt=_responses_prompt, inject=_responses_inject,
        json_reply=_responses_json_reply, sse_pick=_responses_sse_pick,
    )


@app.get("/health")
async def health():
    """Liveness probe. Never touches OpenRouter, so k8s/ALB checks stay free."""
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def passthrough(path: str, request: Request):
    """Everything else (models, credits, generation) goes straight to OpenRouter."""
    try:
        upstream = await client.request(
            request.method,
            f"{OPENROUTER_URL}/{path.removeprefix('v1/')}",
            content=await request.body(),
            headers=_upstream_headers(request),
            params=request.query_params,
        )
    except httpx.RequestError as exc:
        return _error(502, f"openrouter request failed: {exc}", "openrouter_unreachable")
    return _relay(upstream)
