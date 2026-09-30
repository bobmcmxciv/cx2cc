"""Process-wide state shared by the proxy and the console."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

import aiohttp

from .config import Config
from .db import Database
from .keys import KeyStore
from .limits import Limiter

log = logging.getLogger("cx2cc_gateway")


@dataclass
class Settings:
    w_uncached: float = 1.0
    w_cached: float = 0.1
    w_output: float = 8.0
    retention_days: int = 180
    tz_offset_minutes: int = 480

    @classmethod
    def from_db(cls, raw: dict[str, str]) -> "Settings":
        return cls(
            w_uncached=float(raw.get("weight_uncached", 1)),
            w_cached=float(raw.get("weight_cached", 0.1)),
            w_output=float(raw.get("weight_output", 8)),
            retention_days=int(raw.get("retention_days", 180)),
            tz_offset_minutes=int(raw.get("tz_offset_minutes", 480)),
        )


@dataclass
class Catalog:
    """cx2cc's model list, including the aliases it resolves."""

    aliases: dict = field(default_factory=dict)
    default_model: str | None = None
    payload: dict | None = None
    fetched_at: float = 0.0


class Recorder:
    """Writes request audit rows on a background thread so no request ever
    waits on the disk."""

    def __init__(self, db: Database, settings_getter):
        self._db = db
        self._settings = settings_getter
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="audit-writer", daemon=True)
        self._thread.start()

    def submit(self, row: dict) -> None:
        log.info(
            "%s %s %s %s -> %s %s in=%s cached=%s out=%s w=%s %sms%s",
            row["rid"], row.get("alias") or "-", row["route"], row.get("model_requested") or "-",
            row["status"], row.get("model_served") or "-", row["input_tokens"], row["cached_tokens"],
            row["output_tokens"], row["weighted"], row.get("duration_ms"),
            f" error={row['error']}" if row.get("error") else "",
        )
        self._q.put(row)

    def flush(self, timeout: float = 5.0) -> None:
        done = threading.Event()
        self._q.put(done)
        done.wait(timeout)

    def stop(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=10)

    def _run(self) -> None:
        stop = False
        while not stop:
            item = self._q.get()
            batch: list[dict] = []
            events: list[threading.Event] = []
            while True:
                if item is None:
                    stop = True
                elif isinstance(item, threading.Event):
                    events.append(item)
                else:
                    batch.append(item)
                if stop or len(batch) >= 500:
                    break
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
            self._write(batch)
            for e in events:
                e.set()

    def _write(self, batch: list[dict]) -> None:
        if not batch:
            return
        for attempt in range(3):
            try:
                self._db.record_requests(batch, self._settings().tz_offset_minutes)
                return
            except Exception:
                log.exception("audit write failed (attempt %s, %s rows)", attempt + 1, len(batch))
                time.sleep(0.5 * (attempt + 1))


class Runtime:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.keys = KeyStore(db)
        self.keys.reload()
        self.limiter = Limiter()
        self.settings = Settings.from_db(db.settings())
        self.limiter.load(db, self.settings.tz_offset_minutes)
        self.catalog = Catalog()
        self.recorder = Recorder(db, lambda: self.settings)
        self.session: aiohttp.ClientSession | None = None
        self.started_at = time.time()
        # Failed console logins per client IP: [timestamps].
        self.login_failures: dict[str, list[float]] = {}

    def reload_settings(self) -> None:
        self.settings = Settings.from_db(self.db.settings())
