# Changelog

## Unreleased

- Added usage history to the gateway: `tools/import_cx2cc_logs.py` rebuilds daily usage from cx2cc and codex-bridge logs (dates reconstructed backwards from each file's modification time, models joined through the conversation key), `import-history` loads it into a separate `usage_history` table that re-imports replace, and every report reads live and imported data through the `usage_all` view. The console says which days are imported, and key pages can show 90 days. Console asset URLs now carry a version so a deploy reaches browsers immediately.

- Added cx2cc-gateway (`gateway/`), an optional multi-user front door: personal API keys stored as hashes and shown once, with aliases, owners, scopes (`chat`, `images`, `usage`, `accounts`), per-key model allowlists that follow cx2cc's aliases, requests-per-minute / concurrency / daily / 7-day weighted-token limits, expiry, rotation with a grace period and revocation. The caller's key is swapped for the single internal token cx2cc accepts; streams are forwarded unbuffered while usage is read from the final frames of all three dialects; every call gets an audit row (never the prompt or completion) and an `X-Cx2cc-Request-Id`. Keys without the `accounts` scope see `/usage` without subscription e-mail and account ids. A web console under `/admin/` (admin / operator / auditor roles, key-holder self-service, CSV export, admin event log) manages it. aiohttp service with its own tests and Docker image.
- Added the project page (`site/index.html`).
- Added `POST /v1/responses` (alias `/openai/v1/responses`), a byte-level Responses passthrough for Codex CLI 0.135+, and `POST /v1/alpha/search` for Codex CLI 0.158+ standalone web search.
- Added `POST /v1/chat/completions` (alias `/openai/v1/chat/completions`), an OpenAI Chat Completions passthrough that shares model resolution, key rotation and the per-conversation cache key with `/v1/messages`.
- Added `POST /v1/images/generations` and `POST /v1/images/edits` passthroughs (OpenAI Images shape, up to 16 data-URL reference images) for upstreams serving gpt-image-2.
- Added model resolution: `[1m]`-style window suffixes are stripped, `CX2CC_MODEL_ALIASES` remaps names, `CX2CC_MODEL_PASSTHROUGH` plus the upstream catalog form the allowlist, Claude model names run on the default model, and unknown names are rejected with a 400 (`CX2CC_UNKNOWN_MODEL=reject`) instead of being served silently by another model. `/v1/models` mirrors the upstream catalog annotated with `default_model`, `aliases`, `unknown_model_policy` and per-entry `served_as`.
- Added an anchored prompt-size estimate to `message_start.usage` (`prompt_estimate.py`), learned per conversation from the previous turns' real counts, so Claude Code's context meter no longer reads zero on streaming upstreams.
- Forwarded images returned inside tool results to the model as a follow-up user message, since OpenAI tool messages cannot carry images.
- Served with waitress (64 threads, 512 connections, 900 s channel timeout) when available, instead of the werkzeug development server.

- Mapped Anthropic's hosted web search tool (`web_search_<date>`, which Claude Code's WebSearch sends as a forced side query) onto the upstream's hosted `web_search` tool instead of an empty function, and rebuilt `server_tool_use` / `web_search_tool_result` blocks (plus `usage.server_tool_use`) from the bridge's `web_search_calls` and `url_citation` annotations, streamed and non-streamed. `allowed_domains` becomes the upstream `filters`; `blocked_domains` and `max_uses` have no counterpart and are dropped. Before this, WebSearch through cx2cc returned no links at all. Also fixed the streamed text block that follows a tool call reusing the tool block's index (and a KeyError on that path).

- Appended a response-style addendum (`DEFAULT_STYLE_PROMPT`) to the system prompt of every translated request, asking the upstream model for conclusion-first, consolidated paragraphs instead of the one-short-block-per-tool-call narration that fragments transcripts. Appended rather than prepended so the upstream prefix cache and the per-conversation `prompt_cache_key` are unaffected. Disable with `CX2CC_STYLE=off`; override with `CX2CC_STYLE_PROMPT` (file path or literal text).

- Logged a per-request cache fingerprint (`prompt_cache_key`, tool count, cumulative content hashes snapshotted at fixed message indexes) plus the upstream's `cached_tokens` for both streamed and non-streamed responses. Consecutive turns of one conversation must match at every shared index, so when the upstream cache-hit collapses, the log now shows whether the prompt content diverged (and where) or the upstream dropped an intact prefix — a distinction that cannot be reconstructed afterwards.
- Made the fallback id for `tool_use` blocks that arrive without one a pure function of the block's position instead of a random uuid. A random id serialized differently on every retransmission of the same history, which would break the upstream prompt cache at that offset for the rest of the conversation. (Observed Claude Code traffic always carries ids, so this is defensive.)

- Added `GET /accounts` (alias `/v1/accounts`): forwards an upstream's account-pool view with the same key passthrough as `/usage`, for upstreams that serve from several subscriptions. The caller's query string is forwarded too, so upstream filters like `?usage=0` work through the proxy.
- Added `GET /usage` (alias `/v1/usage`): forwards the upstream's usage/quota JSON with the same key passthrough as `/v1/messages`, so CC Switch can display remaining subscription quota. The upstream URL defaults to the base URL without its `/v1` suffix plus `/usage` and can be overridden with `CX2CC_USAGE_URL`.
- Made server tests hermetic: a real `.env` next to `server.py` no longer leaks into tests and makes them hit a live upstream.

- Added a per-conversation `prompt_cache_key` (uuid5 of the system prompt and first user message) sent with every upstream request, so OpenAI-style automatic prompt caching keeps hitting across the turns of one conversation. Disable with `CX2CC_PROMPT_CACHE_KEY=off`. Observed per-turn hit rates on real agentic sessions went from ~13% to ~99%.
- Reported real input token counts and upstream cached tokens (`cache_read_input_tokens`) for streamed responses; previously streamed usage was reported as zero.
- Added `CX2CC_UPSTREAM_MODEL` to configure the upstream model name and `CX2CC_REPORT_UPSTREAM_MODEL` to report the model that actually served each request.
- Accepted `Authorization: Bearer` in addition to `x-api-key` as the caller's upstream key source.
- Documented the prompt caching mechanism and its OpenAI-style usage semantics (reported `input_tokens` includes cached tokens).

## v0.3.0

- Added native macOS arm64 and x86_64 release packages.
- Unified Windows and macOS builds in a least-privilege release workflow with SHA-pinned actions.
- Added final-archive smoke tests, architecture checks, strict package file allowlists, and SHA-256 checksums.
- Added pull request and main-branch CI with tests, compile checks, and full-history secret scanning.
- Removed upstream URLs and raw upstream error bodies from health responses, logs, and client errors.
- Updated the Windows build script and macOS LaunchAgent helpers for packaged executables.

## v0.2.0

- Added a simple Windows GUI launcher (default when `cx2cc.exe` is double-clicked).
- Added Windows EXE packaging support via PyInstaller.
- Added `cx2cc.exe gui`, `serve`, `start`, and `health` modes.
- Updated Windows scripts to prefer the packaged executable and fall back to source mode.
- Added Windows release zip build script and checksum generation.
- Added GitHub Actions CI for automated Windows EXE builds on tag push.

## v0.1.0

- Initial public-ready release.
- Added Anthropic `/v1/messages` to OpenAI Chat Completions translation.
- Added streaming SSE translation for common Claude Code flows.
- Added macOS LaunchAgent install/start/stop helpers.
- Added Windows foreground, silent, and Startup-folder helpers.
- Added explicit environment configuration template.
- Added tests for translator and server behavior.
