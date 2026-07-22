@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set "BIN_DIR=%~dp0bin"
set "MODEL_DIR=%~dp0models"
set "HOST=127.0.0.1"
set "PORT=8080"
set "NGL=99"
set "CTX=40000"
set "SERVER_PID="

if not exist "%BIN_DIR%\llama-server.exe" (
    echo [错误] 找不到 llama-server.exe: %BIN_DIR%\llama-server.exe
    pause
    exit /b 1
)

where python >nul 2>&1
if errorlevel 1 (
    echo [错误] 找不到 python，请先安装 Python 并加入 PATH
    pause
    exit /b 1
)

if not exist "%MODEL_DIR%" (
    echo [错误] 找不到 models 目录: %MODEL_DIR%
    pause
    exit /b 1
)

echo.
echo ========================================
echo   本地模型终端对话
echo ========================================
echo.
echo 可用模型:
echo.

set "COUNT=0"
for %%F in ("%MODEL_DIR%\*.gguf") do (
    set /a COUNT+=1
    set "MODEL_!COUNT!=%%~fF"
    set "NAME_!COUNT!=%%~nxF"
    echo   !COUNT!. %%~nxF
)

if %COUNT%==0 (
    echo [错误] models 目录下没有 .gguf 模型文件
    pause
    exit /b 1
)

echo.
set /p CHOICE=请输入序号选择模型 [1-%COUNT%]: 

echo %CHOICE%| findstr /r "^[1-9][0-9]*$" >nul
if errorlevel 1 (
    echo [错误] 无效输入
    pause
    exit /b 1
)

if %CHOICE% LSS 1 (
    echo [错误] 序号超出范围
    pause
    exit /b 1
)
if %CHOICE% GTR %COUNT% (
    echo [错误] 序号超出范围
    pause
    exit /b 1
)

set "SELECTED=!MODEL_%CHOICE%!"
set "SELECTED_NAME=!NAME_%CHOICE%!"

echo.
echo 已选择: %SELECTED_NAME%
echo 地址:   http://%HOST%:%PORT%
echo GPU层:  %NGL%  ^|  上下文: %CTX%
echo 能力:   本地文件读写 / 命令执行（chat.py 工具）
echo.
echo 正在后台启动 llama-server ...
echo.

REM 清理可能残留的旧服务
taskkill /F /IM llama-server.exe >nul 2>&1

start "llama-server" /MIN "%BIN_DIR%\llama-server.exe" ^
  -m "%SELECTED%" ^
  --host %HOST% ^
  --port %PORT% ^
  -ngl %NGL% ^
  -c %CTX% ^
  --jinja

python "%~dp0chat.py"
set "CHAT_EXIT=%ERRORLEVEL%"

echo.
echo 正在关闭后台服务...
taskkill /F /IM llama-server.exe >nul 2>&1

echo 已结束。
if not "%CHAT_EXIT%"=="0" pause
exit /b %CHAT_EXIT%
