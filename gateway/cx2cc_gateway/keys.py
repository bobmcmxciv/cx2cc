"""In-memory view of the api_keys table, used on every proxied request."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .db import Database, now_ms
from .security import hash_api_key


@dataclass(frozen=True)
class KeyRecord:
    id: int
    alias: str
    owner: str
    scopes: frozenset
    allowed_models: tuple
    rpm_limit: int | None
    concurrency_limit: int | None
    daily_limit: int | None
    weekly_limit: int | None
    status: str
    expires_at: int | None

    @classmethod
    def from_row(cls, row: dict) -> "KeyRecord":
        return cls(
            id=row["id"],
            alias=row["alias"],
            owner=row["owner"],
            scopes=frozenset(split_list(row["scopes"])),
            allowed_models=tuple(m.lower() for m in split_list(row["allowed_models"])),
            rpm_limit=row["rpm_limit"] or None,
            concurrency_limit=row["concurrency_limit"] or None,
            daily_limit=row["daily_limit"] or None,
            weekly_limit=row["weekly_limit"] or None,
            status=row["status"],
            expires_at=row["expires_at"],
        )


def split_list(raw: str | None) -> list[str]:
    return [p.strip() for p in re.split(r"[,\s]+", raw or "") if p.strip()]


_WINDOW_SUFFIX = re.compile(r"\[[^\]]*\]$")


def normalize_model(name: str) -> str:
    return _WINDOW_SUFFIX.sub("", name.strip()).lower()


class KeyStore:
    def __init__(self, db: Database):
        self.db = db
        self._by_hash: dict[str, KeyRecord] = {}
        self._grace: dict[str, tuple[KeyRecord, int]] = {}

    def reload(self) -> None:
        by_hash: dict[str, KeyRecord] = {}
        grace: dict[str, tuple[KeyRecord, int]] = {}
        for row in self.db.all("SELECT * FROM api_keys"):
            rec = KeyRecord.from_row(row)
            by_hash[row["key_hash"]] = rec
            if row["prev_key_hash"] and row["prev_key_expires_at"]:
                grace[row["prev_key_hash"]] = (rec, row["prev_key_expires_at"])
        self._by_hash = by_hash
        self._grace = grace

    def authenticate(self, secret: str) -> tuple[KeyRecord | None, str | None]:
        """(record, problem). A record with a problem is a known key that may not
        be used right now; the caller still attributes the attempt to it."""
        h = hash_api_key(secret)
        now = now_ms()
        rec = self._by_hash.get(h)
        if rec is None:
            entry = self._grace.get(h)
            if entry is None:
                return None, "invalid_key"
            rec, until = entry
            if until <= now:
                return rec, "key_rotated"
        if rec.status != "active":
            return rec, f"key_{rec.status}"
        if rec.expires_at and rec.expires_at <= now:
            return rec, "key_expired"
        return rec, None


_CLAUDE_FAMILY = frozenset({"opus", "sonnet", "haiku", "fable", "opusplan", "default", "auto"})


def _is_claude_family(name: str) -> bool:
    return name.startswith("claude") or name in _CLAUDE_FAMILY


def model_allowed(rec: KeyRecord, requested: str | None, aliases: dict, default_model: str | None) -> bool:
    """A key restricted to some models may use them by name or by any alias
    cx2cc resolves to them; omitting the model means cx2cc's default."""
    if not rec.allowed_models:
        return True
    name = normalize_model(requested) if isinstance(requested, str) and requested.strip() else ""
    if not name or _is_claude_family(name):
        # cx2cc serves Claude Code's own model names with its default model.
        return bool(default_model) and default_model.lower() in rec.allowed_models
    served = str(aliases.get(name, name)).lower()
    return name in rec.allowed_models or served in rec.allowed_models
