@echo off
setlocal
set "ROOT=C:\Users\Administrator\Projects\Girl-Agent"
set "OUTPUT=%ROOT%\training\runs\celia-v3-debug-2step-scheduled"
set "MODEL=C:\Users\Administrator\.cache\huggingface\hub\models--stabilityai--stable-diffusion-xl-base-1.0\snapshots\462165984030d82259a11f4367a4eed129e94a7b"
set "PYTHON=%ROOT%\.conda-train\python.exe"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\train_celia_v3_pilot.ps1" -Steps 2 -PythonPath "%PYTHON%" -BaseModelPath "%MODEL%" -OutputDir "%OUTPUT%"
echo %ERRORLEVEL% > "%OUTPUT%\training.exit-code"
