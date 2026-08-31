@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

rem ============================================================
rem  政策抓取入库运维工具 - 启动脚本
rem  关闭本窗口或按 Ctrl+C 即可停止服务
rem ============================================================

rem ---------- 前置检查 ----------
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. 请先运行 start-with-venv.bat 初始化虚拟环境。
  goto :error_exit
)
if not exist ".env" (
  echo [ERROR] .env not found. 请复制 .env.example 为 .env 并完成配置。
  goto :error_exit
)

rem ---------- 读取 .env 配置（未配置则用默认值） ----------
set "BIND_HOST=127.0.0.1"
set "BIND_PORT=8000"
set "SCHEDULE_HOUR=1"
set "SCHEDULE_MINUTE=0"
set "ADMIN_USER="
set "ADMIN_PASSWORD="
for /f "tokens=1,* delims==" %%A in ('findstr /b /i "CRAWLER_BIND_HOST=" ".env"') do set "BIND_HOST=%%B"
for /f "tokens=1,* delims==" %%A in ('findstr /b /i "CRAWLER_BIND_PORT=" ".env"') do set "BIND_PORT=%%B"
for /f "tokens=1,* delims==" %%A in ('findstr /b /i "CRAWLER_SCHEDULE_HOUR=" ".env"') do set "SCHEDULE_HOUR=%%B"
for /f "tokens=1,* delims==" %%A in ('findstr /b /i "CRAWLER_SCHEDULE_MINUTE=" ".env"') do set "SCHEDULE_MINUTE=%%B"
for /f "tokens=1,* delims==" %%A in ('findstr /b /i "CRAWLER_ADMIN_USER=" ".env"') do set "ADMIN_USER=%%B"
for /f "tokens=1,* delims==" %%A in ('findstr /b /i "CRAWLER_ADMIN_PASSWORD=" ".env"') do set "ADMIN_PASSWORD=%%B"
rem 小时/分钟补零，如 1:0 显示为 01:00
set "SCHEDULE_HOUR=0%SCHEDULE_HOUR%"
set "SCHEDULE_HOUR=%SCHEDULE_HOUR:~-2%"
set "SCHEDULE_MINUTE=0%SCHEDULE_MINUTE%"
set "SCHEDULE_MINUTE=%SCHEDULE_MINUTE:~-2%"

rem 0.0.0.0 / :: 不能直接访问，展示给用户的是本机回环地址
set "BROWSE_HOST=%BIND_HOST%"
if "%BIND_HOST%"=="0.0.0.0" set "BROWSE_HOST=127.0.0.1"
if "%BIND_HOST%"=="::" set "BROWSE_HOST=127.0.0.1"

rem ---------- Release the configured listener port before startup ----------
powershell -NoProfile -Command "$listeners=@(Get-NetTCPConnection -State Listen -LocalPort %BIND_PORT% -ErrorAction SilentlyContinue); if($listeners){$pids=@(); foreach($listener in $listeners){$listenerPid=[int]$listener.OwningProcess; if($pids -notcontains $listenerPid){$pids += $listenerPid}}; Write-Host ('[INFO] Stopping existing listener on port %BIND_PORT% (PID ' + ($pids -join ', ') + ') ...'); Stop-Process -Id $pids -Force -ErrorAction Stop; Start-Sleep -Seconds 1}; if(Get-NetTCPConnection -State Listen -LocalPort %BIND_PORT% -ErrorAction SilentlyContinue){exit 1}"
if errorlevel 1 (
  echo [ERROR] Port %BIND_PORT% is still in use; service was not started.
  goto :error_exit
)
title 政策抓取入库运维工具 - http://%BROWSE_HOST%:%BIND_PORT%/

echo ================================================================
echo   政策抓取入库运维工具 v1.0.0
echo   ------------------------------------------------------------
echo   访问地址 : http://%BROWSE_HOST%:%BIND_PORT%/
echo   健康检查 : http://%BROWSE_HOST%:%BIND_PORT%/api/health
echo   定时任务 : 每天 %SCHEDULE_HOUR%:%SCHEDULE_MINUTE% 自动抓取（可在 .env 调整）
echo   日志目录 : %~dp0logs
if not "%BIND_HOST%"=="127.0.0.1" if not "%BIND_HOST%"=="localhost" (
  if "%ADMIN_USER%"=="" (
    echo   [警告] 非本机监听必须配置 CRAWLER_ADMIN_USER 和 CRAWLER_ADMIN_PASSWORD，否则启动失败
  ) else (
    echo   登录账号 : %ADMIN_USER% （Basic 认证）
  )
)
echo   ------------------------------------------------------------
echo   关闭本窗口或按 Ctrl+C 即可停止服务
echo ================================================================
echo.

rem ---------- 启动服务 ----------
".venv\Scripts\python.exe" main.py
set "EXIT_CODE=%ERRORLEVEL%"
echo.

if "%EXIT_CODE%"=="0" (
  echo [INFO] 服务已停止（exit code = 0）
) else (
  echo [ERROR] 服务异常退出（exit code = %EXIT_CODE%）
)
echo 按任意键关闭窗口...
pause >nul
exit /b %EXIT_CODE%

:error_exit
echo.
echo 按任意键关闭窗口...
pause >nul
exit /b 1
