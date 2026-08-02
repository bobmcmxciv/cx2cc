#!/usr/bin/env python3
"""Merge gpt_usage_dump.py output from several hosts into a cx2cc cache-hit report.

Only turns served after cx2cc learned to report streamed usage (commit d2d7bf0,
2026-07-28 09:14:17 -0700 == 16:14:17Z) carry token counts at all; before that
every streamed request was recorded as input=0/cache_read=0. The nonzero gpt-*
usage seen before that timestamp therefore came from a different, Anthropic-format
relay -- recognisable because there `input_tokens` EXCLUDES the cached part, so
cache_read routinely exceeds input. Those turns are reported separately and are
NOT mixed into the cx2cc hit rate.

cx2cc semantics: input_tokens == OpenAI prompt_tokens, which INCLUDES the cached
part, so hit rate = cache_read / input.
"""
import glob
import json
import os
import sys

CUTOFF = "2026-07-28T16:14:17Z"

src = sys.argv[1] if len(sys.argv) > 1 else "."
rows = []
hosts = {}
for path in sorted(glob.glob(os.path.join(src, "dump_*.json"))):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    hosts[d["host"]] = d["scanned_files"]
    for r in d["rows"]:
        rows.append([d["host"]] + r)


def pct(a, b):
    return (a / b * 100) if b else 0.0


def agg():
    return {"calls": 0, "in": 0, "cr": 0, "out": 0, "cold": 0}


def add(a, i, cr, out):
    a["calls"] += 1
    a["in"] += i
    a["cr"] += cr
    a["out"] += out
    if cr == 0:
        a["cold"] += 1


per_host = {}
per_sess = {}
legacy = agg()
untimed = agg()
bad_shape = 0

for host, sid, ts, model, i, cr, cc, out, cwd in rows:
    if ts < CUTOFF:
        add(legacy, i, cr, out)
        continue
    if i == 0 and cr == 0:          # interrupted / aborted turn, no usage frame
        add(untimed, i, cr, out)
        continue
    if cr > i:
        bad_shape += 1
    add(per_host.setdefault(host, agg()), i, cr, out)
    k = (host, sid)
    s = per_sess.setdefault(k, {"a": agg(), "cwd": cwd, "first": ts, "last": ts, "models": set()})
    add(s["a"], i, cr, out)
    s["models"].add(model)
    s["first"] = min(s["first"], ts)
    s["last"] = max(s["last"], ts)
    if cwd:
        s["cwd"] = cwd

W = 108
print("cx2cc cache-hit report   (turns served after %s)" % CUTOFF)
print("hit%% = cache_read_input_tokens / input_tokens   (cx2cc reports OpenAI prompt_tokens,")
print("        which already contains the cached part -- cache_read is a subset, not an extra)")
print("=" * W)
print(f"{'SESSION':9} {'CALLS':>5} {'INPUT':>14} {'CACHE_READ':>14} {'HIT%':>7} {'COLD':>5}  {'STARTED (UTC)':<17} CWD")
grand = agg()
for host in sorted(per_host, key=lambda h: -per_host[h]["calls"]):
    a = per_host[host]
    for k in grand:
        grand[k] += a[k]
    print("-" * W)
    print(f"HOST {host:<20} transcripts={hosts[host]:<5} sessions="
          f"{sum(1 for (h, _s) in per_sess if h == host)} calls={a['calls']}")
    ss = sorted(((k, v) for k, v in per_sess.items() if k[0] == host),
                key=lambda kv: -kv[1]["a"]["calls"])
    for (h, sid), s in ss:
        b = s["a"]
        print(f"{sid:9} {b['calls']:>5} {b['in']:>14,} {b['cr']:>14,} "
              f"{pct(b['cr'], b['in']):>6.2f}% {b['cold']:>5}  {s['first'][:16].replace('T', ' '):<17} "
              f"{s['cwd'][-34:]}")
    print(f"{'SUBTOTAL':9} {a['calls']:>5} {a['in']:>14,} {a['cr']:>14,} "
          f"{pct(a['cr'], a['in']):>6.2f}% {a['cold']:>5}")

print("=" * W)
print(f"{'ALL HOSTS':9} {grand['calls']:>5} {grand['in']:>14,} {grand['cr']:>14,} "
      f"{pct(grand['cr'], grand['in']):>6.2f}% {grand['cold']:>5}")
print()
print(f"  cache-hit rate (token weighted) : {pct(grand['cr'], grand['in']):.2f}%")
print(f"  fresh (uncached) input          : {grand['in'] - grand['cr']:,} tokens")
print(f"  output                          : {grand['out']:,} tokens")
print(f"  turns with zero cache hit       : {grand['cold']} / {grand['calls']} "
      f"({pct(grand['cold'], grand['calls']):.1f}% of turns)")
print(f"  sanity: turns with cache_read > input (impossible under cx2cc semantics): {bad_shape}")
print()
print(f"excluded, no usage frame (interrupted turns) : {untimed['calls']} turns")
print(f"excluded, pre-{CUTOFF[:10]} (cx2cc reported no streamed usage; these came from an")
print(f"  Anthropic-format relay, input EXCLUDES cache)  : {legacy['calls']} turns, "
      f"input={legacy['in']:,} cache_read={legacy['cr']:,} "
      f"-> hit {pct(legacy['cr'], legacy['in'] + legacy['cr']):.2f}% under Anthropic semantics")
