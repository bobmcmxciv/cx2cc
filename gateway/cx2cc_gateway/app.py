"""aiohttp application: wiring, background upkeep, entry point."""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from aiohttp import web

from . import __version__
from .admin import Console, console_middleware
from .config import Config
from .db import Database
from .proxy import Proxy
from .runtime import Runtime

log = logging.getLogger("cx2cc_gateway")

RUNTIME = web.AppKey("runtime", Runtime)
CONSOLE = web.AppKey("console", Console)


async def _refresh_catalog(rt: Runtime) -> None:
    try:
        async with rt.session.get(
            rt.cfg.upstream + "/v1/models", timeout=aiohttp.ClientTimeout(total=20)
        ) as r:
            if r.status != 200:
                return
            payload = await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        log.warning("model catalog refresh failed: %s", type(exc).__name__)
        return
    if isinstance(payload, dict):
        rt.catalog.payload = payload
        rt.catalog.aliases = {str(k).lower(): str(v).lower() for k, v in (payload.get("aliases") or {}).items()}
        rt.catalog.default_model = payload.get("default_model")
        rt.catalog.fetched_at = time.time()


async def _check_health(rt: Runtime, console: Console) -> None:
    try:
        async with rt.session.get(rt.cfg.upstream + "/health", timeout=aiohttp.ClientTimeout(total=10)) as r:
            console.note_health(r.status == 200)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        console.note_health(False)


async def _upkeep(app: web.Application) -> None:
    rt = app[RUNTIME]
    console = app[CONSOLE]
    tick = 0
    while True:
        try:
            # Keys can also change from the CLI in another process.
            await asyncio.to_thread(rt.keys.reload)
            if tick % 6 == 0:
                await asyncio.to_thread(rt.reload_settings)
                await asyncio.to_thread(rt.limiter.load, rt.db, rt.settings.tz_offset_minutes)
                await _check_health(rt, console)
            if tick % 120 == 0:
                await _refresh_catalog(rt)
            if tick % 8640 == 0:
                removed = await asyncio.to_thread(rt.db.purge, rt.settings.retention_days)
                if removed:
                    log.info("purged %s request rows older than %s days", removed, rt.settings.retention_days)
        except Exception:
            log.exception("upkeep iteration failed")
        tick += 1
        await asyncio.sleep(5)


async def _on_startup(app: web.Application) -> None:
    rt = app[RUNTIME]
    rt.session = aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=1024, keepalive_timeout=30),
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=rt.cfg.upstream_read_timeout),
        auto_decompress=False,
    )
    app["upkeep"] = asyncio.create_task(_upkeep(app))


async def _on_cleanup(app: web.Application) -> None:
    rt = app[RUNTIME]
    task = app.get("upkeep")
    if task:
        task.cancel()
    if rt.session:
        await rt.session.close()
    rt.recorder.flush()
    rt.recorder.stop()


def build_app(cfg: Config, db: Database | None = None) -> web.Application:
    if not cfg.upstream_token:
        raise SystemExit("CX2CC_GW_UPSTREAM_TOKEN is required")
    db = db or Database(cfg.db_path)
    rt = Runtime(cfg, db)
    console = Console(rt)
    proxy = Proxy(rt)

    app = web.Application(client_max_size=cfg.max_body_mb * 1024 * 1024, middlewares=[console_middleware])
    app[RUNTIME] = rt
    app[CONSOLE] = console

    async def healthz(request):
        return web.json_response({
            "status": "ok",
            "version": __version__,
            "uptime_s": int(time.time() - rt.started_at),
            "keys": len(rt.keys._by_hash),
        })

    app.router.add_get("/healthz", healthz)
    app.router.add_routes(console.routes())
    prefix = cfg.api_prefix or ""
    app.router.add_route("*", prefix + "/{tail:.*}", proxy.handle)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def serve(cfg: Config) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    app = build_app(cfg)
    log.info("cx2cc-gateway %s on %s:%s -> %s (api prefix %r)", __version__, cfg.listen_host,
             cfg.listen_port, cfg.upstream, cfg.api_prefix)
    web.run_app(app, host=cfg.listen_host, port=cfg.listen_port, access_log=None,
                handle_signals=True, shutdown_timeout=30)
