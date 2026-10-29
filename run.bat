@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 选择能导入全部依赖的解释器（numpy / PyQt6 / cubiomespi / requests，
REM 以及可选的 PyQt6-WebEngine —— 后者缺失时启动器自动降级为离线地图）。
REM 注意：项目里的 venv 是隔离环境且缺依赖，不要再用它；直接用系统已装好
REM 依赖的 Python（通常是 `py` 启动器对应的 3.14）。
set "PY="

where py >nul 2>&1
if not errorlevel 1 (
    py -c "import numpy, PyQt6, cubiomespi, requests" >nul 2>&1
    if not errorlevel 1 set "PY=py"
)
if not defined PY (
    python -c "import numpy, PyQt6, cubiomespi, requests" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo 未找到已安装依赖的 Python。请先执行：
    echo   pip install numpy PyQt6 cubiomespi requests PyQt6-WebEngine
    echo （若 cubiomespi 安装失败，改用官方源：
    echo   pip install cubiomespi -i https://pypi.org/simple）
    pause
    exit /b 1
)

"%PY%" PlainMC.py
pause
