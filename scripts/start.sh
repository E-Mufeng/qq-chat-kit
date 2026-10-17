#!/usr/bin/env bash
# QQ 聊天机器人 - Linux / macOS 启动脚本
# 用法：bash scripts/start.sh
set -e
cd "$(dirname "$0")/.."

echo "=== QQ 聊天机器人 - 本机启动 (NoneBot) ==="
echo "请先确认本机已安装 Python 3.8+（3.10+ 为本工程实测基线）"

if [ ! -d .venv ]; then
    echo "[1/3] 首次运行：创建虚拟环境..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[2/3] 安装依赖（首次较慢）..."
pip install --upgrade pip
pip install -r requirements.txt

echo "[3/3] 启动机器人（监听 127.0.0.1:8080）..."
echo "让 NapCat 连 ws://127.0.0.1:8080/onebot/v11/ws"
echo "控制台出现 'Bot XXXXXXXXXX connected' 即成功。"
python bot.py
