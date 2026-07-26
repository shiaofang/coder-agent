@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "BIN_DIR=%~dp0bin"
set "MODEL_DIR=%~dp0models"
set "CFG=%~dp0config.json"
set "HOST=127.0.0.1"
set "PORT=8080"
set "NGL=99"
set "CTX=40000"

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Coder Agent
echo ========================================
echo.
echo Available models:
echo.

set "COUNT=0"
if exist "%MODEL_DIR%" (
    for %%F in ("%MODEL_DIR%\*.gguf") do (
        set /a COUNT+=1
        set "MODEL_!COUNT!=%%~fF"
        set "NAME_!COUNT!=%%~nxF"
        echo   !COUNT!. %%~nxF  [local]
    )
)

set /a CLOUD_INDEX=COUNT+1
set "HAS_CLOUD=0"
if exist "%CFG%" (
    set "HAS_CLOUD=1"
    echo   !CLOUD_INDEX!. Cloud model  [config.json]
)

if %COUNT%==0 (
    if "!HAS_CLOUD!"=="0" (
        echo [ERROR] no .gguf files in models\, and no config.json found
        echo.
        echo   Local:  put a GGUF file into models\
        echo   Cloud:  copy config.example.json to config.json and fill it in
        pause
        exit /b 1
    )
)

set /a MAX_CHOICE=COUNT
if "!HAS_CLOUD!"=="1" set /a MAX_CHOICE=CLOUD_INDEX

:choose_model
echo.
set "CHOICE="
set /p CHOICE=Select model [1-%MAX_CHOICE%]: 

if not defined CHOICE (
    echo [ERROR] enter a number
    goto choose_model
)

echo !CHOICE!| findstr /r "^[1-9][0-9]*$" >nul
if errorlevel 1 (
    echo [ERROR] invalid, enter 1-%MAX_CHOICE%
    goto choose_model
)

if !CHOICE! LSS 1 (
    echo [ERROR] out of range, enter 1-%MAX_CHOICE%
    goto choose_model
)
if !CHOICE! GTR %MAX_CHOICE% (
    echo [ERROR] out of range, enter 1-%MAX_CHOICE%
    goto choose_model
)

if "!HAS_CLOUD!"=="1" (
    if "!CHOICE!"=="!CLOUD_INDEX!" (
        goto use_cloud
    )
)

REM ---- local GGUF path ----
if not exist "%BIN_DIR%\llama-server.exe" (
    echo [ERROR] llama-server.exe not found in bin\
    pause
    exit /b 1
)

set "SELECTED=!MODEL_%CHOICE%!"
set "SELECTED_NAME=!NAME_%CHOICE%!"

if not defined SELECTED (
    echo [ERROR] invalid index
    goto choose_model
)
if not exist "!SELECTED!" (
    echo [ERROR] model file missing
    goto choose_model
)

echo.
echo Selected: !SELECTED_NAME! [local]
echo URL: http://%HOST%:%PORT%
echo.
echo Starting llama-server ...
echo.

taskkill /F /IM llama-server.exe >nul 2>&1

start "llama-server" /MIN "%BIN_DIR%\llama-server.exe" ^
  -m "!SELECTED!" ^
  --host %HOST% ^
  --port %PORT% ^
  -ngl %NGL% ^
  -c %CTX% ^
  --jinja

set "CODER_AGENT_PROVIDER=local"
python "%~dp0chat.py"
set "CHAT_EXIT=!ERRORLEVEL!"

echo.
echo Stopping background service...
taskkill /F /IM llama-server.exe >nul 2>&1

goto done

:use_cloud
echo.
echo Selected: Cloud model  [config.json]
echo.

set "CODER_AGENT_PROVIDER=cloud"
python "%~dp0chat.py"
set "CHAT_EXIT=!ERRORLEVEL!"

:done
echo.
echo Done.
if not "!CHAT_EXIT!"=="0" pause
exit /b !CHAT_EXIT!
