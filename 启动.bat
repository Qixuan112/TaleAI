@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   塔利 TaleAI 一键启动
echo ============================================
echo.

REM ---- 1) 选 Python：优先项目 venv ----
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
  echo [1/4] 使用项目虚拟环境 .venv
  goto :have_py
)

echo [1/4] 没找到 .venv，尝试创建...
where uv >nul 2>nul
if %errorlevel%==0 (
  echo       用 uv 建虚拟环境...
  uv venv --seed .venv
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    echo       用 python -m venv 建虚拟环境...
    python -m venv .venv
  ) else (
    echo.
    echo [错误] 既没有 .venv，也没找到 uv / python。
    echo        请先安装 Python 3.11+ 或 uv，再运行本脚本。
    goto :fail
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo [错误] 虚拟环境创建失败。
  goto :fail
)
set "PY=.venv\Scripts\python.exe"

:have_py
REM ---- 2) 确认依赖装好（缺 fastapi 就装一遍）----
echo [2/4] 检查依赖...
"%PY%" -c "import fastapi, uvicorn, openai" >nul 2>nul
if %errorlevel%==0 (
  echo       依赖就绪
) else (
  echo       缺依赖，正在安装...
  where uv >nul 2>nul
  if %errorlevel%==0 (
    uv pip install -e .
  ) else (
    "%PY%" -m pip install -e .
  )
  if !errorlevel! neq 0 (
    echo [错误] 依赖安装失败。
    goto :fail
  )
)

REM ---- 3) 端口预检 ----
REM 端口：已设 TALEAI_PORT 就用它，否则 8321。
if not defined TALEAI_PORT set "TALEAI_PORT=8321"
echo [3/4] 检查端口 %TALEAI_PORT% ...
netstat -ano | findstr "127.0.0.1:%TALEAI_PORT%" | findstr "LISTENING" >nul 2>nul
if !errorlevel!==0 (
  echo.
  echo [错误] 端口 %TALEAI_PORT% 已被占用——多半是塔利已经在跑了。
  echo        要么直接用现有那个；要么换个端口再启动，例如：
  echo            set TALEAI_PORT=8322
  echo            启动.bat
  echo.
  pause
  exit /b 1
)
echo       端口空闲

REM ---- 4) 启动 ----
echo [4/4] 启动塔利...
echo.
echo   聊天页：http://127.0.0.1:%TALEAI_PORT%/
echo   日志页：http://127.0.0.1:%TALEAI_PORT%/logs
echo   设置页：http://127.0.0.1:%TALEAI_PORT%/settings
echo.
echo   （按 Ctrl-C 停止；关掉本窗口也会停止）
echo --------------------------------------------
"%PY%" main.py
echo.
echo 塔利已退出。
pause
exit /b 0

:fail
echo.
pause
exit /b 1
