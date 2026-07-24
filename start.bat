@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "BIN_DIR=%~dp0bin"
set "MODEL_DIR=%~dp0models"
set "HOST=127.0.0.1"
set "PORT=8080"
set "NGL=99"
set "CTX=40000"

if not exist "%BIN_DIR%\llama-server.exe" (
    echo [ERROR] llama-server.exe not found
    pause
    exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] python not found in PATH
    pause
    exit /b 1
)

if not exist "%MODEL_DIR%" (
    echo [ERROR] models folder not found
    pause
    exit /b 1
)

echo.
echo ========================================
echo   Local Model Chat
echo ========================================
echo.
echo Available models:
echo.

set "COUNT=0"
for %%F in ("%MODEL_DIR%\*.gguf") do (
    set /a COUNT+=1
    set "MODEL_!COUNT!=%%~fF"
    set "NAME_!COUNT!=%%~nxF"
    echo   !COUNT!. %%~nxF
)

if %COUNT%==0 (
    echo [ERROR] no .gguf files in models\
    pause
    exit /b 1
)

:choose_model
echo.
set "CHOICE="
set /p CHOICE=Select model [1-%COUNT%]: 

if not defined CHOICE (
    echo [ERROR] enter a number
    goto choose_model
)

echo !CHOICE!| findstr /r "^[1-9][0-9]*$" >nul
if errorlevel 1 (
    echo [ERROR] invalid, enter 1-%COUNT%
    goto choose_model
)

if !CHOICE! LSS 1 (
    echo [ERROR] out of range, enter 1-%COUNT%
    goto choose_model
)
if !CHOICE! GTR %COUNT% (
    echo [ERROR] out of range, enter 1-%COUNT%
    goto choose_model
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
echo Selected: !SELECTED_NAME!
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

python "%~dp0chat.py"
set "CHAT_EXIT=!ERRORLEVEL!"

echo.
echo Stopping background service...
taskkill /F /IM llama-server.exe >nul 2>&1

echo Done.
if not "!CHAT_EXIT!"=="0" pause
exit /b !CHAT_EXIT!
