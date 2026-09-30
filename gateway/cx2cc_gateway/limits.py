"""Per-key request rate, concurrency and weighted-token quotas.

All state lives in the event loop thread, so no locking is needed. Quota
totals are reloaded from usage_daily periodically and advanced in between as
requests finish; they are soft limits (a request that starts under the limit
is allowed to finish over it).
"""
from __future__ import annotations

import math
import time
from collections import defaultdict, deque

from .db import Database, day_of, day_start_ms, now_ms
from .keys import KeyRecord


class Limiter:
    def __init__(self) -> None:
        self.inflight: dict[int, int] = defaultdict(int)
        self._windows: dict[int, deque] = defaultdict(deque)
        self._today: dict[int, int] = {}
        self._week: dict[int, int] = {}
        self._day = ""

    def check(self, rec: KeyRecord, tz_offset_minutes: int) -> tuple[str, int] | None:
        """(reason, retry_after_seconds) when the key must wait, else None."""
        if rec.concurrency_limit and self.inflight[rec.id] >= rec.concurrency_limit:
            return "concurrency_limit", 2
        if rec.rpm_limit:
            now = time.monotonic()
            window = self._windows[rec.id]
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= rec.rpm_limit:
                return "rpm_limit", max(1, math.ceil(60 - (now - window[0])))
        self._roll(tz_offset_minutes)
        if rec.daily_limit and self._today.get(rec.id, 0) >= rec.daily_limit:
            wait = (day_start_ms(1, tz_offset_minutes) - now_ms()) // 1000
            return "daily_quota", max(60, int(wait))
        if rec.weekly_limit and self._week.get(rec.id, 0) >= rec.weekly_limit:
            wait = (day_start_ms(1, tz_offset_minutes) - now_ms()) // 1000
            return "weekly_quota", max(60, int(wait))
        return None

    def acquire(self, rec: KeyRecord) -> None:
        self.inflight[rec.id] += 1
        if rec.rpm_limit:
            self._windows[rec.id].append(time.monotonic())

    def release(self, rec: KeyRecord) -> None:
        self.inflight[rec.id] = max(0, self.inflight[rec.id] - 1)

    def add_usage(self, key_id: int, weighted: int, tz_offset_minutes: int) -> None:
        self._roll(tz_offset_minutes)
        self._today[key_id] = self._today.get(key_id, 0) + weighted
        self._week[key_id] = self._week.get(key_id, 0) + weighted

    def load(self, db: Database, tz_offset_minutes: int) -> None:
        today = day_of(now_ms(), tz_offset_minutes)
        week_start = day_of(day_start_ms(-6, tz_offset_minutes), tz_offset_minutes)
        rows = db.all(
            "SELECT key_id, SUM(CASE WHEN day = ? THEN weighted ELSE 0 END) AS today, "
            "SUM(weighted) AS week FROM usage_daily WHERE day >= ? GROUP BY key_id",
            (today, week_start),
        )
        self._today = {r["key_id"]: int(r["today"] or 0) for r in rows}
        self._week = {r["key_id"]: int(r["week"] or 0) for r in rows}
        self._day = today

    def _roll(self, tz_offset_minutes: int) -> None:
        today = day_of(now_ms(), tz_offset_minutes)
        if today != self._day:
            # Day boundary: the next load() will recompute the week properly;
            # until then only "today" is known to be zero.
            self._today = {}
            self._day = today
