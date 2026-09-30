"""Runtime configuration, read once from the environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    listen_host: str
    listen_port: int
    # cx2cc as seen from the gateway (on the reference deployment: the frps
    # listener on the ECS host that tunnels to cx2cc).
    upstream: str
    # The one credential cx2cc (and codex-bridge behind it) accepts. Clients
    # never see it; the gateway swaps their own key for it on every request.
    upstream_token: str
    data_dir: Path
    # Path prefix the reverse proxy keeps in front of API calls, e.g. "/api"
    # for https://cx2cc.example.com/api/v1/messages.
    api_prefix: str
    cookie_secure: bool
    # Trust X-Real-IP / X-Forwarded-For. Only safe when the gateway is reachable
    # solely through the reverse proxy that sets them.
    trust_proxy: bool
    session_hours: int
    upstream_read_timeout: int
    max_body_mb: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gateway.db"


def load() -> Config:
    prefix = "/" + os.environ.get("CX2CC_GW_API_PREFIX", "/api").strip().strip("/")
    return Config(
        listen_host=os.environ.get("CX2CC_GW_HOST", "0.0.0.0"),
        listen_port=_int("CX2CC_GW_PORT", 8000),
        upstream=os.environ.get("CX2CC_GW_UPSTREAM", "http://127.0.0.1:8901").rstrip("/"),
        upstream_token=os.environ.get("CX2CC_GW_UPSTREAM_TOKEN", "").strip(),
        data_dir=Path(os.environ.get("CX2CC_GW_DATA_DIR", "./data")),
        api_prefix="" if prefix == "/" else prefix,
        cookie_secure=_bool("CX2CC_GW_COOKIE_SECURE", True),
        trust_proxy=_bool("CX2CC_GW_TRUST_PROXY", True),
        session_hours=_int("CX2CC_GW_SESSION_HOURS", 12),
        upstream_read_timeout=_int("CX2CC_GW_UPSTREAM_READ_TIMEOUT", 900),
        max_body_mb=_int("CX2CC_GW_MAX_BODY_MB", 96),
    )
