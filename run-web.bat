@echo off
REM 启动网页版（Windows）：首次运行自动建虚拟环境并装依赖
cd /d "%~dp0"
if not exist .venv (
  echo 首次运行：创建虚拟环境并安装依赖...
  python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\pip install -r requirements.txt
)
set PORT=
.venv\Scripts\python web.py
pause
