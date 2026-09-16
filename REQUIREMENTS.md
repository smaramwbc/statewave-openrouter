# statewave-openrouter - Requirements

Scope: one file (`statewave_openrouter.py`, ~450 lines) that fronts OpenRouter
with Statewave memory. Anything that would be a second service belongs in
`smaramwbc/statewave`, not here.

Status as of 2026-09-08: M0-M4 done (in git as `smaramwbc/statewave-openrouter`;
operable; auth-gated; all three completion surfaces memory-aware; packaged).
Version 1.0.0, Production/Stable - ships when `v1.0.0` is tagged.

Legend: **Done** = shipped and covered by a test in `test_proxy.py`.
**Gap** = not built. **Won't** = deliberately out of scope.

---

## Functional

| ID | Requirement | Status |
| --- | --- | --- |
| F1 | `POST /v1/chat/completions` with a subject fetches `/v1/context` and prepends the bundle as a system message | Done |
| F2 | After the reply, the turn is written to `/v1/episodes` off the response path | Done |
| F3 | No subject → byte-identical pass-through, zero Statewave calls | Done |
| F4 | Subject/session from `X-Statewave-Subject`/`-Session` header or `statewave_subject`/`_session` body field; header wins; body fields stripped before upstream | Done |
| F5 | Ids not matching `^[A-Za-z0-9_.\-:]{1,256}$` → `400 statewave_bad_request` before any upstream call | Done |
| F6 | Retrieval `task` truncated to 4000 chars (server cap is a 422, not a truncation) | Done |
| F7 | Streaming: SSE relayed byte-for-byte, episode written when the stream closes | Done |
| F8 | Every other path proxied to OpenRouter unchanged | Done |
| F9 | `caller_id` + `caller_type` sent on every retrieval; empty env value must not become an empty caller_id | Done |
| F10 | `X-Tenant-ID` forwarded, or pinned via `STATEWAVE_TENANT_ID` | Done |
| F11 | Optional async compile after each turn (`STATEWAVE_COMPILE_AFTER_TURN`) | Done |
| F12 | `POST /v1/completions` (legacy) is memory-aware | Done - context prefixed to `prompt`, and to every element of a list prompt; shares `_memory_proxy` |
| F13 | `POST /v1/responses` is memory-aware | Done - context merged into `instructions`; typed-event SSE reply parser |
| F14 | Non-stream path writes an episode even when the reply text is empty; stream path skips it. Pick one. | Done - picked skip; both paths guard `if reply` in `_memory_proxy` |
| F15 | Multi-turn history summarisation, local caching, prompt templating | Won't - Statewave's job |

## Reliability

| ID | Requirement | Status |
| --- | --- | --- |
| R1 | Statewave failure (any exception, any status) never fails the completion - logged, turn proceeds without memory | Done |
| R2 | 401 from Statewave logged as an error naming the two likely causes, not as a transient warning | Done |
| R3 | Shutdown drains in-flight episode writes before closing the httpx client - the turn exists nowhere else | Done |
| R4 | Upstream timeout 120s default, 10s connect | Done |
| R5 | Streaming rebuilds the reply as it flows - one partial line plus the reply text held, never a second copy of the body | Done - the stream is opened before the response too, which closed O3's last gap |
| R6 | No retry/backoff against Statewave | Won't - a failed turn is one lost episode, not lost data |

## Security

| ID | Requirement | Status |
| --- | --- | --- |
| S1 | Caller's `Authorization` forwarded verbatim; `OPENROUTER_API_KEY` used only as fallback | Done |
| S2 | **Subject is caller-asserted.** Any client that can reach the proxy can send `X-Statewave-Subject: user:99` and read/write that subject's memory. | Done - `PROXY_JWT_SECRET` derives the subject from a verified `X-Statewave-Token`; header accepted only under `STATEWAVE_TRUST_CLIENT_SUBJECT` |
| S3 | With `OPENROUTER_API_KEY` set and no proxy-level auth, anyone who can reach the port spends the operator's OpenRouter credits | Done - `PROXY_JWT_SECRET` set means an HTTP middleware rejects a tokenless call on every route but `/health`. Gating the three handlers alone was not enough: pass-through is not read-only, and `POST /chat/completions` without the `/v1` reached OpenRouter through it on the operator's key |
| S4 | Statewave API key never reaches OpenRouter and vice versa (separate header builders) | Done |
| S5 | Secrets never logged - log lines carry subject ids only | Done |

S2/S3 fix, shipped M2: `PROXY_JWT_SECRET` set means every chat call carries an
HS256 JWT in `X-Statewave-Token` and the subject is its `sub` claim. Without the
secret, a caller-supplied subject is honoured only under
`STATEWAVE_TRUST_CLIENT_SUBJECT`; otherwise the request is a `400`. Two env vars,
one check in a middleware and one in `_resolve_subject` - not an auth framework.

The middleware is what makes S3 true. Leaving pass-through open was a deliberate
call, but it rested on those routes being cheap reads like models and credits;
they are not, since the catch-all also carries `POST /chat/completions` for any
caller who drops the `/v1` prefix.

## Operations

| ID | Requirement | Status |
| --- | --- | --- |
| O1 | Config entirely via env vars, documented in `.env.example` and README | Done |
| O2 | `GET /health` that does not hit OpenRouter | Done |
| O3 | Upstream response headers relayed (`x-ratelimit-*`, OpenRouter request id) | Done - shared `_relay_headers` builder, on every path including streams |
| O4 | CI: ruff + pytest on 3.11 | Done |
| O5 | CI matrix covers 3.12 and 3.13 - both claimed in `pyproject.toml` classifiers, neither tested | Done |
| O6 | Startup warns when the Statewave server is older than 1.0.0 (README states the floor; nothing enforces it) | Done - `_warn_if_statewave_outdated` reads `/v1/version` on boot; missing endpoint/field is silently fine. It read `/healthz`, which reports status and no version, so the warning could never fire |
| O7 | Dockerfile + published image | Done - `ghcr.io/smaramwbc/statewave-openrouter`, pushed by the `Release` workflow |
| O8 | Published to PyPI, tagged, CHANGELOG | Done - `Release` workflow publishes on a `v*` tag (PyPI trusted publishing, no token secret) |

---

## Timeline

One developer, all dates 2026. Everything below is small: this is a
single-file proxy, not a platform.

| Week | Milestone | Ships |
| --- | --- | --- |
| ~~Wed Aug 26 to Fri Aug 28~~ **done 2026-08-27** | **M0 - exist in git** | Initial commit of the current tree, tag `v0.1.0`, CHANGELOG. O8 minus PyPI. |
| ~~Mon Aug 31 to Fri Sep 4~~ **done 2026-08-27** | **M1 - operable** | O2 `/health`, O3 header relay (shared `_relay` builder), O5 CI matrix. Tests: probe hits no upstream; rate-limit header survives a round trip. Stream path still drops upstream headers - folded into R5's rework. |
| ~~Mon Sep 7 to Fri Sep 11~~ **done 2026-08-27** | **M2 - trustworthy** | S2 + S3: `PROXY_JWT_SECRET` verifies an `X-Statewave-Token` JWT and derives the subject from `sub`; `STATEWAVE_TRUST_CLIENT_SUBJECT` opt-out for single-tenant deploys. Tests: forged subject loses to the token; missing/bad token is 401; subject with no trust mode is 400; trusted gateway still overrides. |
| ~~Mon Sep 14 to Fri Sep 18~~ **done 2026-08-27** | **M3 - surface complete** | F12, F13 (`/v1/completions`, `/v1/responses` memory-aware via a shared `_memory_proxy` + per-shape adapters), F14 empty-reply symmetry, O6 version warning. |
| ~~Mon Sep 21 to Fri Sep 25~~ **done 2026-09-08** | **M4 - v1.0.0** | R5 incremental SSE parse (and with it the stream half of O3), O7 Dockerfile + ghcr image, O8 tag-driven PyPI publish, README against the final surface. Version 1.0.0, Alpha classifier dropped. |

Critical path: M0 → M4, done. v1.0.0 ships when the `v1.0.0` tag is pushed;
the `Release` workflow publishes the wheel and the image.

Not scheduled: metrics/OpenTelemetry, multi-provider upstreams, an admin API.
Each turns this into a service that needs owning. Add when something concrete
demands it.
