@echo off
title MediaSaver - Cloudflare 远程隧道
cd /d "%~dp0"
set SERVER_STARTED=0
echo.
echo ============================================
echo   MediaSaver 远程隧道
echo ============================================
echo.
:: Step 1: 启动本地服务
echo [1/3] 启动本地服务...

if exist "dist\MediaSaver.exe" (
    echo   - 找到打包版
    set SERVER_STARTED=1
    start /min "" "dist\MediaSaver.exe"
) else if exist "MediaSaver.exe" (
    echo   - 找到打包版
    set SERVER_STARTED=1
    start /min "" "MediaSaver.exe"
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo   [错误] 没找到 Python
        pause
        exit /b 1
    )
    echo   - 用 Python 启动
    start /min "" python app.py
    set SERVER_STARTED=1
)

if %SERVER_STARTED% equ 0 (
    echo   [错误] 启动失败
    pause
    exit /b 1
)

:: 等几秒让服务启动
echo   - 等待服务就绪...
ping -n 5 127.0.0.1 >nul

:: Step 2: 检查 cloudflared
echo.
echo [2/3] 检查 cloudflared...

set CF_PATH=
if exist "cloudflared.exe" (
    set "CF_PATH=cloudflared.exe"
) else if exist "dist\cloudflared.exe" (
    set "CF_PATH=dist\cloudflared.exe"
) else (
    echo.
    echo   [错误] 找不到 cloudflared.exe
    pause
    exit /b 1
)
echo   - 已找到 cloudflared

:: Step 3: 建立隧道（后台运行，日志写入临时文件）
cls
echo ============================================
echo   MediaSaver 远程隧道
echo ============================================
echo.
echo   正在建立隧道，请稍候...
echo.

set CF_LOG=%TEMP%\mediasaver_tunnel.log
if exist "%CF_LOG%" del /q "%CF_LOG%"

start /b "" "%CF_PATH%" tunnel --url http://localhost:8932 --no-autoupdate > "%CF_LOG%" 2>&1

:: 循环等待网址出现（最多 30 秒）
set TUNNEL_URL=
for /l %%i in (1,1,60) do (
    for /f "usebackq delims=" %%u in (`findstr /r /i "https://[a-z0-9-]*\.trycloudflare\.com" "%CF_LOG%" 2^>nul`) do set "TUNNEL_URL=%%u"
    if defined TUNNEL_URL goto :url_found
    ping -n 2 127.0.0.1 >nul
)

echo   [错误] 30 秒内未获取到隧道地址
echo   请检查网络连接或 cloudflared 是否正常
echo.
pause
exit /b 1

:url_found
:: 提取纯 URL 地址
for /f "usebackq delims=" %%u in (`powershell -NoProfile -Command "(Select-String -Path '%CF_LOG%' -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches).Matches[0].Value" 2^>nul`) do set "TUNNEL_URL=%%u"

echo.
echo ============================================
echo   手机访问地址:
echo.
echo   %TUNNEL_URL%
echo.
echo ============================================
echo.
echo   按任意键 = 关闭隧道并退出
echo.
pause
:: 关闭隧道进程
taskkill /f /im cloudflared.exe >nul 2>nul
exit
