@echo off
chcp 65001 >nul
cd /d %~dp0\..

echo ============================================
echo   QQ 聊天机器人 - 本机启动 (NoneBot)
echo   请先确认本机已安装 Python 3.8+（3.10+ 为本工程实测基线）
echo ============================================

if not exist .venv (
    echo [1/3] 首次运行：创建虚拟环境...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo 错误：找不到 python 命令。请先安装 Python 3.8+ 并勾选 "Add to PATH"。
        echo 下载：https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat

echo [2/3] 安装依赖（首次较慢，约 1-3 分钟）...
python -m pip install --upgrade pip
pip install -r requirements.txt

echo [3/3] 启动机器人（监听 127.0.0.1:8080）...
echo ============================================
echo   启动后保持此窗口打开不要关。
echo   让 NapCat 连 ws://127.0.0.1:8080/onebot/v11/ws
echo   控制台出现 "Bot XXXXXXXXXX connected" 即成功。
echo ============================================
python bot.py
pause
