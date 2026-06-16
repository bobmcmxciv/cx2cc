from __future__ import annotations

import json
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


def health_details() -> dict | None:
    try:
        with urlopen(f"http://{HOST}:{PORT}/health", timeout=3) as resp:
            body = resp.read(2048).decode("utf-8", errors="replace")
            if resp.status != 200:
                return None
        return json.loads(body)
    except Exception:
        return None


def open_path(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass



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


def gui() -> int:
    import tkinter as tk
    from tkinter import messagebox

    base_url = f"http://{HOST}:{PORT}"
    env_path = ROOT / ".env"
    env_example = ROOT / ".env.example"

    root = tk.Tk()
    root.title("cx2cc")
    root.geometry("440x360")
    root.resizable(False, False)

    status_var = tk.StringVar(value="检测中…")
    addr_var = tk.StringVar(value=base_url)
    upstream_var = tk.StringVar(value="未知")

    tk.Label(root, text="cx2cc", font=("Segoe UI", 16, "bold")).pack(pady=(12, 6))

    info = tk.Frame(root)
    info.pack(fill="x", padx=16)
    for label, var in (("状态", status_var), ("地址", addr_var), ("上游", upstream_var)):
        row = tk.Frame(info)
        row.pack(fill="x", pady=2)
        tk.Label(row, text=f"{label}:", width=6, anchor="w").pack(side="left")
        tk.Label(row, textvariable=var, anchor="w").pack(side="left")

    log_box = tk.Text(root, height=7, width=52, state="disabled", wrap="none")
    log_box.pack(padx=16, pady=(10, 6))

    def refresh() -> None:
        details = health_details()
        if details:
            status_var.set("● 运行中")
            upstream_var.set("已配置" if details.get("upstream_configured") else "未配置")
        elif port_open():
            status_var.set("端口被占用，但 cx2cc 未就绪")
            upstream_var.set("未知")
        else:
            status_var.set("○ 未运行")
            upstream_var.set("未知")

        startup_log = LOG_DIR / "startup.log"
        text = ""
        if startup_log.exists():
            try:
                lines = startup_log.read_text(encoding="utf-8", errors="replace").splitlines()
                text = "\n".join(lines[-8:])
            except Exception:
                text = ""
        log_box.config(state="normal")
        log_box.delete("1.0", "end")
        log_box.insert("1.0", text)
        log_box.config(state="disabled")

    def on_start() -> None:
        start()
        root.after(1200, refresh)

    def on_open_config() -> None:
        if env_path.exists():
            open_path(env_path)
        elif env_example.exists():
            messagebox.showinfo("cx2cc", "未找到 .env，将打开 .env.example，请另存为 .env。")
            open_path(env_example)
        else:
            messagebox.showwarning("cx2cc", "未找到 .env 或 .env.example。")

    def on_copy_url() -> None:
        root.clipboard_clear()
        root.clipboard_append(base_url)

    buttons = tk.Frame(root)
    buttons.pack(pady=4)
    row1 = tk.Frame(buttons)
    row1.pack()
    tk.Button(row1, text="启动服务", width=12, command=on_start).pack(side="left", padx=4)
    tk.Button(row1, text="刷新状态", width=12, command=refresh).pack(side="left", padx=4)
    tk.Button(row1, text="打开配置", width=12, command=on_open_config).pack(side="left", padx=4)
    row2 = tk.Frame(buttons)
    row2.pack(pady=(6, 0))
    tk.Button(row2, text="打开日志", width=12, command=lambda: open_path(LOG_DIR)).pack(side="left", padx=4)
    tk.Button(row2, text="复制 Base URL", width=12, command=on_copy_url).pack(side="left", padx=4)
    tk.Button(row2, text="退出", width=12, command=root.destroy).pack(side="left", padx=4)

    def poll() -> None:
        refresh()
        root.after(4000, poll)

    poll()
    root.mainloop()
    return 0


def usage() -> None:
    print("Usage: cx2cc [gui|serve|start|health]")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0].lower() if args else "gui"

    if command == "gui":
        return gui()
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
