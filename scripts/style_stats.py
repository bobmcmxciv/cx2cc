"""Compare assistant text-block shape across models in local Claude Code transcripts.

Scans ~/.claude/projects/**/*.jsonl (recently modified), buckets assistant text
blocks by the model that produced them, and prints per-model shape stats:
block count, median length, share of short fragments, blocks per turn.

Used to ground cx2cc's DEFAULT_STYLE_PROMPT in observed behaviour: the upstream
GPT models narrate one short block per tool call, while Fable consolidates.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
MAX_AGE_DAYS = float(sys.argv[1]) if len(sys.argv) > 1 else 21


def main() -> None:
    cutoff = time.time() - MAX_AGE_DAYS * 86400
    stats = defaultdict(lambda: {"blocks": [], "turns": 0, "sessions": set()})

    for path in PROJECTS.glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    msg = rec.get("message") or {}
                    model = msg.get("model") or "?"
                    texts = [
                        b.get("text", "")
                        for b in (msg.get("content") or [])
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    if not texts:
                        continue
                    s = stats[model]
                    s["turns"] += 1
                    s["sessions"].add(path.name)
                    s["blocks"].extend(len(t) for t in texts if t.strip())
        except OSError:
            continue

    print(f"{'model':<28} {'sessions':>8} {'blocks':>7} {'median':>7} {'<80ch':>6} {'blk/turn':>8}")
    for model, s in sorted(stats.items(), key=lambda kv: -len(kv[1]["blocks"])):
        blocks = s["blocks"]
        if len(blocks) < 20:
            continue
        med = statistics.median(blocks)
        short = sum(1 for b in blocks if b < 80) / len(blocks)
        print(
            f"{model:<28} {len(s['sessions']):>8} {len(blocks):>7} {med:>7.0f} "
            f"{short:>6.0%} {len(blocks) / s['turns']:>8.2f}"
        )


if __name__ == "__main__":
    main()
