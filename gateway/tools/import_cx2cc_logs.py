#!/usr/bin/env python3
"""Rebuild daily usage from cx2cc and codex-bridge service logs.

Before the gateway existed, the only record of who used how much was the
services' own logs. This script turns them into daily rows for
`python -m cx2cc_gateway import-history`:

    python import_cx2cc_logs.py LOGDIR --cutoff-ms <first gateway request> > history.json
    docker exec -i cx2cc-gateway python -m cx2cc_gateway import-history \\
        --key legacy-shared --source cx2cc-logs < history.json

What the logs contain, and so what can be rebuilt:

* cx2cc logs one line per finished /v1/messages call (streamed and not) and per
  non-streamed /v1/chat/completions call, with input, cached and output tokens
  and the first 8 characters of the conversation's prompt_cache_key.
* codex-bridge logs which model served each conversation key, every
  /v1/responses call (no token counts), and every image call with its image
  tokens.

So Messages and non-streamed Chat usage is complete, image usage is complete,
Responses (Codex CLI) and streamed Chat calls are counted without tokens. A
call's model is the one the bridge last reported serving for the same
conversation key; failing that, the model the bridge served most that day.

Log lines only carry HH:MM:SS in the service host's local time. Each file's
dates are rebuilt backwards from its modification time (= the time of its last
line), stepping back a day whenever the clock jumps forward by more than an
hour. Days are then bucketed in the display timezone (UTC+8 by default).
Events at or after --cutoff-ms are left out: from then on the gateway records
the traffic itself.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2}) ")
KEY = re.compile(r"\bkey=([0-9a-f]{8})")

CX2CC_USAGE = (
    ("messages", re.compile(r"<- stream in=(\d+) cached=(\d+) out=(\d+)")),
    ("messages", re.compile(r"<- (?!\[)\w+ \| in=(\d+) cached=(\d+) out=(\d+)")),
    ("chat", re.compile(r"<- \[openai\] in=(\d+) cached=(\d+) out=(\d+)")),
)
CX2CC_ERRORS = re.compile(
    r"Upstream (?:bridge )?request failed with status \d+|Stream translation error|"
    r"rejected unknown model|OpenAI stream proxy error|Responses stream proxy error"
)
CX2CC_CHAT_STREAM = re.compile(r"-> \[openai\] \S+ \| stream=True")

BRIDGE_SERVED = re.compile(r"<- served by account=\S+ model=(\S+) key=([0-9a-f]{8})")
BRIDGE_REQUESTED = re.compile(r"-> requested=\S+ served=(\S+) .*cache_key=\w+:([0-9a-f]{8})")
# Early August lines have the served model but no conversation key.
BRIDGE_REQUESTED_NOKEY = re.compile(r"-> requested=\S+ served=(\S+) ")
BRIDGE_RESPONSES = re.compile(r"<- \[responses\] served by account=\S+ model=(\S+)")
BRIDGE_IMAGES = re.compile(r"<- \[images/\w+\] account=\S+ .*?in_image_tokens=(\d+) out_image_tokens=(\d+)")
IMAGE_MODEL = "gpt-image-2"


def timed_lines(path: str, local_offset_h: float):
    """Yield (epoch_seconds_utc, line) for every line that starts with a time."""
    end_utc = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
    end_local = end_utc + timedelta(hours=local_offset_h)
    end_sod = end_local.hour * 3600 + end_local.minute * 60 + end_local.second
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = []
        for line in fh:
            m = TIME.match(line)
            if m:
                h, mi, s = (int(x) for x in m.groups())
                raw.append((h * 3600 + mi * 60 + s, line))
    if not raw:
        return []
    day = end_local.date()
    out = [None] * len(raw)
    nxt = None
    for i in range(len(raw) - 1, -1, -1):
        sod, line = raw[i]
        if nxt is None:
            if sod > end_sod + 120:
                day -= timedelta(days=1)
        elif sod > nxt + 3600:
            day -= timedelta(days=1)
        nxt = sod
        local = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(seconds=sod)
        out[i] = ((local - timedelta(hours=local_offset_h)).timestamp(), line)
    return out


def files(logdir: str, prefix: str) -> list[str]:
    return sorted(glob.glob(os.path.join(logdir, prefix + "*.log")), key=os.path.getmtime)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("logdir")
    ap.add_argument("--cutoff-ms", type=int, required=True, help="first request the gateway recorded")
    ap.add_argument("--local-utc-offset", type=float, default=-7.0,
                    help="UTC offset of the host that wrote the logs, hours (default -7)")
    ap.add_argument("--tz-offset-minutes", type=int, default=480, help="display timezone (default UTC+8)")
    args = ap.parse_args()
    cutoff = args.cutoff_ms / 1000

    def bucket(ts: float) -> str:
        return (datetime.fromtimestamp(ts, timezone.utc) + timedelta(minutes=args.tz_offset_minutes)).strftime("%Y-%m-%d")

    # 1. Which model served each conversation key, and the day's dominant model.
    served: dict[str, list[tuple[float, str]]] = defaultdict(list)
    per_day_models: dict[str, Counter] = defaultdict(Counter)
    rows: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])  # req, err, in, cached, out
    route_totals: Counter = Counter()

    for path in files(args.logdir, "codex-bridge.err"):
        for ts, line in timed_lines(path, args.local_utc_offset):
            m = BRIDGE_SERVED.search(line) or BRIDGE_REQUESTED.search(line)
            if m:
                served[m.group(2)].append((ts, m.group(1)))
                per_day_models[bucket(ts)][m.group(1)] += 1
                continue
            m = BRIDGE_REQUESTED_NOKEY.search(line)
            if m:
                per_day_models[bucket(ts)][m.group(1)] += 1
                continue
            if ts >= cutoff:
                continue
            m = BRIDGE_RESPONSES.search(line)
            if m:
                rows[(bucket(ts), m.group(1))][0] += 1
                route_totals["responses"] += 1
                continue
            m = BRIDGE_IMAGES.search(line)
            if m:
                r = rows[(bucket(ts), IMAGE_MODEL)]
                r[0] += 1
                r[2] += int(m.group(1))
                r[4] += int(m.group(2))
                route_totals["images"] += 1
    for lst in served.values():
        lst.sort()
    served_ts = {k: [t for t, _ in v] for k, v in served.items()}

    def model_for(key: str | None, ts: float) -> str:
        if key and key in served:
            i = bisect_right(served_ts[key], ts + 10) - 1
            if i >= 0 and ts - served_ts[key][i] < 86400:
                return served[key][i][1]
        common = per_day_models.get(bucket(ts))
        return common.most_common(1)[0][0] if common else "unknown"

    # 2. Token usage and failures from cx2cc.
    joined = unjoined = 0
    for path in files(args.logdir, "cx2cc.err"):
        for ts, line in timed_lines(path, args.local_utc_offset):
            if ts >= cutoff:
                continue
            if "cached=" in line:
                for route, pat in CX2CC_USAGE:
                    m = pat.search(line)
                    if not m:
                        continue
                    km = KEY.search(line)
                    key = km.group(1) if km else None
                    if key and key in served:
                        joined += 1
                    else:
                        unjoined += 1
                    r = rows[(bucket(ts), model_for(key, ts))]
                    r[0] += 1
                    r[2] += int(m.group(1))
                    r[3] += int(m.group(2))
                    r[4] += int(m.group(3))
                    route_totals[route] += 1
                    break
                continue
            if CX2CC_ERRORS.search(line):
                r = rows[(bucket(ts), model_for(None, ts))]
                r[0] += 1
                r[1] += 1
                route_totals["errors"] += 1
            elif CX2CC_CHAT_STREAM.search(line):
                rows[(bucket(ts), model_for(None, ts))][0] += 1
                route_totals["chat_stream_no_tokens"] += 1

    out_rows = [
        {"day": day, "model": model, "requests": v[0], "errors": v[1], "input_tokens": v[2],
         "cached_tokens": v[3], "output_tokens": v[4]}
        for (day, model), v in sorted(rows.items())
    ]
    days = sorted({r["day"] for r in out_rows})
    meta = {
        "cutoff_ms": args.cutoff_ms,
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "routes": dict(route_totals),
        "model_join": {"by_conversation_key": joined, "by_daily_majority": unjoined},
    }
    json.dump({"meta": meta, "rows": out_rows}, sys.stdout, ensure_ascii=False)

    per_day: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    for r in out_rows:
        acc = per_day[r["day"]]
        for i, f in enumerate(("requests", "errors", "input_tokens", "cached_tokens", "output_tokens")):
            acc[i] += r[f]
    print(f"{'day':12}{'requests':>10}{'errors':>8}{'input':>16}{'cached':>16}{'output':>13}{'weighted':>16}",
          file=sys.stderr)
    for day in days:
        req, err, inp, cached, out = per_day[day]
        weighted = (inp - cached) + cached * 0.1 + out * 8
        print(f"{day:12}{req:>10,}{err:>8,}{inp:>16,}{cached:>16,}{out:>13,}{weighted:>16,.0f}", file=sys.stderr)
    print(json.dumps(meta, ensure_ascii=False), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
