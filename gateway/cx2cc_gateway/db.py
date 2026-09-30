"""SQLite storage: keys, console users, sessions, request audit, admin events.

All timestamps are epoch milliseconds (UTC). Day buckets are computed in the
display timezone (fixed offset, default UTC+8) so "today" means the same thing
on the console as it does for the people using the keys.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id              INTEGER PRIMARY KEY,
  username        TEXT NOT NULL UNIQUE COLLATE NOCASE,
  display_name    TEXT NOT NULL DEFAULT '',
  role            TEXT NOT NULL CHECK (role IN ('admin', 'operator', 'auditor')),
  password_hash   TEXT NOT NULL,
  disabled        INTEGER NOT NULL DEFAULT 0,
  created_at      INTEGER NOT NULL,
  created_by      INTEGER,
  last_login_at   INTEGER,
  password_changed_at INTEGER
);

CREATE TABLE IF NOT EXISTS api_keys (
  id                  INTEGER PRIMARY KEY,
  alias               TEXT NOT NULL UNIQUE COLLATE NOCASE,
  owner               TEXT NOT NULL DEFAULT '',
  note                TEXT NOT NULL DEFAULT '',
  key_hash            TEXT NOT NULL UNIQUE,
  key_prefix          TEXT NOT NULL,
  prev_key_hash       TEXT,
  prev_key_expires_at INTEGER,
  scopes              TEXT NOT NULL,
  allowed_models      TEXT NOT NULL DEFAULT '',
  rpm_limit           INTEGER,
  concurrency_limit   INTEGER,
  daily_limit         INTEGER,
  weekly_limit        INTEGER,
  status              TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'disabled', 'revoked')),
  expires_at          INTEGER,
  created_at          INTEGER NOT NULL,
  created_by          INTEGER,
  updated_at          INTEGER NOT NULL,
  revoked_at          INTEGER,
  last_used_at        INTEGER,
  last_used_ip        TEXT
);

CREATE TABLE IF NOT EXISTS requests (
  id              INTEGER PRIMARY KEY,
  rid             TEXT NOT NULL,
  ts              INTEGER NOT NULL,
  key_id          INTEGER,
  alias           TEXT,
  method          TEXT NOT NULL,
  path            TEXT NOT NULL,
  route           TEXT NOT NULL,
  model_requested TEXT,
  model_served    TEXT,
  stream          INTEGER NOT NULL DEFAULT 0,
  status          INTEGER NOT NULL,
  error           TEXT,
  input_tokens    INTEGER NOT NULL DEFAULT 0,
  cached_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens   INTEGER NOT NULL DEFAULT 0,
  weighted        INTEGER NOT NULL DEFAULT 0,
  req_bytes       INTEGER,
  resp_bytes      INTEGER,
  ttfb_ms         INTEGER,
  duration_ms     INTEGER,
  client_ip       TEXT,
  user_agent      TEXT,
  session_id      TEXT,
  key_fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS ix_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS ix_requests_key_ts ON requests(key_id, ts);

CREATE TABLE IF NOT EXISTS usage_daily (
  day           TEXT NOT NULL,
  key_id        INTEGER NOT NULL,
  model         TEXT NOT NULL,
  requests      INTEGER NOT NULL DEFAULT 0,
  errors        INTEGER NOT NULL DEFAULT 0,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  cached_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  weighted      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (day, key_id, model)
);

-- Usage from before the gateway existed (e.g. rebuilt from cx2cc logs): daily
-- aggregates only, replaced wholesale per source on re-import.
CREATE TABLE IF NOT EXISTS usage_history (
  source        TEXT NOT NULL,
  day           TEXT NOT NULL,
  key_id        INTEGER NOT NULL,
  model         TEXT NOT NULL,
  requests      INTEGER NOT NULL DEFAULT 0,
  errors        INTEGER NOT NULL DEFAULT 0,
  input_tokens  INTEGER NOT NULL DEFAULT 0,
  cached_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  weighted      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (source, day, key_id, model)
);

-- Every report reads this: live rollups plus imported history.
CREATE VIEW IF NOT EXISTS usage_all AS
  SELECT day, key_id, model, requests, errors, input_tokens, cached_tokens, output_tokens, weighted
    FROM usage_daily
  UNION ALL
  SELECT day, key_id, model, requests, errors, input_tokens, cached_tokens, output_tokens, weighted
    FROM usage_history;

CREATE TABLE IF NOT EXISTS audit_events (
  id          INTEGER PRIMARY KEY,
  ts          INTEGER NOT NULL,
  actor_type  TEXT NOT NULL,
  actor_id    INTEGER,
  actor_name  TEXT,
  action      TEXT NOT NULL,
  target_type TEXT,
  target_id   INTEGER,
  target_name TEXT,
  detail      TEXT,
  ip          TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS ix_events_target ON audit_events(target_type, target_id);

CREATE TABLE IF NOT EXISTS sessions (
  id          TEXT PRIMARY KEY,
  user_id     INTEGER,
  key_id      INTEGER,
  csrf        TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  expires_at  INTEGER NOT NULL,
  ip          TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    # Codex subscription quota weights, measured: uncached input 1, cached
    # input 0.1, output 8.
    "weight_uncached": "1",
    "weight_cached": "0.1",
    "weight_output": "8",
    "retention_days": "180",
    "tz_offset_minutes": "480",
}

SCOPES = ("chat", "images", "usage", "accounts")
DEFAULT_SCOPES = ("chat", "images", "usage")

KEY_FIELDS = (
    "id", "alias", "owner", "note", "key_prefix", "scopes", "allowed_models",
    "rpm_limit", "concurrency_limit", "daily_limit", "weekly_limit", "status",
    "expires_at", "created_at", "created_by", "updated_at", "revoked_at",
    "last_used_at", "last_used_ip", "prev_key_expires_at",
)


def now_ms() -> int:
    return int(time.time() * 1000)


def day_of(ts_ms: int, tz_offset_minutes: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000 + tz_offset_minutes * 60))


def day_start_ms(day_offset: int, tz_offset_minutes: int, ref_ms: int | None = None) -> int:
    """Start of the local day `day_offset` days from the one containing ref_ms."""
    ref = now_ms() if ref_ms is None else ref_ms
    off = tz_offset_minutes * 60_000
    local_midnight = ((ref + off) // 86_400_000) * 86_400_000
    return local_midnight - off + day_offset * 86_400_000


class Database:
    """Thread-local connections to one SQLite file in WAL mode."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = self.conn()
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (k, v) VALUES (?, ?)", (k, v))
        conn.commit()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    # -- generic -----------------------------------------------------------

    def one(self, sql: str, args: Iterable[Any] = ()) -> dict | None:
        row = self.conn().execute(sql, tuple(args)).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, args: Iterable[Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn().execute(sql, tuple(args)).fetchall()]

    def execute(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn().execute(sql, tuple(args))

    # -- settings ----------------------------------------------------------

    def settings(self) -> dict[str, str]:
        return {r["k"]: r["v"] for r in self.all("SELECT k, v FROM settings")}

    def set_setting(self, k: str, v: str) -> None:
        self.execute(
            "INSERT INTO settings (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            (k, v),
        )

    # -- audit events ------------------------------------------------------

    def event(
        self,
        action: str,
        *,
        actor_type: str,
        actor_id: int | None = None,
        actor_name: str | None = None,
        target_type: str | None = None,
        target_id: int | None = None,
        target_name: str | None = None,
        detail: dict | None = None,
        ip: str | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO audit_events (ts, actor_type, actor_id, actor_name, action, "
            "target_type, target_id, target_name, detail, ip) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                now_ms(), actor_type, actor_id, actor_name, action, target_type, target_id,
                target_name, json.dumps(detail, ensure_ascii=False) if detail else None, ip,
            ),
        )

    # -- request log -------------------------------------------------------

    def record_requests(self, rows: list[dict], tz_offset_minutes: int) -> None:
        """Insert request rows and fold them into usage_daily, one transaction."""
        if not rows:
            return
        c = self.conn()
        c.execute("BEGIN")
        try:
            c.executemany(
                "INSERT INTO requests (rid, ts, key_id, alias, method, path, route, "
                "model_requested, model_served, stream, status, error, input_tokens, "
                "cached_tokens, output_tokens, weighted, req_bytes, resp_bytes, ttfb_ms, "
                "duration_ms, client_ip, user_agent, session_id, key_fingerprint) VALUES "
                "(:rid, :ts, :key_id, :alias, :method, :path, :route, :model_requested, "
                ":model_served, :stream, :status, :error, :input_tokens, :cached_tokens, "
                ":output_tokens, :weighted, :req_bytes, :resp_bytes, :ttfb_ms, :duration_ms, "
                ":client_ip, :user_agent, :session_id, :key_fingerprint)",
                rows,
            )
            for r in rows:
                if r.get("key_id") is None:
                    continue
                model = r.get("model_served") or r.get("model_requested") or "-"
                c.execute(
                    "INSERT INTO usage_daily (day, key_id, model, requests, errors, input_tokens, "
                    "cached_tokens, output_tokens, weighted) VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(day, key_id, model) DO UPDATE SET "
                    "requests = requests + excluded.requests, errors = errors + excluded.errors, "
                    "input_tokens = input_tokens + excluded.input_tokens, "
                    "cached_tokens = cached_tokens + excluded.cached_tokens, "
                    "output_tokens = output_tokens + excluded.output_tokens, "
                    "weighted = weighted + excluded.weighted",
                    (
                        day_of(r["ts"], tz_offset_minutes), r["key_id"], model, 1,
                        1 if (r["status"] >= 400 or r.get("error")) else 0,
                        r["input_tokens"], r["cached_tokens"],
                        r["output_tokens"], r["weighted"],
                    ),
                )
                c.execute(
                    "UPDATE api_keys SET last_used_at = MAX(COALESCE(last_used_at, 0), ?), "
                    "last_used_ip = ? WHERE id = ?",
                    (r["ts"], r.get("client_ip"), r["key_id"]),
                )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def replace_history(self, source: str, key_id: int, rows: list[dict],
                        weights: tuple[float, float, float]) -> dict:
        """Replace every usage_history row of `source` with `rows`
        ({day, model, requests, errors, input_tokens, cached_tokens,
        output_tokens}); weighted tokens use the current weights."""
        w_uncached, w_cached, w_output = weights
        merged: dict[tuple[str, str], list[int]] = {}
        for r in rows:
            k = (str(r["day"]), str(r.get("model") or "-")[:80])
            acc = merged.setdefault(k, [0, 0, 0, 0, 0])
            for i, f in enumerate(("requests", "errors", "input_tokens", "cached_tokens", "output_tokens")):
                acc[i] += int(r.get(f) or 0)
        c = self.conn()
        c.execute("BEGIN")
        try:
            c.execute("DELETE FROM usage_history WHERE source = ?", (source,))
            for (day, model), (req, err, inp, cached, out) in merged.items():
                weighted = int(round((inp - cached) * w_uncached + cached * w_cached + out * w_output))
                c.execute(
                    "INSERT INTO usage_history (source, day, key_id, model, requests, errors, input_tokens, "
                    "cached_tokens, output_tokens, weighted) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (source, day, key_id, model, req, err, inp, cached, out, weighted),
                )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        days = sorted({d for d, _ in merged})
        return {"rows": len(merged), "first_day": days[0] if days else None,
                "last_day": days[-1] if days else None}

    def purge(self, retention_days: int) -> int:
        cutoff = now_ms() - retention_days * 86_400_000
        cur = self.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
        self.execute("DELETE FROM sessions WHERE expires_at < ?", (now_ms(),))
        return cur.rowcount
