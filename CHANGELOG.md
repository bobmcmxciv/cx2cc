# Changelog

## v0.2.0

- Added a simple Windows GUI launcher (default when `cx2cc.exe` is double-clicked).
- Added Windows EXE packaging support via PyInstaller.
- Added `cx2cc.exe gui`, `serve`, `start`, and `health` modes.
- Updated Windows scripts to prefer the packaged executable and fall back to source mode.
- Added Windows release zip build script and checksum generation.

## v0.1.0

- Initial public-ready release.
- Added Anthropic `/v1/messages` to OpenAI Chat Completions translation.
- Added streaming SSE translation for common Claude Code flows.
- Added macOS LaunchAgent install/start/stop helpers.
- Added Windows foreground, silent, and Startup-folder helpers.
- Added explicit environment configuration template.
- Added tests for translator and server behavior.
