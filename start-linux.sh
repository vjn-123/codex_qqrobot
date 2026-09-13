#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_DIR="$ROOT/client"

cd "$ROOT"

if [[ ! -f "$CLIENT_DIR/qq_gateway_client.py" ]]; then
  echo "[错误] 找不到 client/qq_gateway_client.py"
  exit 1
fi

if [[ ! -f "$CLIENT_DIR/.env" ]]; then
  echo "[错误] 找不到 client/.env"
  echo "请先复制 client/.env.example 为 client/.env，并填写 QQ_APP_ID 和 QQ_APP_SECRET。"
  exit 1
fi

PYTHON_BIN="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[错误] 项目虚拟环境不存在：$PYTHON_BIN"
  echo "请先执行：./setup-venv.sh"
  exit 1
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "[错误] 找不到 Codex CLI，请先安装并完成登录。"
  exit 1
fi

echo "QQ 机器人正在启动..."
echo "项目目录：$ROOT"
echo "按 Ctrl+C 停止。"
echo

exec "$PYTHON_BIN" "$CLIENT_DIR/qq_gateway_client.py"
