import os
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent
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


def main() -> int:
    if health_ok():
        log(f"cx2cc already healthy on {HOST}:{PORT}")
        return 0

    if port_open():
        log(f"port {HOST}:{PORT} is already in use; not starting cx2cc")
        return 0

    stdout = open(LOG_DIR / "cx2cc.out.log", "ab")
    stderr = open(LOG_DIR / "cx2cc.err.log", "ab")
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )

    proc = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=str(ROOT),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        close_fds=False,
        creationflags=creationflags,
    )
    log(f"started cx2cc pid={proc.pid} on {HOST}:{PORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
