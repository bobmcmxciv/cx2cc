#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PLIST="com.cx2cc.proxy"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST}.plist"
HOST="${CX2CC_HOST:-127.0.0.1}"
PORT="${CX2CC_PORT:-8901}"

_health() {
    curl -s --max-time 3 "http://${HOST}:${PORT}/health" 2>/dev/null || echo "UNREACHABLE"
}

_status_info() {
    echo "  监听地址: http://${HOST}:${PORT}"
    echo "  健康检查: $(_health)"
}

install() {
    mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"
    chmod +x "$ROOT/cx2cc-wrapper.sh"
    cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${ROOT}/cx2cc-wrapper.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${ROOT}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${ROOT}/logs/cx2cc.out.log</string>
    <key>StandardErrorPath</key>
    <string>${ROOT}/logs/cx2cc.err.log</string>
</dict>
</plist>
EOF
    echo "已安装 LaunchAgent: $PLIST_PATH"
}

uninstall() {
    stop || true
    rm -f "$PLIST_PATH"
    echo "已移除 LaunchAgent: $PLIST_PATH"
}

start() {
    if launchctl list | grep -q "$PLIST"; then
        echo "cx2cc 已在运行中"
        _status_info
        return 0
    fi
    if [ ! -f "$PLIST_PATH" ]; then
        install
    fi
    launchctl load "$PLIST_PATH"
    echo "cx2cc 已通过 launchd 启动"
    sleep 1
    _status_info
}

stop() {
    if launchctl list | grep -q "$PLIST"; then
        launchctl unload "$PLIST_PATH"
        echo "cx2cc 已停止"
    else
        echo "cx2cc 未在运行"
    fi
}

restart() {
    stop
    sleep 1
    start
}

status() {
    if launchctl list | grep -q "$PLIST"; then
        echo "cx2cc launchd 服务: 已加载"
        _status_info
    else
        echo "cx2cc launchd 服务: 未加载"
    fi
}

log() {
    local log_dir="$ROOT/logs"
    local logfile="${1:-cx2cc.out.log}"
    if [ -f "$log_dir/$logfile" ]; then
        tail -f "$log_dir/$logfile"
    else
        echo "日志文件不存在: $log_dir/$logfile"
        ls -la "$log_dir/" 2>/dev/null || true
    fi
}

case "${1:-status}" in
    install)   install ;;
    uninstall) uninstall ;;
    start)     start ;;
    stop)      stop ;;
    restart)   restart ;;
    status)    status ;;
    log)       log "${2:-}" ;;
    *)
        echo "用法: $0 {install|uninstall|start|stop|restart|status|log}"
        exit 1
        ;;
esac
