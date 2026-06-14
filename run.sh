#!/usr/bin/env bash
# 启动「网页视频地址提取」GUI
cd "$(dirname "$0")"
exec .venv/bin/python app.py "$@"
