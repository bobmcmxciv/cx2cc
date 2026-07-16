# cx2cc

[中文文档](README.zh-CN.md) | English

cx2cc is a local compatibility proxy that exposes a subset of the Anthropic Messages API and forwards requests to an OpenAI Chat Completions compatible upstream.

It is intended for local use with Claude Code, CC Switch, or similar tools that can point Anthropic-compatible traffic at a custom base URL.

```text
Claude Code / CC Switch
        │ Anthropic-style /v1/messages
        ▼
http://127.0.0.1:8901
        │ OpenAI-style /chat/completions
        ▼
OpenAI-compatible upstream
```

## Features

- `POST /v1/messages` compatibility shim
- `GET /v1/models` compatibility shim
- Streaming Server-Sent Events translation
- Text, image, tool use, and tool result translation for common Claude Code flows
- Optional fallback upstream API keys with finite retry
- Native macOS arm64 / x86_64 packages and a LaunchAgent helper
- Windows x64 EXE package, foreground, silent, and Startup-folder helper scripts

## Compatibility scope

cx2cc is not the official Anthropic API and does not implement every Anthropic feature.

The supported target is the practical subset used by Claude Code / CC Switch against `/v1/messages`:

- top-level `system`
- `messages` with text blocks
- user image blocks converted to OpenAI `image_url`
- `tools` converted to OpenAI function tools
- `tool_choice` for `auto`, `any`, and specific tools
- assistant `tool_use` and user `tool_result`
- `max_tokens`, `temperature`, `top_p`, `stream`, and `stop_sequences`
- streaming events: `message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`

Known limitations:

- Only `/v1/messages`, `/v1/models`, and `/health` are exposed.
- Batches, Files, token counting, prompt caching, server-side Anthropic tools, structured outputs, and native thinking blocks are not fully implemented.
- Streaming token usage depends on upstream chunks; input tokens may be reported as `0` in streaming mode.
- Actual model behavior and tool-call fidelity depend on the upstream OpenAI-compatible provider.

## Requirements

- Python 3.10+ when running from source. Windows and macOS release packages include native executables and do not require Python.
- An OpenAI Chat Completions compatible upstream endpoint
- An upstream API key, passed either through `x-api-key` or configured as a fallback environment variable

## Windows EXE quick start

Download the versioned `cx2cc-vX.Y.Z-windows-x64.zip` from the GitHub Release page, unzip it, then copy `.env.example` to `.env` beside `cx2cc.exe`.

```powershell
copy .env.example .env
notepad .env
```

Then double-click `cx2cc.exe`. With no arguments it opens a small GUI window where you can:

- See whether the proxy is running and whether the upstream is configured
- Start the background service
- Open the config file (`.env`)
- Open the logs folder
- Copy the local Base URL

Command-line modes are still available:

```powershell
.\cx2cc.exe gui      # open the GUI (default when double-clicked)
.\cx2cc.exe serve    # run the server in the foreground
.\cx2cc.exe start    # start the server in the background
.\cx2cc.exe health   # print a health check
```

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8901/health
```

Logs are written to `logs\` beside `cx2cc.exe`. Closing the GUI does not stop a background service started with `start`.

## macOS executable quick start

Download the package for your Mac: `cx2cc-vX.Y.Z-macos-arm64.tar.gz` for Apple silicon or `cx2cc-vX.Y.Z-macos-x86_64.tar.gz` for Intel. Extract it, enter the directory, and create the configuration:

```bash
tar -xzf cx2cc-vX.Y.Z-macos-arm64.tar.gz
cd cx2cc-vX.Y.Z-macos-arm64
cp .env.example .env
```

After editing `.env`, run the server directly:

```bash
./cx2cc serve
curl http://127.0.0.1:8901/health
```

For a persistent background service, use the bundled LaunchAgent helper:

```bash
./cx2cc.sh install
./cx2cc.sh start
./cx2cc.sh status
```

The macOS artifact is a native raw Mach-O executable, not an `.app`. There is no universal2 build, and the binary is currently unsigned and unnotarized. If Gatekeeper blocks the first launch, verify the checksum first, then allow the binary under System Settings → Privacy & Security. Do not bypass verification for files from untrusted sources.

## Verify release files

Each archive has a matching `.sha256` file. On Windows PowerShell:

```powershell
Get-FileHash .\cx2cc-vX.Y.Z-windows-x64.zip -Algorithm SHA256
Get-Content .\cx2cc-vX.Y.Z-windows-x64.zip.sha256
```

On macOS:

```bash
shasum -a 256 -c cx2cc-vX.Y.Z-macos-arm64.tar.gz.sha256
```

## Source installation

```bash
git clone https://github.com/<owner>/cx2cc.git
cd cx2cc
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set at least:

```env
CX2CC_UPSTREAM_BASE_URL=https://your-openai-compatible-host/v1
```

Optionally set fallback key(s):

```env
CX2CC_UPSTREAM_API_KEY=your-upstream-key
# or
CX2CC_UPSTREAM_API_KEYS=key1,key2
```

If your client passes `x-api-key`, the request header key is used before any fallback key.

## Run from source

```bash
python start-cx2cc.py serve
```

Health check:

```bash
curl http://127.0.0.1:8901/health
```

## Configure Claude Code / CC Switch

Point the Anthropic base URL to the local proxy:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8901
```

Then provide your upstream key in the way your client supports. Common patterns:

```bash
export ANTHROPIC_AUTH_TOKEN=your-upstream-key
```

or configure fallback keys in `.env` with `CX2CC_UPSTREAM_API_KEY` / `CX2CC_UPSTREAM_API_KEYS`.

Keep `CX2CC_HOST=127.0.0.1` unless you intentionally want other machines to reach the proxy. If you bind to `0.0.0.0`, add your own authentication, firewall, or reverse-proxy protection.

## macOS background service

Install and start a LaunchAgent from a source checkout or an extracted macOS release directory:

```bash
chmod +x cx2cc.sh cx2cc-wrapper.sh
./cx2cc.sh install
./cx2cc.sh start
```

Manage it:

```bash
./cx2cc.sh status
./cx2cc.sh restart
./cx2cc.sh log
./cx2cc.sh stop
./cx2cc.sh uninstall
```

The script dynamically writes `~/Library/LaunchAgents/com.cx2cc.proxy.plist` for your checkout path. The generated plist is machine-local and should not be committed.

## Windows usage

If `cx2cc.exe` is present, the Windows helper scripts use it automatically. In a source checkout without `cx2cc.exe`, they fall back to Python.

Foreground mode:

```bat
run.bat
```

or directly:

```powershell
.\cx2cc.exe serve
```

Detached helper:

```bat
start-cx2cc.bat
```

or directly:

```powershell
.\cx2cc.exe start
```

Silent PowerShell helper:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\start-cx2cc.ps1
```

Install Startup-folder autostart:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\install-startup.ps1
```

Remove Startup-folder autostart:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\uninstall-startup.ps1
```

The Windows scripts use their own location to find the release/source directory. Release packages use `cx2cc.exe`; source checkouts try `py -3` first, then `python`. They do not rely on a hardcoded Python install path.

## Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `CX2CC_UPSTREAM_BASE_URL` | Yes | none | OpenAI-compatible API base URL, e.g. `https://example.com/v1`. |
| `CX2CC_UPSTREAM_API_KEY` | No | none | Fallback upstream key used when request `x-api-key` is absent. |
| `CX2CC_UPSTREAM_API_KEYS` | No | none | Comma/newline separated fallback keys. Takes precedence over `CX2CC_UPSTREAM_API_KEY`. |
| `CX2CC_HOST` | No | `127.0.0.1` | Local listen host. |
| `CX2CC_PORT` | No | `8901` | Local listen port. |

## Development

```bash
python -m pytest
python -m compileall server.py translator.py start-cx2cc.py
```

Build the Windows EXE on Windows:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\build-exe.ps1
```

By default, the build script creates `dist\cx2cc-dev-windows-x64.zip` and a SHA256 file. Pass `-Version vX.Y.Z` to set a release version.

## Security notes

- Release packages exclude `.env`, logs, virtual environments, build caches, and Git metadata. Do not commit or upload those files or API keys yourself.
- The proxy forwards prompts and tool results to the configured upstream provider.
- By default, cx2cc binds to `127.0.0.1` only.
- `/health` reports only whether an upstream is configured. Logs and client errors do not record or forward upstream URLs, error bodies, or full API keys. Keep `logs/` private anyway.

## License

MIT
