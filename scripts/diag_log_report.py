#!/usr/bin/env python3
"""Attribute cache-hit collapses using cx2cc's per-request diag log lines.

Feeds on the `<- ... | msgs=N key=K tools=T h[...]` lines that server.py and
translator.py emit (one per completed response). Consecutive completions that
share a `prompt_cache_key` are consecutive turns of one conversation; their
prompts share a prefix, so every hash snapshot at an index both turns cover
must match. On a turn whose `cached` collapsed, that yields the attribution
the transcripts could never give:

  - some shared snapshot differs  -> the prompt itself diverged; the first
                                     differing index brackets where
  - all shared snapshots match    -> the content was intact and the upstream
                                     dropped a cache it could have used

Usage: python diag_log_report.py <cx2cc.err.log> [more logs...]
"""
import re
import sys
from collections import Counter, defaultdict

LINE = re.compile(
    r"<- (?:stream|\S+) \| ?in=(\d+) cached=(\d+) out=(\d+) \| "
    r"msgs=(\d+) key=(\S+) tools=(\d+) h\[([^\]]*)\]"
)
# translator's stream line has no leading "| " after "<- stream"
STREAM_LINE = re.compile(
    r"<- stream in=(\d+) cached=(\d+) out=(\d+) \| "
    r"msgs=(\d+) key=(\S+) tools=(\d+) h\[([^\]]*)\]"
)
REQ = re.compile(r"-> diag msgs=\d+ key=(\S+)")


def marks_of(s):
    out = {}
    for part in s.split():
        idx, h = part.split(":", 1)
        out[idx] = h
    return out


turns = defaultdict(list)   # key -> [(in, cached, out, msgs, marks)]
requests_seen = Counter()
for path in sys.argv[1:]:
    for line in open(path, encoding="utf-8", errors="replace"):
        m = STREAM_LINE.search(line) or LINE.search(line)
        if m:
            i, c, o, msgs, key, tools, h = m.groups()
            turns[key].append((int(i), int(c), int(o), int(msgs), marks_of(h)))
            continue
        r = REQ.search(line)
        if r:
            requests_seen[r.group(1)] += 1

tot_in = tot_cached = tot_out = n = 0
first_turns = len(turns)
healthy = retries = 0
dropped = []            # (key, lost) upstream dropped an intact prefix
diverged = Counter()    # first differing index -> count
diverged_lost = 0
for key, seq in turns.items():
    for prev, cur in zip(seq, seq[1:]):
        n += 1
        pi, pc, po, pmsgs, pmarks = prev
        ci, cc, co, cmsgs, cmarks = cur
        tot_in += ci
        tot_cached += cc
        tot_out += co
        if cmsgs == pmsgs and cmarks == pmarks:
            retries += 1
            continue
        expected = min(pi, ci)
        if cc >= 0.9 * expected:
            healthy += 1
            continue
        shared = sorted(
            (k for k in pmarks.keys() & cmarks.keys() if k != "t"), key=int
        )
        bad = [k for k in (["t"] + shared) if pmarks[k] != cmarks[k]]
        lost = max(0, expected - cc)
        if bad:
            diverged[bad[0]] += 1
            diverged_lost += lost
        else:
            dropped.append((key, lost))

for key, seq in turns.items():
    i, c, o, msgs, _ = seq[0]
    tot_in += i
    tot_cached += c
    tot_out += o

total_reqs = sum(requests_seen.values())
print(f"conversations={first_turns}  completed turns={sum(len(s) for s in turns.values())}  "
      f"requests logged={total_reqs}  (gap = aborted/errored before the usage frame)")
if tot_in:
    print(f"input={tot_in:,}  cached={tot_cached:,}  hit={tot_cached / tot_in * 100:.2f}%  output={tot_out:,}")
print()
print(f"continuation pairs analysed : {n}")
print(f"  healthy (>=90% retention) : {healthy}")
print(f"  retries (identical prompt): {retries}")
print(f"  UPSTREAM DROPPED intact prefix: {len(dropped)} turns, "
      f"{sum(l for _, l in dropped):,} tokens re-billed")
print(f"  PROMPT DIVERGED           : {sum(diverged.values())} turns, "
      f"{diverged_lost:,} tokens re-billed")
for idx, cnt in sorted(diverged.items(), key=lambda kv: -kv[1]):
    where = "tools/system" if idx == "t" else f"by message {idx}"
    print(f"      first divergence {where}: {cnt}")
