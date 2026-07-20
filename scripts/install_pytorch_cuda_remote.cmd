@echo off
setlocal
set "ROOT=C:\Users\Administrator\Projects\Girl-Agent"
set "PYTHON=%ROOT%\.conda-train\python.exe"
set "LOGDIR=%ROOT%\training\setup-logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
"%PYTHON%" -m pip install --force-reinstall --index-url https://download.pytorch.org/whl/cu128 torch==2.9.0 torchvision==0.24.0 > "%LOGDIR%\pytorch-install.out.log" 2> "%LOGDIR%\pytorch-install.err.log"
echo %ERRORLEVEL% > "%LOGDIR%\pytorch-install.exit-code"
