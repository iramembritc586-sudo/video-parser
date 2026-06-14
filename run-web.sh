#!/usr/bin/env bash
# 启动网页版（Linux / macOS）：自动建虚拟环境、装依赖、开服务并打开浏览器
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "首次运行：创建虚拟环境并安装依赖…"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
# 避免被外部 PORT 环境变量干扰
unset PORT
exec .venv/bin/python web.py
