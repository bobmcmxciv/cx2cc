# Changelog

## Unreleased

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
