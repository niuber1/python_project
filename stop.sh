#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_FILE="$APP_DIR/crawler.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "未找到 PID 文件，服务可能未由 start.sh 启动。"
  exit 0
fi

pid="$(cat "$PID_FILE")"
if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
  kill "$pid"
  echo "已停止 crawlerToBase，PID=$pid"
else
  echo "服务未运行，清理过期 PID 文件。"
fi
rm -f "$PID_FILE"
