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
- macOS LaunchAgent helper
- Windows foreground, silent, and Startup-folder helper scripts

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

- Python 3.10+
- An OpenAI Chat Completions compatible upstream endpoint
- An upstream API key, passed either through `x-api-key` or configured as a fallback environment variable

## Installation

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

## Run locally

```bash
python server.py
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

Install and start a LaunchAgent from the current checkout:

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

Foreground mode:

```bat
run.bat
```

Detached helper:

```bat
start-cx2cc.bat
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

The Windows scripts use their own location to find the repository and try `py -3` first, then `python`. They do not rely on a hardcoded Python install path.

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

## Security notes

- Do not commit `.env`, logs, API keys, or generated startup files.
- The proxy forwards prompts and tool results to the configured upstream provider.
- By default, cx2cc binds to localhost only.
- Logs intentionally avoid printing full API keys; keep `logs/` private anyway because upstream error bodies may contain sensitive request context.

## License

MIT
