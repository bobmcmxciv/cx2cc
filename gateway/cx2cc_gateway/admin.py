"""Management console: JSON API behind /admin/api, plus the static UI.

Two kinds of sign-in share one session table:

* console users (admin / operator / auditor) with a username and password;
* key holders, who sign in with their own API key and only ever see that key.

What each role may do is the ROLE_PERMS table below; every change goes to
audit_events together with who made it and from where.
"""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiohttp import web

from .db import DEFAULT_SCOPES, KEY_FIELDS, SCOPES, day_of, day_start_ms, now_ms
from .keys import normalize_model, split_list
from .proxy import client_ip
from .runtime import Runtime
from .security import (
    generate_api_key,
    hash_api_key,
    hash_password,
    key_display_prefix,
    new_token,
    password_problem,
    verify_password,
)

log = logging.getLogger("cx2cc_gateway.admin")

COOKIE = "cx2cc_console"
STATIC_DIR = Path(__file__).resolve().parent / "static"

ROLES = ("admin", "operator", "auditor")
ROLE_PERMS = {
    "admin": frozenset({
        "keys.view_all", "keys.create", "keys.manage_all", "keys.grant_accounts",
        "audit.view_all", "users.manage", "settings.manage", "upstream.view",
    }),
    "operator": frozenset({"keys.create", "keys.manage_own"}),
    "auditor": frozenset({"keys.view_all", "audit.view_all", "upstream.view"}),
    "key": frozenset(),
}

# Explicit, so a host whose mimetypes registry maps .js to text/plain (seen on
# Windows) cannot make the browser refuse the scripts under nosniff.
_CONTENT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

LOGIN_WINDOW_S = 15 * 60
LOGIN_MAX_FAILURES = 10
MAX_PAGE = 200
CSV_MAX_ROWS = 50_000


@dataclass
class Principal:
    kind: str               # "user" or "key"
    role: str               # admin / operator / auditor / key
    id: int                 # users.id or api_keys.id
    name: str
    csrf: str
    session_hash: str

    def can(self, perm: str) -> bool:
        return perm in ROLE_PERMS.get(self.role, frozenset())

    def public(self) -> dict:
        return {
            "kind": self.kind,
            "role": self.role,
            "id": self.id,
            "name": self.name,
            "csrf": self.csrf,
            "permissions": sorted(ROLE_PERMS.get(self.role, ())),
        }


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _session_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# -- visibility ---------------------------------------------------------------

def key_scope_sql(p: Principal, column: str = "key_id") -> tuple[str, list]:
    """SQL condition limiting rows to the keys this principal may see."""
    if p.can("keys.view_all"):
        return "1=1", []
    if p.kind == "key":
        return f"{column} = ?", [p.id]
    return f"{column} IN (SELECT id FROM api_keys WHERE created_by = ?)", [p.id]


def can_manage_key(p: Principal, key_row: dict) -> bool:
    if p.can("keys.manage_all"):
        return True
    return p.can("keys.manage_own") and key_row.get("created_by") == p.id


def key_public(row: dict) -> dict:
    out = {k: row.get(k) for k in KEY_FIELDS}
    out["scopes"] = split_list(row.get("scopes"))
    out["allowed_models"] = split_list(row.get("allowed_models"))
    grace = row.get("prev_key_expires_at")
    out["rotation_grace_until"] = grace if grace and grace > now_ms() else None
    out.pop("prev_key_expires_at", None)
    if "created_by_name" in row:
        out["created_by_name"] = row["created_by_name"]
    return out


def user_public(row: dict) -> dict:
    return {
        k: row.get(k)
        for k in ("id", "username", "display_name", "role", "disabled", "created_at",
                  "created_by", "last_login_at", "password_changed_at")
    }


# -- validation -------------------------------------------------------------

def _opt_int(value, name: str, maximum: int = 10**15) -> int | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} 必须是整数")
    if n < 0 or n > maximum:
        raise ApiError(400, f"{name} 超出范围")
    return n or None


def _text(value, name: str, max_len: int, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ApiError(400, f"{name} 不能为空")
    if len(text) > max_len:
        raise ApiError(400, f"{name} 不能超过 {max_len} 个字符")
    return text


_ALIAS_RE = re.compile(r"^[^\s/\\<>\"'`]{1,64}$")


def _alias(value) -> str:
    alias = _text(value, "别名", 64, required=True)
    if not _ALIAS_RE.match(alias):
        raise ApiError(400, "别名不能包含空白、斜杠、引号或尖括号")
    return alias


def _scopes(value, p: Principal) -> str:
    items = value if isinstance(value, list) else split_list(value)
    scopes = [s for s in SCOPES if s in items]
    unknown = [s for s in items if s not in SCOPES]
    if unknown:
        raise ApiError(400, f"未知权限范围：{', '.join(unknown)}")
    if not scopes:
        raise ApiError(400, "至少选择一个权限范围")
    if "accounts" in scopes and not p.can("keys.grant_accounts"):
        raise ApiError(403, "只有管理员可以授予账号池（accounts）范围")
    return ",".join(scopes)


def _models(value) -> str:
    items = value if isinstance(value, list) else split_list(value)
    models = []
    for m in items:
        m = normalize_model(str(m))
        if m and m not in models:
            models.append(m)
    if len(models) > 30:
        raise ApiError(400, "模型白名单最多 30 项")
    return ",".join(models)


def _expires(body: dict) -> int | None:
    if body.get("expires_in_days") not in (None, "", 0, "0"):
        days = _opt_int(body.get("expires_in_days"), "有效期天数", 3650)
        return now_ms() + days * 86_400_000
    return _opt_int(body.get("expires_at"), "过期时间")


# -- the console ----------------------------------------------------------------

class Console:
    def __init__(self, rt: Runtime):
        self.rt = rt
        self.db = rt.db
        self._upstream_cache: dict[str, tuple[float, object]] = {}

    def routes(self) -> list[web.RouteDef]:
        r = web
        return [
            r.get("/admin", self.redirect_root),
            r.get("/admin/", self.index),
            r.get("/admin/static/{name}", self.static),
            r.post("/admin/api/login", self.login),
            r.post("/admin/api/login-key", self.login_key),
            r.post("/admin/api/logout", self.logout),
            r.get("/admin/api/me", self.me),
            r.post("/admin/api/password", self.change_password),
            r.get("/admin/api/overview", self.overview),
            r.get("/admin/api/keys", self.list_keys),
            r.post("/admin/api/keys", self.create_key),
            r.get("/admin/api/keys/{id:\\d+}", self.get_key),
            r.patch("/admin/api/keys/{id:\\d+}", self.update_key),
            r.post("/admin/api/keys/{id:\\d+}/{action:disable|enable|revoke|rotate}", self.key_action),
            r.get("/admin/api/requests", self.list_requests),
            r.get("/admin/api/requests.csv", self.export_requests),
            r.get("/admin/api/events", self.list_events),
            r.get("/admin/api/users", self.list_users),
            r.post("/admin/api/users", self.create_user),
            r.patch("/admin/api/users/{id:\\d+}", self.update_user),
            r.get("/admin/api/settings", self.get_settings),
            r.patch("/admin/api/settings", self.update_settings),
            r.get("/admin/api/upstream", self.upstream),
        ]

    # -- plumbing -----------------------------------------------------------

    async def run(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def principal(self, request: web.Request, required: bool = True) -> Principal | None:
        token = request.cookies.get(COOKIE)
        p = await self.run(self._principal_sync, token) if token else None
        if p is None and required:
            raise ApiError(401, "请先登录")
        if p is not None and request.method not in ("GET", "HEAD"):
            if request.headers.get("X-CSRF-Token") != p.csrf:
                raise ApiError(403, "CSRF 校验失败，请刷新页面")
        return p

    def _principal_sync(self, token: str) -> Principal | None:
        sh = _session_hash(token)
        s = self.db.one("SELECT * FROM sessions WHERE id = ? AND expires_at > ?", (sh, now_ms()))
        if not s:
            return None
        if s["user_id"]:
            u = self.db.one("SELECT * FROM users WHERE id = ? AND disabled = 0", (s["user_id"],))
            if not u:
                return None
            return Principal("user", u["role"], u["id"], u["display_name"] or u["username"], s["csrf"], sh)
        k = self.db.one("SELECT * FROM api_keys WHERE id = ?", (s["key_id"],))
        if not k or k["status"] == "revoked":
            return None
        return Principal("key", "key", k["id"], k["alias"], s["csrf"], sh)

    def _new_session(self, user_id: int | None, key_id: int | None, ip: str) -> tuple[str, str]:
        token = new_token()
        csrf = new_token()
        now = now_ms()
        self.db.execute(
            "INSERT INTO sessions (id, user_id, key_id, csrf, created_at, expires_at, ip) "
            "VALUES (?,?,?,?,?,?,?)",
            (_session_hash(token), user_id, key_id, csrf, now,
             now + self.rt.cfg.session_hours * 3_600_000, ip),
        )
        return token, csrf

    def _set_cookie(self, resp: web.Response, token: str) -> None:
        resp.set_cookie(
            COOKIE, token, path="/admin", httponly=True, secure=self.rt.cfg.cookie_secure,
            samesite="Strict", max_age=self.rt.cfg.session_hours * 3600,
        )

    def _ip(self, request: web.Request) -> str:
        return client_ip(request, self.rt.cfg.trust_proxy)

    async def _json(self, request: web.Request) -> dict:
        if "json" not in request.headers.get("Content-Type", ""):
            raise ApiError(415, "需要 application/json")
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "请求体不是合法 JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return body

    def _event(self, p: Principal | None, action: str, request: web.Request, **kw) -> None:
        self.db.event(
            action,
            actor_type=p.kind if p else "anonymous",
            actor_id=p.id if p else None,
            actor_name=p.name if p else kw.pop("actor_name", None),
            ip=self._ip(request),
            **kw,
        )

    def _throttled(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self.rt.login_failures.get(ip, []) if t > now - LOGIN_WINDOW_S]
        self.rt.login_failures[ip] = recent
        return len(recent) >= LOGIN_MAX_FAILURES

    def _failed(self, ip: str) -> None:
        self.rt.login_failures.setdefault(ip, []).append(time.time())

    # -- static ---------------------------------------------------------------

    async def redirect_root(self, request):
        raise web.HTTPMovedPermanently("/admin/")

    async def index(self, request):
        return web.FileResponse(
            STATIC_DIR / "index.html",
            headers={"Cache-Control": "no-cache", "Content-Type": "text/html; charset=utf-8"},
        )

    async def static(self, request):
        name = request.match_info["name"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name.startswith("."):
            raise web.HTTPNotFound()
        path = STATIC_DIR / name
        if not path.is_file():
            raise web.HTTPNotFound()
        ctype = _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
        return web.FileResponse(path, headers={"Cache-Control": "no-cache", "Content-Type": ctype})

    # -- authentication -----------------------------------------------------

    async def login(self, request):
        ip = self._ip(request)
        if self._throttled(ip):
            raise ApiError(429, "失败次数过多，请 15 分钟后再试")
        body = await self._json(request)
        username = _text(body.get("username"), "用户名", 64, required=True)
        password = str(body.get("password") or "")

        def check():
            u = self.db.one("SELECT * FROM users WHERE username = ?", (username,))
            ok = bool(u) and not u["disabled"] and verify_password(password, u["password_hash"])
            if not ok:
                self.db.event("login.failed", actor_type="anonymous", actor_name=username[:64], ip=ip)
                return None
            self.db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now_ms(), u["id"]))
            token, csrf = self._new_session(u["id"], None, ip)
            self.db.event("login", actor_type="user", actor_id=u["id"], actor_name=u["username"], ip=ip)
            return u, token, csrf

        result = await self.run(check)
        if result is None:
            self._failed(ip)
            raise ApiError(401, "用户名或密码错误")
        u, token, csrf = result
        p = Principal("user", u["role"], u["id"], u["display_name"] or u["username"], csrf, "")
        resp = web.json_response({"me": p.public()})
        self._set_cookie(resp, token)
        return resp

    async def login_key(self, request):
        ip = self._ip(request)
        if self._throttled(ip):
            raise ApiError(429, "失败次数过多，请 15 分钟后再试")
        body = await self._json(request)
        secret = str(body.get("key") or "").strip()
        rec, problem = self.rt.keys.authenticate(secret) if secret else (None, "invalid_key")
        if rec is None or problem:
            self._failed(ip)
            await self.run(lambda: self.db.event(
                "portal.login_failed", actor_type="anonymous", ip=ip,
                target_type="key" if rec else None, target_id=rec.id if rec else None,
                target_name=rec.alias if rec else None, detail={"reason": problem},
            ))
            raise ApiError(401, "API Key 无效或已停用")

        def start():
            token, csrf = self._new_session(None, rec.id, ip)
            self.db.event("portal.login", actor_type="key", actor_id=rec.id, actor_name=rec.alias,
                          target_type="key", target_id=rec.id, target_name=rec.alias, ip=ip)
            return token, csrf

        token, csrf = await self.run(start)
        p = Principal("key", "key", rec.id, rec.alias, csrf, "")
        resp = web.json_response({"me": p.public()})
        self._set_cookie(resp, token)
        return resp

    async def logout(self, request):
        p = await self.principal(request, required=False)
        if p:
            await self.run(self.db.execute, "DELETE FROM sessions WHERE id = ?", (p.session_hash,))
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE, path="/admin")
        return resp

    async def me(self, request):
        p = await self.principal(request, required=False)
        return web.json_response({"me": p.public() if p else None})

    async def change_password(self, request):
        p = await self.principal(request)
        if p.kind != "user":
            raise ApiError(403, "只有控制台账号可以修改密码")
        body = await self._json(request)
        new = str(body.get("new_password") or "")
        problem = password_problem(new)
        if problem:
            raise ApiError(400, problem)

        def change():
            u = self.db.one("SELECT * FROM users WHERE id = ?", (p.id,))
            if not verify_password(str(body.get("old_password") or ""), u["password_hash"]):
                return False
            self.db.execute(
                "UPDATE users SET password_hash = ?, password_changed_at = ? WHERE id = ?",
                (hash_password(new), now_ms(), p.id),
            )
            self.db.execute("DELETE FROM sessions WHERE user_id = ? AND id != ?", (p.id, p.session_hash))
            self._event(p, "user.password_changed", request, target_type="user", target_id=p.id,
                        target_name=u["username"])
            return True

        if not await self.run(change):
            raise ApiError(400, "原密码不正确")
        return web.json_response({"ok": True})

    # -- overview -------------------------------------------------------------

    async def overview(self, request):
        p = await self.principal(request)
        days = min(max(int(request.query.get("days", 30)), 7), 90)
        return web.json_response(await self.run(self._overview, p, days))

    def _overview(self, p: Principal, days: int) -> dict:
        tz = self.rt.settings.tz_offset_minutes
        now = now_ms()
        today = day_of(now, tz)
        d7 = day_of(day_start_ms(-6, tz), tz)
        d30 = day_of(day_start_ms(-29, tz), tz)
        dn = day_of(day_start_ms(-(days - 1), tz), tz)
        cond, args = key_scope_sql(p)
        ucond, _ = key_scope_sql(p, "u.key_id")

        def totals(since: str) -> dict:
            row = self.db.one(
                "SELECT COALESCE(SUM(requests),0) AS requests, COALESCE(SUM(errors),0) AS errors, "
                "COALESCE(SUM(input_tokens),0) AS input_tokens, COALESCE(SUM(cached_tokens),0) AS cached_tokens, "
                "COALESCE(SUM(output_tokens),0) AS output_tokens, COALESCE(SUM(weighted),0) AS weighted "
                f"FROM usage_daily WHERE day >= ? AND {cond}",
                [since, *args],
            )
            return row

        per_day_key = self.db.all(
            "SELECT u.day, u.key_id, k.alias, SUM(u.weighted) AS weighted, SUM(u.requests) AS requests "
            f"FROM usage_daily u LEFT JOIN api_keys k ON k.id = u.key_id WHERE u.day >= ? AND {ucond} "
            "GROUP BY u.day, u.key_id ORDER BY u.day",
            [dn, *args],
        )
        by_key = self.db.all(
            "SELECT u.key_id, k.alias, k.owner, k.status, k.last_used_at, SUM(u.weighted) AS weighted, "
            "SUM(u.requests) AS requests, SUM(u.errors) AS errors, SUM(u.input_tokens) AS input_tokens, "
            "SUM(u.cached_tokens) AS cached_tokens, SUM(u.output_tokens) AS output_tokens "
            f"FROM usage_daily u LEFT JOIN api_keys k ON k.id = u.key_id WHERE u.day >= ? AND {ucond} "
            "GROUP BY u.key_id ORDER BY weighted DESC",
            [d7, *args],
        )
        by_owner = self.db.all(
            "SELECT COALESCE(NULLIF(k.owner, ''), '（未填写）') AS owner, COUNT(DISTINCT u.key_id) AS keys, "
            "SUM(u.weighted) AS weighted, SUM(u.requests) AS requests "
            f"FROM usage_daily u LEFT JOIN api_keys k ON k.id = u.key_id WHERE u.day >= ? AND {ucond} "
            "GROUP BY 1 ORDER BY weighted DESC",
            [d7, *args],
        )
        by_model = self.db.all(
            "SELECT model, SUM(requests) AS requests, SUM(weighted) AS weighted "
            f"FROM usage_daily WHERE day >= ? AND {cond} GROUP BY model ORDER BY weighted DESC",
            [d7, *args],
        )
        # Chart colours follow the key, so they are assigned from a window that
        # does not move with the range filter.
        top_keys = [r["key_id"] for r in self.db.all(
            f"SELECT key_id FROM usage_daily WHERE day >= ? AND {cond} GROUP BY key_id "
            "ORDER BY SUM(weighted) DESC LIMIT 5",
            [d30, *args],
        )]
        kcond, kargs = key_scope_sql(p, "id")
        counts = self.db.all(f"SELECT status, COUNT(*) AS n FROM api_keys WHERE {kcond} GROUP BY status", kargs)
        active_24h = self.db.one(
            f"SELECT COUNT(*) AS n FROM api_keys WHERE {kcond} AND last_used_at >= ?", [*kargs, now - 86_400_000]
        )["n"]
        rcond, rargs = self._request_scope(p)
        recent_errors = self.db.all(
            "SELECT id, rid, ts, alias, route, model_requested, status, error, client_ip "
            f"FROM requests WHERE ({rcond}) AND (status >= 400 OR error IS NOT NULL) "
            "AND ts >= ? ORDER BY id DESC LIMIT 10",
            [*rargs, now - 7 * 86_400_000],
        )
        return {
            "tz_offset_minutes": tz,
            "days": days,
            "today": today,
            "totals": {"today": totals(today), "7d": totals(d7), "30d": totals(d30)},
            "per_day_key": per_day_key,
            "top_keys_30d": top_keys,
            "by_key": by_key,
            "by_owner": by_owner,
            "by_model": by_model,
            "key_counts": {r["status"]: r["n"] for r in counts},
            "active_24h": active_24h,
            "recent_errors": recent_errors,
            "upstream_ok": self._upstream_cache.get("health", (0, None))[1],
        }

    def _request_scope(self, p: Principal) -> tuple[str, list]:
        if p.can("audit.view_all"):
            return "1=1", []
        return key_scope_sql(p)

    # -- keys -------------------------------------------------------------------

    async def list_keys(self, request):
        p = await self.principal(request)
        q = request.query.get("q", "").strip()
        status = request.query.get("status", "").strip()
        return web.json_response({"keys": await self.run(self._list_keys, p, q, status)})

    def _list_keys(self, p: Principal, q: str, status: str) -> list[dict]:
        tz = self.rt.settings.tz_offset_minutes
        cond, args = key_scope_sql(p, "k.id")
        where = [cond]
        if q:
            where.append("(k.alias LIKE ? OR k.owner LIKE ? OR k.note LIKE ? OR k.key_prefix LIKE ?)")
            args += [f"%{q}%"] * 4
        if status in ("active", "disabled", "revoked"):
            where.append("k.status = ?")
            args.append(status)
        rows = self.db.all(
            "SELECT k.*, u.username AS created_by_name FROM api_keys k LEFT JOIN users u ON u.id = k.created_by "
            f"WHERE {' AND '.join(where)} ORDER BY k.status = 'revoked', k.alias COLLATE NOCASE",
            args,
        )
        stats = self._key_stats(tz)
        out = []
        for r in rows:
            item = key_public(r)
            item["usage"] = stats.get(r["id"], {})
            item["can_manage"] = can_manage_key(p, r)
            out.append(item)
        return out

    def _key_stats(self, tz: int, key_id: int | None = None) -> dict[int, dict]:
        today = day_of(now_ms(), tz)
        d7 = day_of(day_start_ms(-6, tz), tz)
        d30 = day_of(day_start_ms(-29, tz), tz)
        extra, args = ("AND key_id = ?", [key_id]) if key_id is not None else ("", [])
        rows = self.db.all(
            "SELECT key_id, "
            "SUM(CASE WHEN day = ? THEN weighted ELSE 0 END) AS weighted_today, "
            "SUM(CASE WHEN day = ? THEN requests ELSE 0 END) AS requests_today, "
            "SUM(CASE WHEN day >= ? THEN weighted ELSE 0 END) AS weighted_7d, "
            "SUM(CASE WHEN day >= ? THEN requests ELSE 0 END) AS requests_7d, "
            "SUM(CASE WHEN day >= ? THEN errors ELSE 0 END) AS errors_7d, "
            "SUM(weighted) AS weighted_30d, SUM(requests) AS requests_30d, "
            "SUM(input_tokens) AS input_30d, SUM(cached_tokens) AS cached_30d, SUM(output_tokens) AS output_30d "
            f"FROM usage_daily WHERE day >= ? {extra} GROUP BY key_id",
            [today, today, d7, d7, d7, d30, *args],
        )
        return {r.pop("key_id"): r for r in rows}

    def _key_row(self, key_id: int) -> dict:
        row = self.db.one(
            "SELECT k.*, u.username AS created_by_name FROM api_keys k "
            "LEFT JOIN users u ON u.id = k.created_by WHERE k.id = ?",
            (key_id,),
        )
        if not row:
            raise ApiError(404, "Key 不存在")
        return row

    def _visible_key(self, p: Principal, key_id: int) -> dict:
        row = self._key_row(key_id)
        if p.can("keys.view_all") or (p.kind == "key" and p.id == key_id) or (
            p.kind == "user" and row["created_by"] == p.id
        ):
            return row
        raise ApiError(404, "Key 不存在")

    async def create_key(self, request):
        p = await self.principal(request)
        if not p.can("keys.create"):
            raise ApiError(403, "当前账号没有创建 API Key 的权限")
        body = await self._json(request)
        alias = _alias(body.get("alias"))
        fields = {
            "owner": _text(body.get("owner"), "使用人", 64),
            "note": _text(body.get("note"), "备注", 500),
            "scopes": _scopes(body.get("scopes") or list(DEFAULT_SCOPES), p),
            "allowed_models": _models(body.get("allowed_models")),
            "rpm_limit": _opt_int(body.get("rpm_limit"), "每分钟请求数", 100_000),
            "concurrency_limit": _opt_int(body.get("concurrency_limit"), "并发数", 10_000),
            "daily_limit": _opt_int(body.get("daily_limit"), "每日额度"),
            "weekly_limit": _opt_int(body.get("weekly_limit"), "7 日额度"),
            "expires_at": _expires(body),
        }
        secret = generate_api_key()

        def create():
            if self.db.one("SELECT id FROM api_keys WHERE alias = ?", (alias,)):
                raise ApiError(409, f"别名 {alias} 已存在")
            now = now_ms()
            cur = self.db.execute(
                "INSERT INTO api_keys (alias, owner, note, key_hash, key_prefix, scopes, allowed_models, "
                "rpm_limit, concurrency_limit, daily_limit, weekly_limit, status, expires_at, created_at, "
                "created_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?)",
                (alias, fields["owner"], fields["note"], hash_api_key(secret), key_display_prefix(secret),
                 fields["scopes"], fields["allowed_models"], fields["rpm_limit"], fields["concurrency_limit"],
                 fields["daily_limit"], fields["weekly_limit"], fields["expires_at"], now,
                 p.id if p.kind == "user" else None, now),
            )
            kid = cur.lastrowid
            self._event(p, "key.create", request, target_type="key", target_id=kid, target_name=alias,
                        detail={k: v for k, v in fields.items() if v not in (None, "")})
            self.rt.keys.reload()
            return self._key_row(kid)

        row = await self.run(create)
        return web.json_response({"key": secret, "record": key_public(row)}, status=201)

    async def get_key(self, request):
        p = await self.principal(request)
        key_id = int(request.match_info["id"])
        days = min(max(int(request.query.get("days", 30)), 7), 90)
        return web.json_response(await self.run(self._key_detail, p, key_id, days))

    def _key_detail(self, p: Principal, key_id: int, days: int) -> dict:
        tz = self.rt.settings.tz_offset_minutes
        row = self._visible_key(p, key_id)
        since = day_of(day_start_ms(-(days - 1), tz), tz)
        daily = self.db.all(
            "SELECT day, SUM(requests) AS requests, SUM(errors) AS errors, SUM(input_tokens) AS input_tokens, "
            "SUM(cached_tokens) AS cached_tokens, SUM(output_tokens) AS output_tokens, SUM(weighted) AS weighted "
            "FROM usage_daily WHERE key_id = ? AND day >= ? GROUP BY day ORDER BY day",
            (key_id, since),
        )
        off = tz * 60_000
        hourly = self.db.all(
            "SELECT ((ts + ?) / 3600000) * 3600000 - ? AS hour, COUNT(*) AS requests, SUM(weighted) AS weighted, "
            "SUM(CASE WHEN status >= 400 OR error IS NOT NULL THEN 1 ELSE 0 END) AS errors "
            "FROM requests WHERE key_id = ? AND ts >= ? GROUP BY hour ORDER BY hour",
            (off, off, key_id, now_ms() - 48 * 3_600_000),
        )
        models = self.db.all(
            "SELECT model, SUM(requests) AS requests, SUM(weighted) AS weighted, SUM(input_tokens) AS input_tokens, "
            "SUM(cached_tokens) AS cached_tokens, SUM(output_tokens) AS output_tokens "
            "FROM usage_daily WHERE key_id = ? AND day >= ? GROUP BY model ORDER BY weighted DESC",
            (key_id, since),
        )
        ips = self.db.all(
            "SELECT client_ip, COUNT(*) AS requests, MAX(ts) AS last_ts, MAX(user_agent) AS user_agent "
            "FROM requests WHERE key_id = ? AND ts >= ? GROUP BY client_ip ORDER BY requests DESC LIMIT 20",
            (key_id, now_ms() - 7 * 86_400_000),
        )
        latency = self.db.all(
            "SELECT ttfb_ms FROM requests WHERE key_id = ? AND ts >= ? AND ttfb_ms IS NOT NULL AND status < 400 "
            "ORDER BY ttfb_ms",
            (key_id, now_ms() - 7 * 86_400_000),
        )
        ttfb = [r["ttfb_ms"] for r in latency]

        def pct(q):
            return ttfb[min(len(ttfb) - 1, int(q * len(ttfb)))] if ttfb else None

        item = key_public(row)
        item["can_manage"] = can_manage_key(p, row)
        item["usage"] = self._key_stats(tz, key_id).get(key_id, {})
        inflight = self.rt.limiter.inflight.get(key_id, 0)
        return {
            "key": item,
            "daily": daily,
            "hourly": hourly,
            "models": models,
            "ips": ips,
            "ttfb_p50": pct(0.5),
            "ttfb_p95": pct(0.95),
            "inflight": inflight,
            "tz_offset_minutes": tz,
        }

    async def update_key(self, request):
        p = await self.principal(request)
        key_id = int(request.match_info["id"])
        body = await self._json(request)

        def update():
            row = self._visible_key(p, key_id)
            if not can_manage_key(p, row):
                raise ApiError(403, "没有修改这把 Key 的权限")
            if row["status"] == "revoked":
                raise ApiError(409, "已吊销的 Key 不能修改")
            changes: dict = {}
            if "alias" in body:
                alias = _alias(body["alias"])
                other = self.db.one("SELECT id FROM api_keys WHERE alias = ? AND id != ?", (alias, key_id))
                if other:
                    raise ApiError(409, f"别名 {alias} 已存在")
                changes["alias"] = alias
            if "owner" in body:
                changes["owner"] = _text(body["owner"], "使用人", 64)
            if "note" in body:
                changes["note"] = _text(body["note"], "备注", 500)
            if "scopes" in body:
                scopes = _scopes(body["scopes"], p)
                had_accounts = "accounts" in split_list(row["scopes"])
                if had_accounts and "accounts" not in scopes.split(",") and not p.can("keys.grant_accounts"):
                    raise ApiError(403, "只有管理员可以调整账号池（accounts）范围")
                changes["scopes"] = scopes
            if "allowed_models" in body:
                changes["allowed_models"] = _models(body["allowed_models"])
            for field, label, maximum in (
                ("rpm_limit", "每分钟请求数", 100_000), ("concurrency_limit", "并发数", 10_000),
                ("daily_limit", "每日额度", 10**15), ("weekly_limit", "7 日额度", 10**15),
            ):
                if field in body:
                    changes[field] = _opt_int(body[field], label, maximum)
            if "expires_at" in body or "expires_in_days" in body:
                changes["expires_at"] = _expires(body)
            changes = {k: v for k, v in changes.items() if row.get(k) != v}
            if not changes:
                return row
            changes["updated_at"] = now_ms()
            sets = ", ".join(f"{k} = ?" for k in changes)
            self.db.execute(f"UPDATE api_keys SET {sets} WHERE id = ?", [*changes.values(), key_id])
            detail = {k: {"from": row.get(k), "to": v} for k, v in changes.items() if k != "updated_at"}
            self._event(p, "key.update", request, target_type="key", target_id=key_id,
                        target_name=changes.get("alias", row["alias"]), detail=detail)
            self.rt.keys.reload()
            return self._key_row(key_id)

        row = await self.run(update)
        return web.json_response({"record": key_public(row)})

    async def key_action(self, request):
        p = await self.principal(request)
        key_id = int(request.match_info["id"])
        action = request.match_info["action"]
        body = await self._json(request) if request.can_read_body else {}

        def act():
            row = self._visible_key(p, key_id)
            if not can_manage_key(p, row):
                raise ApiError(403, "没有操作这把 Key 的权限")
            if row["status"] == "revoked":
                raise ApiError(409, "Key 已吊销，不能再操作")
            now = now_ms()
            secret = None
            detail: dict = {}
            if action == "disable":
                self.db.execute("UPDATE api_keys SET status = 'disabled', updated_at = ? WHERE id = ?", (now, key_id))
            elif action == "enable":
                self.db.execute("UPDATE api_keys SET status = 'active', updated_at = ? WHERE id = ?", (now, key_id))
            elif action == "revoke":
                self.db.execute(
                    "UPDATE api_keys SET status = 'revoked', revoked_at = ?, updated_at = ?, "
                    "prev_key_hash = NULL, prev_key_expires_at = NULL WHERE id = ?",
                    (now, now, key_id),
                )
                self.db.execute("DELETE FROM sessions WHERE key_id = ?", (key_id,))
            elif action == "rotate":
                grace_hours = _opt_int(body.get("grace_hours"), "宽限小时数", 168) or 0
                secret = generate_api_key()
                self.db.execute(
                    "UPDATE api_keys SET key_hash = ?, key_prefix = ?, prev_key_hash = ?, "
                    "prev_key_expires_at = ?, updated_at = ? WHERE id = ?",
                    (hash_api_key(secret), key_display_prefix(secret),
                     row["key_hash"] if grace_hours else None,
                     now + grace_hours * 3_600_000 if grace_hours else None, now, key_id),
                )
                detail = {"grace_hours": grace_hours, "old_prefix": row["key_prefix"],
                          "new_prefix": key_display_prefix(secret)}
            self._event(p, f"key.{action}", request, target_type="key", target_id=key_id,
                        target_name=row["alias"], detail=detail or None)
            self.rt.keys.reload()
            return self._key_row(key_id), secret

        row, secret = await self.run(act)
        out = {"record": key_public(row)}
        if secret:
            out["key"] = secret
        return web.json_response(out)

    # -- request audit ----------------------------------------------------------

    def _request_filters(self, p: Principal, q) -> tuple[str, list]:
        cond, args = self._request_scope(p)
        where = [f"({cond})"]
        if q.get("key_id"):
            where.append("key_id = ?")
            args.append(int(q["key_id"]))
        if q.get("alias"):
            where.append("alias LIKE ?")
            args.append(f"%{q['alias']}%")
        if q.get("route"):
            where.append("route = ?")
            args.append(q["route"])
        if q.get("model"):
            where.append("(model_served LIKE ? OR model_requested LIKE ?)")
            args += [f"%{q['model']}%"] * 2
        status = q.get("status", "")
        if status == "ok":
            where.append("status < 400 AND error IS NULL")
        elif status == "error":
            where.append("(status >= 400 OR error IS NOT NULL)")
        elif status.isdigit():
            where.append("status = ?")
            args.append(int(status))
        if q.get("ip"):
            where.append("client_ip = ?")
            args.append(q["ip"])
        if q.get("session"):
            where.append("session_id = ?")
            args.append(q["session"])
        if q.get("error"):
            where.append("error LIKE ?")
            args.append(f"%{q['error']}%")
        if q.get("from"):
            where.append("ts >= ?")
            args.append(int(q["from"]))
        if q.get("to"):
            where.append("ts < ?")
            args.append(int(q["to"]))
        return " AND ".join(where), args

    async def list_requests(self, request):
        p = await self.principal(request)
        q = request.query
        limit = min(max(int(q.get("limit", 50)), 1), MAX_PAGE)

        def fetch():
            where, args = self._request_filters(p, q)
            if q.get("before_id"):
                where += " AND id < ?"
                args.append(int(q["before_id"]))
            rows = self.db.all(f"SELECT * FROM requests WHERE {where} ORDER BY id DESC LIMIT ?", [*args, limit + 1])
            return rows

        rows = await self.run(fetch)
        more = len(rows) > limit
        rows = rows[:limit]
        return web.json_response({"requests": rows, "next_before_id": rows[-1]["id"] if more else None})

    async def export_requests(self, request):
        p = await self.principal(request)
        q = request.query

        def fetch():
            where, args = self._request_filters(p, q)
            rows = self.db.all(
                f"SELECT * FROM requests WHERE {where} ORDER BY id DESC LIMIT {CSV_MAX_ROWS}", args
            )
            self._event(p, "audit.export", request, detail={k: v for k, v in q.items()} or None)
            return rows

        rows = await self.run(fetch)
        buf = io.StringIO()
        cols = ["id", "rid", "ts", "time", "key_id", "alias", "method", "path", "route", "model_requested",
                "model_served", "stream", "status", "error", "input_tokens", "cached_tokens", "output_tokens",
                "weighted", "req_bytes", "resp_bytes", "ttfb_ms", "duration_ms", "client_ip", "user_agent",
                "session_id", "key_fingerprint"]
        w = csv.writer(buf)
        w.writerow(cols)
        tz = self.rt.settings.tz_offset_minutes
        for r in rows:
            r["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"] / 1000 + tz * 60))
            w.writerow([r.get(c) for c in cols])
        name = time.strftime("cx2cc-requests-%Y%m%d-%H%M%S.csv", time.gmtime(time.time() + tz * 60))
        return web.Response(
            body=("﻿" + buf.getvalue()).encode("utf-8"),
            content_type="text/csv",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
        )

    async def list_events(self, request):
        p = await self.principal(request)
        q = request.query
        limit = min(max(int(q.get("limit", 50)), 1), MAX_PAGE)

        def fetch():
            where, args = [], []
            if p.can("audit.view_all"):
                where.append("1=1")
            elif p.kind == "key":
                where.append("target_type = 'key' AND target_id = ?")
                args.append(p.id)
            else:
                where.append(
                    "((actor_type = 'user' AND actor_id = ?) OR (target_type = 'key' AND target_id IN "
                    "(SELECT id FROM api_keys WHERE created_by = ?)))"
                )
                args += [p.id, p.id]
            if q.get("target_type"):
                where.append("target_type = ?")
                args.append(q["target_type"])
            if q.get("target_id"):
                where.append("target_id = ?")
                args.append(int(q["target_id"]))
            if q.get("action"):
                where.append("action LIKE ?")
                args.append(q["action"] + "%")
            if q.get("before_id"):
                where.append("id < ?")
                args.append(int(q["before_id"]))
            return self.db.all(
                f"SELECT * FROM audit_events WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                [*args, limit + 1],
            )

        rows = await self.run(fetch)
        more = len(rows) > limit
        rows = rows[:limit]
        for r in rows:
            if r.get("detail"):
                try:
                    r["detail"] = json.loads(r["detail"])
                except ValueError:
                    pass
        return web.json_response({"events": rows, "next_before_id": rows[-1]["id"] if more else None})

    # -- console users --------------------------------------------------------------

    async def list_users(self, request):
        p = await self.principal(request)
        if not p.can("users.manage"):
            raise ApiError(403, "只有管理员可以管理控制台账号")
        rows = await self.run(self.db.all, "SELECT * FROM users ORDER BY id")
        return web.json_response({"users": [user_public(r) for r in rows]})

    async def create_user(self, request):
        p = await self.principal(request)
        if not p.can("users.manage"):
            raise ApiError(403, "只有管理员可以管理控制台账号")
        body = await self._json(request)
        username = _text(body.get("username"), "用户名", 64, required=True)
        if not re.fullmatch(r"[A-Za-z0-9._@-]{2,64}", username):
            raise ApiError(400, "用户名只能包含字母、数字和 . _ @ -")
        role = body.get("role")
        if role not in ROLES:
            raise ApiError(400, "角色必须是 admin / operator / auditor")
        password = str(body.get("password") or "")
        problem = password_problem(password)
        if problem:
            raise ApiError(400, problem)
        display = _text(body.get("display_name"), "显示名", 64)

        def create():
            if self.db.one("SELECT id FROM users WHERE username = ?", (username,)):
                raise ApiError(409, f"用户名 {username} 已存在")
            now = now_ms()
            cur = self.db.execute(
                "INSERT INTO users (username, display_name, role, password_hash, created_at, created_by, "
                "password_changed_at) VALUES (?,?,?,?,?,?,?)",
                (username, display, role, hash_password(password), now, p.id, now),
            )
            self._event(p, "user.create", request, target_type="user", target_id=cur.lastrowid,
                        target_name=username, detail={"role": role})
            return self.db.one("SELECT * FROM users WHERE id = ?", (cur.lastrowid,))

        row = await self.run(create)
        return web.json_response({"user": user_public(row)}, status=201)

    async def update_user(self, request):
        p = await self.principal(request)
        if not p.can("users.manage"):
            raise ApiError(403, "只有管理员可以管理控制台账号")
        user_id = int(request.match_info["id"])
        body = await self._json(request)

        def update():
            u = self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))
            if not u:
                raise ApiError(404, "账号不存在")
            changes: dict = {}
            if "display_name" in body:
                changes["display_name"] = _text(body["display_name"], "显示名", 64)
            if "role" in body:
                if body["role"] not in ROLES:
                    raise ApiError(400, "角色必须是 admin / operator / auditor")
                changes["role"] = body["role"]
            if "disabled" in body:
                changes["disabled"] = 1 if body["disabled"] else 0
            demoting = changes.get("role", u["role"]) != "admin" or changes.get("disabled", u["disabled"])
            if u["role"] == "admin" and not u["disabled"] and demoting:
                admins = self.db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND disabled = 0")["n"]
                if admins <= 1:
                    raise ApiError(409, "至少要保留一个可用的管理员")
            detail = {k: {"from": u.get(k), "to": v} for k, v in changes.items() if u.get(k) != v}
            if body.get("password"):
                problem = password_problem(str(body["password"]))
                if problem:
                    raise ApiError(400, problem)
                changes["password_hash"] = hash_password(str(body["password"]))
                changes["password_changed_at"] = now_ms()
                detail["password"] = "reset"
            if not changes:
                return u
            sets = ", ".join(f"{k} = ?" for k in changes)
            self.db.execute(f"UPDATE users SET {sets} WHERE id = ?", [*changes.values(), user_id])
            if changes.get("disabled") or "password_hash" in changes or "role" in changes:
                self.db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            self._event(p, "user.update", request, target_type="user", target_id=user_id,
                        target_name=u["username"], detail=detail)
            return self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))

        row = await self.run(update)
        return web.json_response({"user": user_public(row)})

    # -- settings ---------------------------------------------------------------------

    async def get_settings(self, request):
        p = await self.principal(request)
        s = self.rt.settings
        return web.json_response({
            "settings": {
                "weight_uncached": s.w_uncached, "weight_cached": s.w_cached, "weight_output": s.w_output,
                "retention_days": s.retention_days, "tz_offset_minutes": s.tz_offset_minutes,
            },
            "editable": p.can("settings.manage"),
            "scopes": list(SCOPES),
            "default_scopes": list(DEFAULT_SCOPES),
            "api_prefix": self.rt.cfg.api_prefix,
            "roles": {r: sorted(v) for r, v in ROLE_PERMS.items()},
        })

    async def update_settings(self, request):
        p = await self.principal(request)
        if not p.can("settings.manage"):
            raise ApiError(403, "只有管理员可以修改设置")
        body = await self._json(request)
        allowed = {
            "weight_uncached": (float, 0, 1000), "weight_cached": (float, 0, 1000),
            "weight_output": (float, 0, 1000), "retention_days": (int, 7, 3650),
            "tz_offset_minutes": (int, -720, 840),
        }
        changes = {}
        for k, (typ, lo, hi) in allowed.items():
            if k in body:
                try:
                    v = typ(body[k])
                except (TypeError, ValueError):
                    raise ApiError(400, f"{k} 格式不正确")
                if not lo <= v <= hi:
                    raise ApiError(400, f"{k} 超出范围")
                changes[k] = v

        def save():
            before = self.db.settings()
            for k, v in changes.items():
                self.db.set_setting(k, str(v))
            self._event(p, "settings.update", request, target_type="settings",
                        detail={k: {"from": before.get(k), "to": v} for k, v in changes.items()})
            self.rt.reload_settings()

        await self.run(save)
        return await self.get_settings(request)

    # -- upstream view -------------------------------------------------------------

    async def upstream(self, request):
        p = await self.principal(request)
        out: dict = {"catalog": self.rt.catalog.payload, "catalog_fetched_at": self.rt.catalog.fetched_at}
        out["health"] = await self._upstream_get("health", "/health", 10)
        if p.can("upstream.view"):
            accounts = await self._upstream_get("accounts", "/accounts", 30)
            if isinstance(accounts, dict) and isinstance(accounts.get("accounts"), list):
                keep = ("id", "plan", "email", "active", "available", "used_percent", "window_reset_at",
                        "limit_reached", "cooldown_reason", "cooldown_until", "skipped", "priority")
                accounts = {
                    "active": accounts.get("active"),
                    "accounts": [{k: a.get(k) for k in keep if k in a} for a in accounts["accounts"]],
                }
            out["accounts"] = accounts
            out["usage"] = await self._upstream_get("usage", "/usage", 30)
        return web.json_response(out)

    async def _upstream_get(self, name: str, path: str, ttl: int):
        cached = self._upstream_cache.get(name)
        if cached and cached[0] > time.time() - ttl:
            return cached[1]
        try:
            async with self.rt.session.get(
                self.rt.cfg.upstream + path,
                headers={"Authorization": f"Bearer {self.rt.cfg.upstream_token}"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                value = await r.json(content_type=None) if r.status == 200 else {"error": f"HTTP {r.status}"}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            value = {"error": type(exc).__name__}
        self._upstream_cache[name] = (time.time(), value)
        return value

    def note_health(self, ok: bool) -> None:
        self._upstream_cache["health"] = (time.time(), {"status": "ok"} if ok else {"error": "unreachable"})


@web.middleware
async def console_middleware(request: web.Request, handler):
    if not request.path.startswith("/admin"):
        return await handler(request)
    try:
        resp = await handler(request)
    except ApiError as exc:
        resp = web.json_response({"error": exc.message}, status=exc.status)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("console error on %s", request.path)
        resp = web.json_response({"error": "服务器内部错误"}, status=500)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    if request.path.startswith("/admin/api"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp
