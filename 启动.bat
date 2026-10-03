@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo   塔利 TaleAI 一键启动
echo ============================================
echo.

REM ---- 端口：先定好（后面预检和打印都用它）----
REM 只接受 1..65535 的数字；非法就回落到 8321。
if not defined TALEAI_PORT set "TALEAI_PORT=8321"
set "PORT_OK="
for /f "delims=0123456789" %%a in ("!TALEAI_PORT!") do set "PORT_OK=1"
if defined PORT_OK (
  echo [警告] TALEAI_PORT="!TALEAI_PORT!" 不是纯数字，改用 8321。
  set "TALEAI_PORT=8321"
) else if !TALEAI_PORT! LSS 1 set "TALEAI_PORT=8321"
if !TALEAI_PORT! GTR 65535 (
  echo [警告] TALEAI_PORT 超出 1-65535，改用 8321。
  set "TALEAI_PORT=8321"
)

REM ---- 1) 选 Python：优先项目 venv ----
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
  echo [1/4] 使用项目虚拟环境 .venv
  goto :have_py
)

echo [1/4] 没找到 .venv，尝试创建...
where uv >nul 2>nul
if !errorlevel!==0 (
  echo       用 uv 建虚拟环境...
  uv venv --seed .venv
) else (
  where python >nul 2>nul
  if !errorlevel!==0 (
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
if !errorlevel!==0 (
  echo       依赖就绪
) else (
  echo       缺依赖，正在安装...
  where uv >nul 2>nul
  if !errorlevel!==0 (
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
REM 只看本机监听。匹配 ":<port> " 后缀——netstat 的本地地址列可能是
REM 127.0.0.1 / 0.0.0.0 / [::] / [::1]，不能只认 127.0.0.1。
echo [3/4] 检查端口 !TALEAI_PORT! ...
set "PORT_BUSY="
for /f "tokens=*" %%l in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":!TALEAI_PORT! "') do set "PORT_BUSY=1"
if defined PORT_BUSY (
  echo.
  echo [错误] 端口 !TALEAI_PORT! 已被占用——多半是塔利已经在跑了。
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
echo   聊天页：http://127.0.0.1:!TALEAI_PORT!/
echo   日志页：http://127.0.0.1:!TALEAI_PORT!/logs
echo   设置页：http://127.0.0.1:!TALEAI_PORT!/settings
echo.
echo   （按 Ctrl-C 停止；关掉本窗口也会停止）
echo --------------------------------------------
"%PY%" main.py
set "APP_RC=!errorlevel!"
echo.
if !APP_RC!==0 (
  echo 塔利已正常退出。
) else (
  echo 塔利退出，返回码 !APP_RC!（启动失败时看上面的报错）。
)
pause
exit /b !APP_RC!

:fail
echo.
pause
exit /b 1
