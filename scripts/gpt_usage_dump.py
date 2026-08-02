#!/usr/bin/env python3
"""Dump one row per gpt-* assistant turn found in Claude Code transcripts.

Row: [sessionId, timestamp, model, input, cache_read, cache_creation, output, cwd]
Classification of which rows actually came through cx2cc is done by the merger,
not here -- this side stays a dumb, auditable extractor.
"""
import json
import os
import sys
import glob

home = os.path.expanduser("~")
roots = sys.argv[1:] or [os.path.join(home, ".claude", "projects")]

rows = []
seen = set()
files = 0
for root in roots:
    for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        files += 1
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"gpt' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                m = d.get("message") or {}
                model = str(m.get("model") or "")
                if not model.startswith("gpt"):
                    continue
                mid = m.get("id")
                if mid:
                    if mid in seen:
                        continue
                    seen.add(mid)
                u = m.get("usage") or {}
                rows.append([
                    (d.get("sessionId") or os.path.basename(path)[:-6])[:8],
                    d.get("timestamp") or "",
                    model,
                    u.get("input_tokens") or 0,
                    u.get("cache_read_input_tokens") or 0,
                    u.get("cache_creation_input_tokens") or 0,
                    u.get("output_tokens") or 0,
                    d.get("cwd") or "",
                ])

hostname = os.environ.get("COMPUTERNAME") or ""
if not hostname and hasattr(os, "uname"):
    hostname = os.uname().nodename
print(json.dumps({"host": hostname or "?", "scanned_files": files, "rows": rows}))
