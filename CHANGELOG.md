# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

## [Unreleased]

### Security
- In JWT mode (`PROXY_JWT_SECRET` set) with no pinned `STATEWAVE_TENANT_ID`,
  the tenant now comes from a `tenant` claim on the verified token instead of
  the client's `X-Tenant-ID` header. A caller with a valid token could
  otherwise still pick any tenant; a token with no `tenant` claim now sends
  no tenant at all, rather than falling back to the header.

### Fixed
- A JSON array or string request body (valid JSON, not an object) no longer
  500s; it is rejected with `400 statewave_bad_request` before
  `_resolve_subject` touches it.
- OpenRouter answering `200` with a non-JSON body (e.g. an HTML maintenance
  page) on the non-stream path no longer 500s; the response is relayed
  unchanged and the episode write is skipped.
- The same guard now also covers a `200` whose body parses as JSON but is not
  an object (e.g. `[1,2]`): that still reached the reply adapters and 500d on
  `payload.get(...)`. Only a JSON object is read for a reply now.
- An SSE `data:` line carrying valid JSON that is not an object no longer
  raises mid-stream. The status was already sent, so this truncated the
  client's stream instead of erroring; such a line now counts as carrying no
  text, like a comment or `[DONE]`.

## [1.0.0] - 2026-09-08

First stable release. The surface is complete; what is documented below is
what follow-up releases have to keep working.

### Added
- `GET /health` liveness probe that never contacts OpenRouter (O2).
- CI now runs on Python 3.11, 3.12 and 3.13 (O5).
- `PROXY_JWT_SECRET`: verify an inbound `X-Statewave-Token` (HS256 JWT) and take
  the subject from its `sub` claim. With the secret set, every chat call needs a
  valid token, which also closes the open-port credit drain (S2, S3).
- `STATEWAVE_TRUST_CLIENT_SUBJECT`: opt back into trusting the
  `X-Statewave-Subject` header (single-tenant, or a self-authing gateway).
- `POST /v1/completions` (legacy) is memory-aware: context is prepended to the
  `prompt`, the turn is written back (F12).
- `POST /v1/responses` is memory-aware: context is merged into `instructions`,
  the turn is written back (F13).
- On startup the proxy pings Statewave `/v1/version` and logs a warning if the
  server is older than 1.0.0 (O6). Best-effort, never blocks boot.
- `Dockerfile` and a `ghcr.io/smaramwbc/statewave-openrouter` image, built
  and pushed by a tag-driven `Release` workflow that also publishes to PyPI
  (O7, O8). The container listens on `$PORT` (8080 by default), runs as `nobody`.

### Security
- A pinned `STATEWAVE_TENANT_ID` overrides a client `X-Tenant-ID` header rather
  than the header winning. Without it, any caller - a valid token holder
  included - could point a request at another tenant's memory.
- JWT verification requires an `exp` claim; a token without one is rejected with
  `401 statewave_bad_token`.

### Changed
- Invalid JSON in a request body returns `400 statewave_bad_request`, and an
  unreachable OpenRouter returns `502 openrouter_unreachable`, instead of an
  unhandled `500`.
- Non-stream and pass-through responses relay all upstream headers
  (`x-ratelimit-*`, OpenRouter request id, and the rest) via a shared `_relay`
  builder, instead of keeping only `content-type` (O3).
- Streaming rebuilds the reply one SSE line at a time as the bytes arrive,
  instead of buffering the whole body first: an in-flight stream now holds the
  reply text and one partial line, not a second copy of the response (R5).
  Opening the stream before the response is built is also what lets the
  streaming path relay upstream status and headers - the last gap in O3.
- The three memory-aware endpoints now share one handler (`_memory_proxy`) with
  per-endpoint shape adapters.
- An empty assistant reply no longer writes an episode on the non-stream path
  either; both paths now agree (F14).
- **Breaking:** a caller-supplied `X-Statewave-Subject` is no longer trusted by
  default. Without `PROXY_JWT_SECRET` or `STATEWAVE_TRUST_CLIENT_SUBJECT` set, a
  request carrying a subject now gets `400 statewave_untrusted_subject`.

## [0.1.0] - 2026-08-27

Initial release. Single-file OpenAI-compatible proxy that fronts OpenRouter with
Statewave memory.

### Added
- `POST /v1/chat/completions`: fetches a Statewave context bundle for the request
  subject, prepends it as a system message, and writes the turn back as an
  episode off the response path. Streaming and non-streaming both supported.
- Subject/session via `X-Statewave-Subject` / `X-Statewave-Session` headers or
  `statewave_subject` / `statewave_session` body fields (header wins, body fields
  stripped before upstream).
- No subject: byte-identical pass-through with zero Statewave calls.
- Everything else under `/` proxied to OpenRouter verbatim.
- Caller identity (`caller_id` + `caller_type`) on every retrieval; tenant
  forwarding via `X-Tenant-ID` or pinned `STATEWAVE_TENANT_ID`.
- Optional async compile after each turn (`STATEWAVE_COMPILE_AFTER_TURN`).
- Statewave failures never fail the completion; shutdown drains in-flight
  episode writes before closing the HTTP client.

[1.0.0]: https://github.com/smaramwbc/statewave-openrouter/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/smaramwbc/statewave-openrouter/releases/tag/v0.1.0
