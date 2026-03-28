@echo off
REM ═══════════════════════════════════════════════════════════
REM  WB Election Intel — Quick Launcher (Windows)
REM  Double-click this file to start
REM ═══════════════════════════════════════════════════════════

cd /d "%~dp0"

echo.
echo   ╔══════════════════════════════════════════════╗
echo   ║  WB Election Intel — Bankura 2026 Launcher  ║
echo   ╚══════════════════════════════════════════════╝
echo.

REM Check for Python 3
python --version >nul 2>&1
if %errorlevel% neq 0 (
    py -3 --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo   ERROR: Python 3 not found on this computer.
        echo.
        echo   Download it from: https://www.python.org/downloads/
        echo   Make sure to tick "Add Python to PATH" during install.
        echo.
        pause
        exit /b 1
    )
    set PYTHON=py -3
) else (
    set PYTHON=python
)

echo   Python found. Starting proxy...
echo.

REM Pass API key as first argument if provided
if "%~1" neq "" (
    %PYTHON% proxy.py %1
) else (
    %PYTHON% proxy.py
)

pause
