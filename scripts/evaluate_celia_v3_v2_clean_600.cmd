@echo off
setlocal
set "ROOT=C:\Users\Administrator\Projects\Girl-Agent"
set "RUN=%ROOT%\training\runs\celia-v3-v2-clean-text-600"
set "CONDA=C:\Users\Administrator\anaconda3\Scripts\conda.exe"
set "ENV=%ROOT%\.conda-train"
set "SCRIPT=%ROOT%\training\evaluations\evaluate_celia_v3_checkpoints.py"

"%CONDA%" run -p "%ENV%" python "%SCRIPT%" --run "%RUN%" --scale 0.7 > "%RUN%\logs\evaluation.log" 2>&1
echo %ERRORLEVEL% > "%RUN%\evaluation.exit-code"
