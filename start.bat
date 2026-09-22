@echo off
setlocal
cd /d "%~dp0"

REM Model selection, llama-server startup (VRAM auto-fit, mmproj, error output)
REM and shutdown are all handled inside Python now: see agent/server.py.
REM Tuning lives in config.json -> "server": { fit_margin, fit_ctx, ngl, ctx, extra_args }.
REM Environment overrides still work: CODER_AGENT_NGL / CODER_AGENT_CTX / CODER_AGENT_PROVIDER.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH
    pause
    exit /b 1
)

python -c "import rich, prompt_toolkit, playwright.sync_api" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies: rich, prompt_toolkit, playwright ...
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
