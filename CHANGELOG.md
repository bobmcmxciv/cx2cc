# Changelog

## Unreleased

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
