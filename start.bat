@echo off
setlocal
cd /d "%~dp0"

REM Cursor bridge: pick GGUF, start llama-server, print tunnel hints.
REM Tuning: config.json -> server: fit_margin, fit_ctx, ngl, ctx, extra_args.
REM Env overrides: CODER_AGENT_NGL / CODER_AGENT_CTX / CURSOR_PROXY_TOKEN.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH
    pause
    exit /b 1
)

python -c "import rich" >nul 2>&1
if errorlevel 1 (
    echo Installing dependency: rich ...
    python -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo [ERROR] pip install failed. Run manually: python -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

python "%~dp0chat.py" %*
set "CHAT_EXIT=%ERRORLEVEL%"

echo.
echo Done.
if not "%CHAT_EXIT%"=="0" pause
exit /b %CHAT_EXIT%
