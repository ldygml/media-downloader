@echo off
title MediaSaver 本地服务
cd /d "%~dp0"

echo.
echo ============================================
echo   MediaSaver 本地服务
echo ============================================
echo.

if exist "dist\MediaSaver.exe" (
    echo   找到打包版，启动中...
    start /min "" "dist\MediaSaver.exe"
    goto :started
)

if exist "MediaSaver.exe" (
    echo   找到打包版，启动中...
    start /min "" "MediaSaver.exe"
    goto :started
)

where python >nul 2>nul
if errorlevel 1 (
    echo   [错误] 没找到 Python
    pause
    exit /b 1
)
echo   用 Python 启动服务...
start /min "" python app.py

:started
echo.
echo   服务已启动（最小化窗口）
echo   浏览器打开 http://localhost:8932
echo.
pause
