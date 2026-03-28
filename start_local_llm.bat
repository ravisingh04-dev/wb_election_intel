@echo off
REM ═══════════════════════════════════════════════════════════
REM  WB Election Intel — Local LLM Launcher (Windows)
REM ═══════════════════════════════════════════════════════════

cd /d "%~dp0"

echo.
echo   ╔══════════════════════════════════════════════╗
echo   ║  WB Election Intel — Local LLM Launcher     ║
echo   ╚══════════════════════════════════════════════╝
echo.
echo   Backends available:
echo     1) Ollama   - local, FREE, offline, no API key
echo     2) Groq     - cloud, FREE tier, fast, needs free key
echo     3) Gemini   - cloud, FREE tier, has web search, needs free key
echo.

set /p CHOICE="  Choose backend [1/2/3] (default 1): "
if "%CHOICE%"=="" set CHOICE=1

if "%CHOICE%"=="2" (
    echo.
    echo   Get free Groq key at: https://console.groq.com/
    set /p GKEY="  Enter Groq API key (gsk_...): "
    python proxy_local.py --backend groq --key %GKEY%
    goto end
)

if "%CHOICE%"=="3" (
    echo.
    echo   Get free Gemini key at: https://aistudio.google.com/app/apikey
    set /p GKEY="  Enter Gemini API key (AIza...): "
    python proxy_local.py --backend gemini --key %GKEY%
    goto end
)

REM Default: Ollama
echo.
echo   Checking for Ollama...
where ollama >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Ollama not found. Download and install from:
    echo     https://ollama.com/download/windows
    echo.
    echo   After installing, come back and run this again.
    echo   Then pull a model in Command Prompt:
    echo     ollama pull qwen2.5:7b
    echo.
    pause
    goto end
)

echo   Ollama found.
echo   Your installed models:
    ollama list
    echo.
    echo   Select model:
    echo     1) qwen2.5:7b   - best JSON + Bengali-aware [default]
    echo     2) mistral      - fast, reliable
    echo     3) deepseek-r1  - strong analysis
    echo     4) Enter custom name
    echo.
    set /p MCHOICE="  Choice [1/2/3/4, default 1]: "
    if "%MCHOICE%"=="" set MCHOICE=1
    if "%MCHOICE%"=="1" set MODEL=qwen2.5:7b
    if "%MCHOICE%"=="2" set MODEL=mistral
    if "%MCHOICE%"=="3" set MODEL=deepseek-r1
    if "%MCHOICE%"=="4" set /p MODEL="  Enter model name exactly: "
    if "%MODEL%"=="" set MODEL=qwen2.5:7b
    echo   Using model: %MODEL%

echo   Starting proxy with Ollama / %MODEL%...
python proxy_local.py --backend ollama --model %MODEL%

:end
pause
