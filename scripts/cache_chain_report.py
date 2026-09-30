#!/usr/bin/env python3
"""Where does cx2cc's uncached input actually come from?"""
import glob
import json
import os
import sys
from datetime import datetime

KEYED = "2026-07-29T07:15:11Z"      # prompt_cache_key deployed

src = sys.argv[1] if len(sys.argv) > 1 else "."
rows = []
seen = set()          # a resumed session re-records earlier turns; count each once
dupes = 0
for path in sorted(glob.glob(os.path.join(src, "chain_*.json"))):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    for r in d["rows"]:
        if r[2] < KEYED:
            continue
        if r[1] in seen:
            dupes += 1
            continue
        seen.add(r[1])
        rows.append([d["host"]] + r)
print(f"(dropped {dupes} duplicate turn records left behind by resumed/forked sessions)")


def secs(a, b):
    f = "%Y-%m-%dT%H:%M:%S"
    return (datetime.strptime(b[:19], f) - datetime.strptime(a[:19], f)).total_seconds()


roots = [r for r in rows if r[8] is None]
cont = [r for r in rows if r[8] is not None]
tot_in = sum(r[5] for r in rows)
tot_cr = sum(r[6] for r in rows)
print(f"turns={len(rows)}  input={tot_in:,}  cache_read={tot_cr:,}  hit={tot_cr/tot_in*100:.2f}%")
print(f"  branch roots (no ancestor turn, cold by nature): {len(roots)}, "
      f"input={sum(r[5] for r in roots):,}, cache_read={sum(r[6] for r in roots):,}")
print(f"  continuation turns:                              {len(cont)}, "
      f"input={sum(r[5] for r in cont):,}, cache_read={sum(r[6] for r in cont):,}")
print()

# On a continuation turn the ancestor's whole prompt is a prefix of this one, so
# cache_read should reach prev_input (modulo the upstream's 128-token rounding).
print("continuation turns by retention = cache_read / ancestor's input")
print(f"{'bucket':<34}{'turns':>7}{'median gap':>12}{'lost input':>16}{'share':>8}")
buckets = [(-0.001, 0.001, "0     prefix dropped entirely"),
           (0.001, 0.5, "<50%  prefix mostly dropped"),
           (0.5, 0.9, "50-90% partial"),
           (0.9, 0.99, "90-99% near-complete"),
           (0.99, 9e9, ">=99% healthy")]
lost_total = 0
for lo, hi, label in buckets:
    sel = [r for r in cont if lo <= (r[6] / r[8] if r[8] else 0) < hi]
    if not sel:
        continue
    lost = sum(max(0, min(r[8], r[5]) - r[6]) for r in sel)
    lost_total += lost
    gaps = sorted(secs(r[9], r[3]) for r in sel)
    print(f"{label:<34}{len(sel):>7}{gaps[len(gaps)//2]:>10.0f}s{lost:>16,}{lost/tot_in*100:>7.1f}%")
print(f"{'TOTAL re-billed prefix':<34}{'':>7}{'':>11}{lost_total:>16,}{lost_total/tot_in*100:>7.1f}%")
print()
print(f"ceiling if every prefix were retained: "
      f"{(tot_cr + lost_total)/tot_in*100:.2f}%  (now {tot_cr/tot_in*100:.2f}%)")
print()

broken = [r for r in cont if r[8] and r[6] / r[8] < 0.9]
print(f"the {len(broken)} broken-prefix turns, by idle gap before them:")
for lo, hi in [(0, 30), (30, 120), (120, 600), (600, 3600), (3600, 9e9)]:
    sel = [r for r in broken if lo <= secs(r[9], r[3]) < hi]
    if sel:
        print(f"  gap {lo:>5}-{hi if hi < 9e8 else 'inf':>5}s : {len(sel):>5} turns "
              f"({len(sel)/len(broken)*100:>5.1f}%), lost {sum(max(0, min(r[8], r[5]) - r[6]) for r in sel):>13,}")
print()
print("main chain vs subagent branches:")
for side, label in ((False, "main chain"), (True, "subagent (isSidechain)")):
    sel = [r for r in rows if r[4] is side]
    if not sel:
        continue
    i = sum(r[5] for r in sel)
    c = sum(r[6] for r in sel)
    b = [r for r in sel if r[8] and r[6] / r[8] < 0.9]
    print(f"  {label:<24} turns={len(sel):>5} input={i:>13,} hit={c/i*100:>6.2f}% "
          f"broken-prefix turns={len(b)}")
print()
print("worst offenders (file, turns with broken prefix, tokens re-billed):")
agg = {}
for r in broken:
    k = (r[0], r[1])
    a = agg.setdefault(k, [0, 0])
    a[0] += 1
    a[1] += max(0, min(r[8], r[5]) - r[6])
for k, v in sorted(agg.items(), key=lambda kv: -kv[1][1])[:8]:
    print(f"  {k[0][:14]:<15} {k[1]:<10} turns={v[0]:>4}  re-billed={v[1]:>13,}")
