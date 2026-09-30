"""Command line: run the gateway, or manage users and keys offline.

    python -m cx2cc_gateway serve
    python -m cx2cc_gateway create-user --username admin --role admin      (password from stdin)
    python -m cx2cc_gateway reset-password --username admin                (password from stdin)
    python -m cx2cc_gateway create-key --alias bob-mac --owner bob [--scopes chat,images,usage]
    python -m cx2cc_gateway import-key --alias legacy-shared --scopes chat,images,usage,accounts
                                                                           (existing secret from stdin)
    python -m cx2cc_gateway list-keys
"""
from __future__ import annotations

import argparse
import getpass
import sys

from . import config
from .db import DEFAULT_SCOPES, SCOPES, Database, now_ms
from .security import (
    generate_api_key,
    hash_api_key,
    hash_password,
    key_display_prefix,
    password_problem,
)


def _read_secret(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().strip()


def _scopes(raw: str) -> str:
    items = [s.strip() for s in raw.split(",") if s.strip()]
    bad = [s for s in items if s not in SCOPES]
    if bad or not items:
        raise SystemExit(f"scopes must be a subset of {','.join(SCOPES)}")
    return ",".join(s for s in SCOPES if s in items)


def _insert_key(db: Database, alias: str, owner: str, note: str, scopes: str, secret: str) -> int:
    if db.one("SELECT id FROM api_keys WHERE alias = ?", (alias,)):
        raise SystemExit(f"alias {alias} already exists")
    if db.one("SELECT id FROM api_keys WHERE key_hash = ?", (hash_api_key(secret),)):
        raise SystemExit("this secret is already registered under another alias")
    now = now_ms()
    cur = db.execute(
        "INSERT INTO api_keys (alias, owner, note, key_hash, key_prefix, scopes, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,'active',?,?)",
        (alias, owner, note, hash_api_key(secret), key_display_prefix(secret), scopes, now, now),
    )
    return cur.lastrowid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cx2cc_gateway")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    cu = sub.add_parser("create-user")
    cu.add_argument("--username", required=True)
    cu.add_argument("--role", choices=("admin", "operator", "auditor"), required=True)
    cu.add_argument("--display-name", default="")
    rp = sub.add_parser("reset-password")
    rp.add_argument("--username", required=True)
    for name in ("create-key", "import-key"):
        k = sub.add_parser(name)
        k.add_argument("--alias", required=True)
        k.add_argument("--owner", default="")
        k.add_argument("--note", default="")
        k.add_argument("--scopes", default=",".join(DEFAULT_SCOPES))
    sub.add_parser("list-keys")
    args = parser.parse_args(argv)

    cfg = config.load()
    if args.cmd == "serve":
        from .app import serve

        serve(cfg)
        return 0

    db = Database(cfg.db_path)
    if args.cmd == "create-user":
        password = _read_secret("password: ")
        problem = password_problem(password)
        if problem:
            raise SystemExit(problem)
        if db.one("SELECT id FROM users WHERE username = ?", (args.username,)):
            raise SystemExit(f"user {args.username} already exists")
        now = now_ms()
        cur = db.execute(
            "INSERT INTO users (username, display_name, role, password_hash, created_at, password_changed_at) "
            "VALUES (?,?,?,?,?,?)",
            (args.username, args.display_name, args.role, hash_password(password), now, now),
        )
        db.event("user.create", actor_type="cli", actor_name="cli", target_type="user",
                 target_id=cur.lastrowid, target_name=args.username, detail={"role": args.role})
        print(f"created {args.role} {args.username}")
    elif args.cmd == "reset-password":
        u = db.one("SELECT * FROM users WHERE username = ?", (args.username,))
        if not u:
            raise SystemExit(f"no user {args.username}")
        password = _read_secret("new password: ")
        problem = password_problem(password)
        if problem:
            raise SystemExit(problem)
        db.execute("UPDATE users SET password_hash = ?, password_changed_at = ?, disabled = 0 WHERE id = ?",
                   (hash_password(password), now_ms(), u["id"]))
        db.execute("DELETE FROM sessions WHERE user_id = ?", (u["id"],))
        db.event("user.update", actor_type="cli", actor_name="cli", target_type="user", target_id=u["id"],
                 target_name=u["username"], detail={"password": "reset"})
        print(f"password reset for {args.username}")
    elif args.cmd in ("create-key", "import-key"):
        scopes = _scopes(args.scopes)
        secret = generate_api_key() if args.cmd == "create-key" else _read_secret("existing key: ")
        if len(secret) < 20:
            raise SystemExit("refusing a key shorter than 20 characters")
        kid = _insert_key(db, args.alias, args.owner, args.note, scopes, secret)
        db.event(f"key.{args.cmd.split('-')[0]}", actor_type="cli", actor_name="cli", target_type="key",
                 target_id=kid, target_name=args.alias, detail={"scopes": scopes})
        if args.cmd == "create-key":
            print(secret)
        else:
            print(f"imported {args.alias} (id {kid}, prefix {key_display_prefix(secret)})")
    elif args.cmd == "list-keys":
        for r in db.all("SELECT id, alias, owner, status, key_prefix, scopes, last_used_at FROM api_keys ORDER BY id"):
            print(f"{r['id']:>4}  {r['alias']:<24} {r['status']:<9} {r['key_prefix']:<16} "
                  f"{r['scopes']:<28} {r['owner']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
