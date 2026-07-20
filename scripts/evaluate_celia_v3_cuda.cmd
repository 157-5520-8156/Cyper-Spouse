@echo off
setlocal
set "ROOT=C:\Users\Administrator\Projects\Girl-Agent"
set "RUN=%ROOT%\training\runs\celia-v3-v1-512px-pilot-cuda-600-r2"
set "CONDA=C:\Users\Administrator\anaconda3\Scripts\conda.exe"
set "ENV=%ROOT%\.conda-train"

"%CONDA%" run -p "%ENV%" python "%ROOT%\training\evaluations\evaluate_celia_v3_checkpoints.py" --run "%RUN%" --scale 0.7 > "%RUN%\evaluation.log" 2>&1
echo %ERRORLEVEL% > "%RUN%\evaluation.exit-code"
