@echo off
rem ============================================================
rem 和包自动领券 - 一键启动（Windows）
rem 首次运行自动创建虚拟环境并安装依赖；之后每次一键启动。
rem 可选环境变量：
rem   PORT         监听端口，默认 8000
rem   WEBHOOK_URL  外部 Webhook 地址（如 https://你的隧道/api/hooks），配置后自动拉取验证码
rem ============================================================
chcp 65001 >nul
cd /d %~dp0

rem 1) 定位 Python
set PY=python
where python >nul 2>nul
if errorlevel 1 (
  where py >nul 2>nul
  if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.8+ ^(https://python.org^)
    pause & exit /b 1
  )
  set PY=py -3
)

rem 2) 检查 Node.js
where node >nul 2>nul
if errorlevel 1 (
  echo [错误] 未检测到 Node.js，请先安装 ^(https://nodejs.org^)
  pause & exit /b 1
)

rem 3) 创建虚拟环境并安装依赖（仅首次）
if not exist .venv (
  echo [首次运行] 创建虚拟环境并安装依赖...
  %PY% -m venv .venv
  .venv\Scripts\pip install --quiet --upgrade pip
  .venv\Scripts\pip install --quiet pycryptodome
  echo [完成] 依赖安装完成
)

rem 4) 启动
if "%PORT%"=="" set PORT=8000
echo [启动] Web 控制台: http://localhost:%PORT%
echo         Webhook 地址(外接): http://^<本机局域网IP^>:%PORT%/api/hooks
.venv\Scripts\python web_hebao.py
pause
