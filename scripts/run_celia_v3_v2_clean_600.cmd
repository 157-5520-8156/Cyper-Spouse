@echo off
setlocal
set ROOT=C:\Users\Administrator\Projects\Girl-Agent
set PYTHON=%ROOT%\.conda-train\python.exe
set MODEL=C:\Users\Administrator\.cache\huggingface\hub\models--stabilityai--stable-diffusion-xl-base-1.0\snapshots\462165984030d82259a11f4367a4eed129e94a7b
set OUTPUT=%ROOT%\training\runs\celia-v3-v2-clean-text-600

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\train_celia_v3_pilot.ps1" -Steps 600 -Root "%ROOT%" -PythonPath "%PYTHON%" -BaseModelPath "%MODEL%" -DatasetDir "%ROOT%\assets\reference" -OutputDir "%OUTPUT%" -TrainTextEncoder
set EXITCODE=%ERRORLEVEL%
> "%OUTPUT%\training.exit-code" echo %EXITCODE%
exit /b %EXITCODE%
