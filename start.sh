#!/bin/bash
# AI Time Guard - 启动脚本
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
  PYTHON_BIN=".venv/bin/python"
else
  PYTHON_BIN="python3"
fi

"$PYTHON_BIN" ai_time_guard.py &
echo "AI Time Guard 已启动，使用: $PYTHON_BIN"
