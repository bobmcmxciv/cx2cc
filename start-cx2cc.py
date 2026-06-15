from __future__ import annotations

import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT = app_dir()
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

HOST = os.environ.get("CX2CC_HOST", "127.0.0.1")
PORT = int(os.environ.get("CX2CC_PORT", "8901"))


def log(message: str) -> None:
    with open(LOG_DIR / "startup.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")


def health_ok() -> bool:
    try:
        with urlopen(f"http://{HOST}:{PORT}/health", timeout=3) as resp:
            body = resp.read(512).decode("utf-8", errors="replace")
            status = resp.status
        return status == 200 and '"status":"ok"' in body.replace(" ", "")
    except Exception:
        return False


def port_open() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=1):
            return True
    except OSError:
        return False


def serve() -> int:
    from server import main as server_main

    server_main()
    return 0


def _serve_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "serve"]
    return [sys.executable, str(Path(__file__).resolve()), "serve"]


def start() -> int:
    if health_ok():
        log(f"cx2cc already healthy on {HOST}:{PORT}")
        return 0

    if port_open():
        log(f"port {HOST}:{PORT} is already in use; not starting cx2cc")
        return 0

    stdout = open(LOG_DIR / "cx2cc.out.log", "ab")
    stderr = open(LOG_DIR / "cx2cc.err.log", "ab")
    creationflags = 0
    close_fds = True
    if os.name == "nt":
        creationflags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
        close_fds = False

    proc = subprocess.Popen(
        _serve_command(),
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        close_fds=close_fds,
        creationflags=creationflags,
    )
    log(f"started cx2cc pid={proc.pid} on {HOST}:{PORT}")
    return 0


def health() -> int:
    if health_ok():
        print(f"cx2cc is healthy on http://{HOST}:{PORT}")
        return 0
    print(f"cx2cc is not responding on http://{HOST}:{PORT}")
    return 1


def usage() -> None:
    print("Usage: cx2cc [serve|start|health]")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0].lower() if args else "serve"

    if command == "serve":
        return serve()
    if command == "start":
        return start()
    if command == "health":
        return health()

    usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
