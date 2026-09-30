# cx2cc-gateway

A multi-user front door for cx2cc: every person gets their own API key, every
call is authenticated, limited and audited, and a web console manages it all.
cx2cc itself keeps accepting exactly one internal credential; the gateway swaps
each caller's key for it, so nothing behind the gateway has to know how many
users there are.

```text
clients (Claude Code, Codex CLI, cc-switch, Chatbox, OpenAI SDKs)
   │  HTTPS  https://cx2cc.example.com/api/...   personal key (x-api-key or Bearer)
   ▼
reverse proxy (nginx / NPM)          TLS, CORS preflight
   │  /api/*  and  /admin/*
   ▼
cx2cc-gateway :8000                  per-key auth, scopes, model allowlist,
   │                                 rate / concurrency / quota limits, audit,
   │  internal token                 management console
   ▼
cx2cc                                protocol translation (unchanged)
```

## What it does

**Keys.** Each key has an alias (unique, e.g. `bob-macbook`), an owner, a note,
and is stored as a SHA-256 hash; the secret (`sk-cx2cc-…`) is shown once, at
creation or rotation. Keys can be disabled and re-enabled, given an expiry,
rotated with a 0–168 hour grace period for the old secret, or revoked for good.
An existing shared token can be imported as a key (`import-key`), so the
switch to personal keys can happen client by client.

**Scopes.** `chat` (Messages, Chat Completions, Responses, search), `images`
(generations and edits), `usage` (subscription quota) and `accounts` (the
upstream account pool, which includes subscription e-mail addresses). Keys
without `accounts` get `/usage` with the e-mail and account identifiers
removed.

**Model allowlist.** Optional per key. A request passes when either the
requested name or the model cx2cc resolves it to (via its alias table, read
from `/v1/models`) is on the list; Claude Code's own model names and requests
without a model count as cx2cc's default model.

**Limits.** Requests per minute, concurrent requests, daily and 7-day weighted
token quotas. Exceeding one answers `429` with `Retry-After`. Weighted tokens are
`(input − cached) × 1 + cached × 0.1 + output × 8` by default, matching how the
subscription meters usage; the weights are settings.

**Audit.** One row per call: time, key, client IP, user agent, route, requested
and served model, input / cached / output tokens, weighted tokens, request and
response size, time to first byte, duration, conversation id, and the failure
reason if any. Prompts and completions are never stored. Streams are forwarded
chunk by chunk and read on the fly; usage comes from the final SSE frames
(`message_delta`, the last Chat Completions chunk, `response.completed`) or the
JSON body. Every response carries `X-Cx2cc-Request-Id`, which is the audit row's
`rid`. Rows are written on a background thread; per-day rollups are kept
forever, raw rows for `retention_days` (default 180).

**Admin events.** Logins and failed logins, key creation / edit / disable /
enable / rotation / revocation, console-user and settings changes, and audit
exports, with actor and IP.

**Console.** `/admin/`: overview (totals, daily usage stacked by key, per-owner
and per-model breakdowns, recent failures), key list and detail (daily and
hourly series, model mix, source IPs, latency, recent calls), request search
with CSV export, event log, console users, settings, and a read-only view of
the upstream model catalog, aliases and account pool.

| Role | Can |
| --- | --- |
| `admin` | everything: all keys, the `accounts` scope, console users, settings, upstream view |
| `operator` | create keys; see and manage only the keys they created |
| `auditor` | read everything, change nothing |
| key holder | sign in with their own API key and see only that key |

Sessions are server-side, cookies are `HttpOnly; Secure; SameSite=Strict`,
state-changing calls need the per-session CSRF token, passwords are scrypt
hashes, and ten failed sign-ins from one IP lock it out for 15 minutes.

## Routes

The gateway serves the API under `CX2CC_GW_API_PREFIX` (default `/api`) and
forwards the rest of the path to cx2cc unchanged.

| Path | Scope | Notes |
| --- | --- | --- |
| `POST /v1/messages` | chat | Anthropic Messages, streamed or not |
| `POST /v1/chat/completions` | chat | also `/openai/v1/...` |
| `POST /v1/responses` | chat | Codex CLI |
| `POST /v1/alpha/search` | chat | Codex CLI standalone web search |
| `POST /v1/images/generations`, `/v1/images/edits` | images | |
| `GET /usage` | usage | e-mail / account ids stripped without `accounts` |
| `GET /accounts` | accounts | |
| `GET /v1/models`, `GET /health` | none | anonymous, not audited |

Unknown paths need a valid key and are forwarded (cx2cc answers 404); without a
key they get 404 directly. Errors use the Anthropic envelope on Anthropic routes
and the OpenAI envelope on OpenAI routes.

## Running it

```bash
cp .env.example .env        # set CX2CC_GW_UPSTREAM and CX2CC_GW_UPSTREAM_TOKEN
mkdir -p data && sudo chown 10001 data
docker compose up -d --build
docker exec -i cx2cc-gateway python -m cx2cc_gateway create-user --username admin --role admin <<< 'a-long-password'
# optional: keep an existing shared token working during the switch
docker exec -i cx2cc-gateway python -m cx2cc_gateway import-key --alias legacy-shared \
  --scopes chat,images,usage,accounts <<< "$OLD_SHARED_TOKEN"
```

Behind nginx:

```nginx
location /api/ {
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 900s;
    client_max_body_size 64m;
    proxy_pass http://127.0.0.1:13030;      # keep the /api prefix
}
location /admin/ {
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_pass http://127.0.0.1:13030;
}
```

Publish the container port only where the reverse proxy can reach it: the
gateway trusts `X-Real-IP` / `X-Forwarded-For` for the client IP
(`CX2CC_GW_TRUST_PROXY=1`).

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `CX2CC_GW_UPSTREAM` | `http://127.0.0.1:8901` | cx2cc as seen from the gateway |
| `CX2CC_GW_UPSTREAM_TOKEN` | required | the one key cx2cc accepts |
| `CX2CC_GW_API_PREFIX` | `/api` | path prefix kept by the reverse proxy |
| `CX2CC_GW_DATA_DIR` | `./data` (`/data` in the image) | SQLite database location |
| `CX2CC_GW_COOKIE_SECURE` | `1` | `0` only for plain-HTTP local testing |
| `CX2CC_GW_TRUST_PROXY` | `1` | read the client IP from proxy headers |
| `CX2CC_GW_SESSION_HOURS` | `12` | console session lifetime |
| `CX2CC_GW_UPSTREAM_READ_TIMEOUT` | `900` | seconds of upstream silence tolerated mid-response |
| `CX2CC_GW_MAX_BODY_MB` | `96` | request body limit |
| `CX2CC_GW_HOST` / `CX2CC_GW_PORT` | `0.0.0.0` / `8000` | listen address |

### Command line

```text
python -m cx2cc_gateway serve
python -m cx2cc_gateway create-user --username NAME --role admin|operator|auditor   (password on stdin)
python -m cx2cc_gateway reset-password --username NAME                             (password on stdin)
python -m cx2cc_gateway create-key --alias ALIAS [--owner O] [--scopes chat,images,usage]
python -m cx2cc_gateway import-key --alias ALIAS [--scopes ...]                    (secret on stdin)
python -m cx2cc_gateway list-keys
```

The CLI writes the same database; a running gateway picks key changes up within
five seconds.

### Importing history

Usage from before the gateway can be rebuilt from cx2cc's and codex-bridge's
own logs and shown alongside live data (daily totals only, attributed to one
key, typically the imported shared token):

```bash
python tools/import_cx2cc_logs.py /path/to/codex-bridge/logs --cutoff-ms <first gateway request> > history.json
docker exec -i cx2cc-gateway python -m cx2cc_gateway import-history   --key legacy-shared --source cx2cc-logs < history.json
```

`--cutoff-ms` is the timestamp of the first request the gateway recorded
(`SELECT MIN(ts) FROM requests`), so nothing is counted twice. Messages and
non-streamed Chat Completions calls come back with full token counts, image
calls with their image tokens; Responses and streamed Chat Completions calls
are counted without tokens, because cx2cc never logged them. Models are joined
through the conversation key the bridge logs. Re-running with the same
`--source` replaces the earlier import. Reports read the `usage_all` view, i.e.
live rollups plus imported history.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The tests run the gateway against an in-process fake cx2cc and cover
credential swapping, streaming without buffering, usage extraction for all
three dialects, scopes, allowlists, limits, rotation, revocation, client
disconnects, role separation, CSRF and the CSV export.
