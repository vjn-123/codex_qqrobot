#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$ROOT/.venv"
PYTHON_BIN="${PYTHON_BIN:-}"

if [[ -z "$PYTHON_BIN" ]]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
fi

if [[ -z "$PYTHON_BIN" ]]; then
  echo "[错误] 找不到 Python 3。请先安装 Python 3.10 或更高版本。" >&2
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "创建项目虚拟环境：$VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install -r "$ROOT/requirements.txt"

if [[ ! -f "$ROOT/client/.env" ]]; then
  cp "$ROOT/client/.env.example" "$ROOT/client/.env"
  echo "已创建 client/.env，请填写 QQ_APP_ID、QQ_APP_SECRET 和 CODEX_COMMAND。"
else
  echo "已保留现有 client/.env。"
fi

echo "环境准备完成。"
echo "下一步：编辑 client/.env，然后运行 ./start-linux.sh"
