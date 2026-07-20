@echo off
setlocal
set "ROOT=C:\Users\Administrator\Projects\Girl-Agent"
set "RUN=%ROOT%\training\runs\celia-v3-v1-512px-pilot-cuda-600-r2"
set "CONDA=C:\Users\Administrator\anaconda3\Scripts\conda.exe"
set "ENV=%ROOT%\.conda-train"
set "SCRIPT=%ROOT%\training\evaluations\evaluate_celia_v3_checkpoints.py"

"%CONDA%" run -p "%ENV%" python "%SCRIPT%" --run "%RUN%" --checkpoint-steps 600 --case-ids 01-canonical-front,02-opposite-three-quarter --scale 1.0 --output-dir "%RUN%\evaluation-scale-100" > "%RUN%\evaluation-scale-100.log" 2>&1
"%CONDA%" run -p "%ENV%" python "%SCRIPT%" --run "%RUN%" --checkpoint-steps 600 --case-ids 01-canonical-front,02-opposite-three-quarter --scale 1.3 --output-dir "%RUN%\evaluation-scale-130" > "%RUN%\evaluation-scale-130.log" 2>&1
echo %ERRORLEVEL% > "%RUN%\evaluation-scales.exit-code"
