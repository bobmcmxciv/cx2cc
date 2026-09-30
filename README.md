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
- `POST /v1/chat/completions` (alias `POST /openai/v1/chat/completions`) OpenAI Chat Completions passthrough, for clients that already speak OpenAI natively
- `GET /v1/models` compatibility shim
- `GET /usage` passthrough of the upstream's quota/rate-limit JSON, for usage display in CC Switch
- `GET /accounts` passthrough for upstreams that serve from a pool of subscriptions
- `POST /v1/responses` (alias `POST /openai/v1/responses`) OpenAI Responses passthrough, for Codex CLI 0.135+ which only speaks Responses
- `POST /v1/alpha/search` passthrough for Codex CLI 0.158+ standalone web search
- `POST /v1/images/generations` and `POST /v1/images/edits` passthroughs (OpenAI Images shape, reference images as data URLs) for upstreams that expose gpt-image-2, used by the `gpt-image` Claude Code skill
- Claude Code's WebSearch mapped onto the upstream's hosted `web_search` tool, with search results rebuilt as `server_tool_use` / `web_search_tool_result` blocks
- Model resolution: `[1m]`-style window suffixes stripped, an alias table, a passthrough allowlist merged with the upstream catalog, and unknown models rejected instead of silently swapped; `/v1/models` says which model actually serves each name
- Streaming Server-Sent Events translation
- Text, image, tool use, and tool result translation for common Claude Code flows
- Per-conversation `prompt_cache_key` so upstream automatic prompt caching keeps hitting across turns, with cache reads reported back to the client
- An anchored prompt-size estimate in `message_start`, so Claude Code's context meter is not stuck at zero on OpenAI-style upstreams
- Images returned inside tool results forwarded to the model as a follow-up user message
- Optional multi-user gateway ([`gateway/`](gateway/README.md)): personal API keys, scopes, model allowlists, rate and quota limits, a per-call audit log and a web console
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

- Only `/v1/messages`, `/v1/chat/completions` (alias `/openai/v1/chat/completions`), `/v1/responses`, `/v1/alpha/search`, `/v1/images/generations` and `/v1/images/edits` (aliases under `/openai/v1/`), `/v1/models`, `/usage`, `/accounts`, and `/health` are exposed.
- Batches, Files, token counting, server-side Anthropic tools, structured outputs, and native thinking blocks are not fully implemented.
- Prompt caching relies on the upstream's automatic caching (see [Prompt caching](#prompt-caching)); Anthropic `cache_control` markers are ignored.
- Streaming token usage depends on the upstream sending a usage chunk (`stream_options.include_usage` is requested automatically); if the upstream sends none, input tokens are reported as `0`.
- Actual model behavior and tool-call fidelity depend on the upstream OpenAI-compatible provider.

## Prompt caching

cx2cc does not cache anything itself, but it keeps the upstream provider's automatic prompt caching effective and visible:

- Every request carries a stable `prompt_cache_key`, a uuid5 hash of the conversation's system prompt and first user message — constant across the turns of one conversation, distinct between conversations, and revealing no prompt content. OpenAI-style backends use this key for cache-affine routing; without it, consecutive turns of one conversation can be load-balanced onto different cache nodes and miss a cache that exists. On real agentic sessions this was the difference between ~13% and ~99% observed per-turn hit rates.
- Upstream `usage.prompt_tokens_details.cached_tokens` is reported back to the client as `cache_read_input_tokens` in the streamed `message_delta`.
- Anthropic `cache_control` markers are accepted and ignored; upstream caching is automatic and needs no annotations.

Two accounting caveats, inherited from OpenAI usage semantics:

- The reported `input_tokens` already **includes** the cached portion; `cache_read_input_tokens` is a subset of it, not an addition. Tools that sum Anthropic-style (`input + cache_read`) will double-count cache hits.
- `cache_creation_input_tokens` is always `0`; OpenAI-style upstreams have no cache-write charge.

Set `CX2CC_PROMPT_CACHE_KEY=off` if your upstream rejects unknown request fields.

## Model resolution

Every request's model name goes through the same steps before it reaches the upstream:

1. A trailing window marker such as `[1m]` is stripped.
2. `CX2CC_MODEL_ALIASES` (`from=to,from=to`) remaps the name, e.g. to move a whole fleet pinned to an old slug onto a new default without touching clients.
3. The result is served if it is on the allowlist: `CX2CC_MODEL_PASSTHROUGH` plus whatever the upstream advertises on its own `/models` (cached for ten minutes).
4. Claude Code's own names (`claude-*`, `opus`, `sonnet`, `haiku`, …) and requests without a model run on `CX2CC_UPSTREAM_MODEL`.
5. Any other name is rejected with a 400 that lists what is available (`CX2CC_UNKNOWN_MODEL=reject`, the default); `CX2CC_UNKNOWN_MODEL=default` restores the old behaviour of quietly serving it with the default model.

`GET /v1/models` mirrors the upstream catalog and annotates it with what this proxy does: `default_model`, `aliases`, `unknown_model_policy`, and a `served_as` field on every entry that is remapped. `?refresh=1` is forwarded so a client can force the upstream past its cache. With `CX2CC_REPORT_UPSTREAM_MODEL=true`, responses name the model that actually served the request.

## Web search

Claude Code's WebSearch sends Anthropic's hosted tool (`web_search_<date>`) as a forced side query. cx2cc maps it onto the upstream's hosted `web_search` tool (`allowed_domains` become the upstream `filters`; `blocked_domains` and `max_uses` have no counterpart) and rebuilds `server_tool_use` and `web_search_tool_result` blocks, plus `usage.server_tool_use`, from the upstream's search calls and `url_citation` annotations, streamed and non-streamed. Codex CLI's own search needs no mapping: `/v1/responses` and `/v1/alpha/search` are byte-level passthroughs.

## OpenAI Chat Completions passthrough

For clients that already speak the OpenAI dialect natively (SDKs, tools, other platforms) cx2cc exposes a passthrough entry point that shares the same upstream, key rotation, and prompt-cache-key discipline as the Anthropic path but does not translate the request or the response.

```text
OpenAI-format client
        │ POST /v1/chat/completions   (or /openai/v1/chat/completions)
        ▼
http://127.0.0.1:8901
        │ POST /chat/completions      (verbatim, plus model resolution / cache key)
        ▼
OpenAI-compatible upstream
```

- Endpoints: `POST /v1/chat/completions` (natural OpenAI base-URL layout) and the explicit alias `POST /openai/v1/chat/completions`; `GET /v1/models` and `GET /openai/v1/models` list the same catalog.
- Authorization is the same as `/v1/messages`: either `x-api-key` or `Authorization: Bearer …`.
- Errors follow the OpenAI shape (`{"error": {"message", "type", "code"}}`) rather than the Anthropic shape used on `/v1/messages`, so OpenAI SDKs surface them as normal API errors.
- The request body is forwarded verbatim, with three additive tweaks: model resolution (same rules as `/v1/messages`, see [Model resolution](#model-resolution)), an auto-attached per-conversation `prompt_cache_key` when the client didn't send one (disable with `CX2CC_PROMPT_CACHE_KEY=off`), and `stream_options.include_usage` on streaming requests so token counts still come back.
- Streaming responses are proxied byte-for-byte, so the client sees the upstream's own `chat.completion.chunk` events and `[DONE]` sentinel — no Anthropic-shape translation.
- The `/v1/messages` response-style addendum (see below) is intentionally not injected here; OpenAI-format callers compose their own system prompt.

Example: point any OpenAI SDK at `http://127.0.0.1:8901/v1` as its base URL, use the caller's cx2cc key as the API key, and request one of the slugs the upstream advertises on `/v1/models`.

## Response style injection

cx2cc appends a style addendum to the system prompt of every `/v1/messages` request. Claude-shaped harnesses driving OpenAI-style models tend to produce fragmented transcripts — one short narration block per tool call instead of consolidated prose — and the default addendum (`translator.DEFAULT_STYLE_PROMPT`, written in Chinese) counters that: lead with the conclusion, write full paragraphs, no per-tool-call narration, no re-narrating tool output, structure only when content warrants it, length proportional to information.

- The addendum is **appended, never prepended**, so the client's original system prompt stays a byte-identical prefix and the upstream's prefix cache keeps hitting across turns (one-time miss on the first request after changing the text). The per-conversation `prompt_cache_key` hashes the original client system prompt and is unaffected.
- When the client sends no system prompt at all, the addendum is sent as the entire system message.
- `CX2CC_STYLE=off` disables the injection.
- `CX2CC_STYLE_PROMPT` overrides the default: if the value is a path to a readable file, the file's contents are used; otherwise the value itself is used literally.

## Usage endpoint

`GET /usage` (alias `GET /v1/usage`) forwards the upstream's usage/quota JSON, so a tool like CC Switch can show remaining quota through cx2cc instead of reaching the upstream directly.

- Key handling matches `/v1/messages`: the caller's `x-api-key` / `Authorization: Bearer` is passed through as the upstream bearer token, and an invalid key comes back as the upstream's `401`.
- The upstream URL defaults to the configured base URL with a trailing `/v1` stripped, plus `/usage` (`https://example.com/v1` → `https://example.com/usage`). Set `CX2CC_USAGE_URL` to point somewhere else.
- cx2cc does not interpret the payload. Whatever JSON the upstream answers with HTTP 200 is returned verbatim; the payload shape is therefore upstream-defined. Upstreams without a usage endpoint answer 404, forwarded as a status code only.

In CC Switch, enable usage query on the provider card with a custom script that requests `{{baseUrl}}/usage` with header `x-api-key: {{apiKey}}` and extracts whatever fields your upstream serves.

`GET /accounts` (alias `GET /v1/accounts`) is the same kind of passthrough, for upstreams that multiplex several subscriptions and expose which one is currently serving. The query string is forwarded, so upstream filters such as `?usage=0` keep working through cx2cc. Upstreams without the endpoint answer 404, forwarded as-is.

## Image generation endpoint

`POST /v1/images/generations` (alias `/openai/v1/images/generations`) is a JSON passthrough in the OpenAI Images API shape: `{"prompt", "size", "quality", "background", "n"}` in, `{"data": [{"b64_json": ...}], "size", "quality", "usage", ...}` out. The body is forwarded to `<CX2CC_UPSTREAM_BASE_URL>/images/generations` with the caller's key and the upstream JSON comes back verbatim; codex-bridge serves it from the ChatGPT backend's gpt-image-2 endpoint on the subscription, so no third-party image relay or extra key is involved. The `gpt-image` Claude Code skill (`~/.claude/skills/gpt-image`) is the intended client: it reuses the `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` a machine already has for cx2cc. Timeout is 600 s because one image takes ~15 s upstream and `n` (max 4) is served sequentially.

`POST /v1/images/edits` (alias `/openai/v1/images/edits`) is the image-to-image variant: same body plus `images`, a list of data-URL reference images (up to 16, order meaningful, ~1.5k input image_tokens each) that the bridge forwards to the backend's edits endpoint. A 1 MB PNG is ~1.4 MB of JSON, so the whole chain (NPM, frp, waitress) has to accept multi-megabyte bodies.

## Multi-user gateway

cx2cc authenticates nobody itself: it forwards the caller's key upstream. When one endpoint is shared by several people, put [cx2cc-gateway](gateway/README.md) in front of it. The gateway gives every person their own key (with an alias, owner, scopes, an optional model allowlist, rate / concurrency / daily / 7-day quota limits, expiry, rotation with a grace period, and revocation), swaps it for the single internal credential cx2cc accepts, streams the answer back unbuffered, and records one audit row per call (who, when, from where, which model, how many tokens, how long, what failed; never the prompt or the completion). A web console under `/admin/` has admin, operator (can create keys, sees only their own) and auditor roles, and lets key holders sign in with their own key to see their usage. It is a separate aiohttp service with its own tests and Docker image; cx2cc itself is unchanged.

`site/index.html` is the project page served at the deployment's root.

## Requirements

- Python 3.10+ when running from source. Windows and macOS release packages include native executables and do not require Python.
- An OpenAI Chat Completions compatible upstream endpoint
- An upstream API key, passed through `x-api-key` or `Authorization: Bearer`, or configured as a fallback environment variable

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
| `CX2CC_UPSTREAM_MODEL` | No | `gpt-5.5` | Default upstream model: used for Claude model names, requests without a model, and (with `CX2CC_UNKNOWN_MODEL=default`) unknown names. |
| `CX2CC_MODEL_ALIASES` | No | none | `from=to,from=to` remaps applied after the window-suffix strip and before the allowlist. |
| `CX2CC_MODEL_PASSTHROUGH` | No | none | Comma-separated model names clients may request directly, in addition to the upstream's `/models` list. |
| `CX2CC_UNKNOWN_MODEL` | No | `reject` | `reject` answers unknown model names with a 400; `default` serves them with `CX2CC_UPSTREAM_MODEL`. |
| `CX2CC_REPORT_UPSTREAM_MODEL` | No | off | When `1`/`true`/`yes`/`on`, responses name the model the upstream says it served instead of echoing the client's requested model. |
| `CX2CC_PROMPT_CACHE_KEY` | No | on | Set `off` to stop sending the per-conversation `prompt_cache_key` upstream. |
| `CX2CC_STYLE` | No | on | Set `off` to stop appending the response-style addendum to the system prompt. |
| `CX2CC_STYLE_PROMPT` | No | built-in | Replace the default style addendum: a readable file path (contents used) or literal text. |
| `CX2CC_USAGE_URL` | No | derived | Upstream URL behind `GET /usage`. Defaults to the base URL without its `/v1` suffix plus `/usage`. |
| `CX2CC_HOST` | No | `127.0.0.1` | Local listen host. |
| `CX2CC_PORT` | No | `8901` | Local listen port. |

## Development

```bash
python -m pytest
python -m compileall server.py translator.py prompt_estimate.py start-cx2cc.py
```

The gateway has its own dependencies and test suite:

```bash
cd gateway
python -m pip install -r requirements-dev.txt
python -m pytest
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
