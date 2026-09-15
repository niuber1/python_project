#!/usr/bin/env bash
# crawlerToBase Linux 启动脚本：在项目目录后台启动，运行输出统一写入 nohup.log。
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_BIN="$APP_DIR/.venv/bin/python"
ENV_FILE="$APP_DIR/.env"
PID_FILE="$APP_DIR/crawler.pid"
LOG_FILE="$APP_DIR/nohup.log"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "未找到虚拟环境：$PYTHON_BIN；请先执行部署初始化。" >&2
  exit 1
fi
if [[ ! -f "$ENV_FILE" ]]; then
  echo "未找到配置文件：$ENV_FILE" >&2
  exit 1
fi

if [[ -f "$PID_FILE" ]]; then
  previous_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ "$previous_pid" =~ ^[0-9]+$ ]] && kill -0 "$previous_pid" 2>/dev/null; then
    kill "$previous_pid"
    for _ in {1..20}; do
      kill -0 "$previous_pid" 2>/dev/null || break
      sleep 1
    done
    kill -0 "$previous_pid" 2>/dev/null && kill -9 "$previous_pid" || true
  fi
fi

cd "$APP_DIR"
nohup "$PYTHON_BIN" main.py >> "$LOG_FILE" 2>&1 &
echo $! > "$PID_FILE"
echo "crawlerToBase 已启动，PID=$(cat "$PID_FILE")，日志：$LOG_FILE"
