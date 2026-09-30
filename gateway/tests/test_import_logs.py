import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "import_cx2cc_logs.py"


def write(path: Path, lines: list[str], mtime_utc: datetime) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ts = mtime_utc.timestamp()
    os.utime(path, (ts, ts))


def run(logdir: Path, cutoff: datetime) -> dict:
    out = subprocess.run(
        [sys.executable, str(TOOL), str(logdir), "--cutoff-ms", str(int(cutoff.timestamp() * 1000))],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def test_rebuilds_days_across_midnight_joins_models_and_stops_at_cutoff(tmp_path):
    # Host clock UTC-7. 23:50 local on Sep 1 = 06:50 UTC Sep 2 = 14:50 UTC+8 Sep 2;
    # 00:10 local on Sep 2 = 07:10 UTC = 15:10 UTC+8 Sep 2; 17:30 local Sep 2 = 00:30 UTC Sep 3
    # = 08:30 UTC+8 Sep 3, which is after the cutoff below and must be dropped.
    write(tmp_path / "codex-bridge.err.log", [
        "23:49:58 [INFO] -> requested=gpt-6-sol served=gpt-6-sol stream=True msgs=2 tools=1 cache_key=client:aaaaaaaa",
        "23:50:00 [INFO] <- served by account=pro-1 model=gpt-6-sol key=aaaaaaaa attempt=0",
        "00:09:00 [INFO] <- served by account=pro-1 model=gpt-6-luna key=bbbbbbbb attempt=0",
        "00:12:00 [INFO] <- [images/edits] account=pro-1 images=1 size=1024x1024 quality=low in_image_tokens=1500 out_image_tokens=200 12.0s",
        "00:13:00 [INFO] <- [responses] served by account=pro-1 model=gpt-6-sol key=cccccccc attempt=0",
        "17:30:00 [INFO] <- served by account=pro-1 model=gpt-6-sol key=dddddddd attempt=0",
    ], datetime(2026, 9, 3, 0, 30, 5, tzinfo=timezone.utc))
    write(tmp_path / "cx2cc.err.log", [
        "23:50:30 [INFO] <- stream in=1000 cached=900 out=10 | msgs=3 key=aaaaaaaa tools=1 h[t:1]",
        "00:10:00 [INFO] <- end_turn | in=500 cached=0 out=5 | msgs=1 key=bbbbbbbb tools=0 h[t:2]",
        "00:11:00 [ERROR] Upstream request failed with status 500",
        "17:30:30 [INFO] <- stream in=7 cached=0 out=1 | msgs=1 key=dddddddd tools=0 h[t:3]",
    ], datetime(2026, 9, 3, 0, 30, 30, tzinfo=timezone.utc))
    data = run(tmp_path, datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc))
    rows = {(r["day"], r["model"]): r for r in data["rows"]}
    assert set(rows) == {("2026-09-02", "gpt-6-sol"), ("2026-09-02", "gpt-6-luna"), ("2026-09-02", "gpt-image-2")}
    sol = rows[("2026-09-02", "gpt-6-sol")]
    # stream call + responses call (no tokens) + the 500 counted on the day's majority model
    assert (sol["requests"], sol["errors"], sol["input_tokens"], sol["cached_tokens"], sol["output_tokens"]) == (3, 1, 1000, 900, 10)
    assert rows[("2026-09-02", "gpt-6-luna")]["input_tokens"] == 500
    assert rows[("2026-09-02", "gpt-image-2")]["output_tokens"] == 200
    assert data["meta"]["first_day"] == data["meta"]["last_day"] == "2026-09-02"
