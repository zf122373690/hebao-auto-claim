#!/usr/bin/env bash
# ============================================================
# 和包自动领券 - 一键启动（macOS / Linux）
# 首次运行自动创建虚拟环境并安装依赖；之后每次一键启动。
# 可选环境变量：
#   PORT         监听端口，默认 8000
#   WEBHOOK_URL  外部 Webhook 地址（如 https://你的隧道/api/hooks），配置后自动拉取验证码
# ============================================================
set -e
cd "$(dirname "$0")"

# 1) 定位 Python
if command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  PY=python
fi
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "❌ 未检测到 Python，请先安装 Python 3.8+（https://python.org）"
  exit 1
fi

# 2) 检查 Node.js（核心脚本通过 node 发加密请求）
if ! command -v node >/dev/null 2>&1; then
  echo "❌ 未检测到 Node.js，请先安装（https://nodejs.org）"
  exit 1
fi

# 3) 创建虚拟环境并安装依赖（仅首次）
if [ ! -d ".venv" ]; then
  echo "🛠️  首次运行：创建虚拟环境并安装依赖…"
  "$PY" -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet pycryptodome
  echo "✅ 依赖安装完成"
fi

# 4) 启动
PORT="${PORT:-8000}"
echo "🚀 启动 Web 控制台: http://localhost:${PORT}"
echo "   Webhook 地址(外接): http://<本机局域网IP>:${PORT}/api/hooks"
exec env PORT="$PORT" ./.venv/bin/python web_hebao.py
