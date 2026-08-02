#!/usr/bin/env python3
"""Per-turn cache diagnostics that follow the real conversation branch.

A Claude Code transcript is a tree: entries link to their predecessor by
parentUuid, and a session interleaves the main chain with subagent branches.
Ordering a session's gpt turns by timestamp therefore pairs turns that were
never in the same prompt, which makes retention nonsense (that is what produced
retention > 100%). Here each assistant turn is instead compared against its
nearest assistant ANCESTOR, which is by construction the turn whose prompt is a
prefix of this one.

Row: [file, message_id, ts, isSidechain, input, cache_read, output,
      prev_input, prev_ts]   (prev_* are null for a branch root)

message_id is carried so the report can drop the copies a resumed/forked session
leaves behind: the same turn is re-recorded in the new transcript file and would
otherwise be counted twice.
"""
import glob
import json
import os
import sys

roots = sys.argv[1:] or [os.path.join(os.path.expanduser("~"), ".claude", "projects")]

out_rows = []
files = 0
for root in roots:
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        parent = {}      # uuid -> parentUuid
        turn = {}        # uuid -> (ts, sidechain, in, cr, out)
        order = []
        has_gpt = False
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                u = d.get("uuid")
                if not u:
                    continue
                parent[u] = d.get("parentUuid")
                m = d.get("message") or {}
                if d.get("type") != "assistant":
                    continue
                if not str(m.get("model") or "").startswith("gpt"):
                    continue
                us = m.get("usage") or {}
                i = us.get("input_tokens") or 0
                cr = us.get("cache_read_input_tokens") or 0
                if i == 0 and cr == 0:
                    continue          # interrupted turn, no usage frame
                has_gpt = True
                turn[u] = (d.get("timestamp") or "", bool(d.get("isSidechain")),
                           i, cr, us.get("output_tokens") or 0, m.get("id") or u)
                order.append(u)
        if not has_gpt:
            continue
        files += 1
        name = os.path.basename(path)[:-6][:8]
        for u in order:
            ts, side, i, cr, o, mid = turn[u]
            # nearest assistant ancestor == the previous turn of this same branch
            p = parent.get(u)
            depth = 0
            while p is not None and p not in turn:
                p = parent.get(p)
                depth += 1
                if depth > 5000:
                    p = None
                    break
            prev = turn.get(p) if p else None
            out_rows.append([name, mid, ts, side, i, cr, o,
                             prev[2] if prev else None,
                             prev[0] if prev else None])

hostname = os.environ.get("COMPUTERNAME") or ""
if not hostname and hasattr(os, "uname"):
    hostname = os.uname().nodename
print(json.dumps({"host": hostname or "?", "files_with_gpt": files, "rows": out_rows}))
