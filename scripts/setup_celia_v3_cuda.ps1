param(
  [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
Set-Location $Root

$python = 'python'
$venv = Join-Path $Root '.venv-train'
if (-not (Test-Path $venv)) {
  & $python -m venv $venv
}

$venvPython = Join-Path $venv 'Scripts\python.exe'
& $venvPython -m pip install --upgrade pip

# CUDA 12.8 wheels are supported on Windows by the installed NVIDIA driver.
& $venvPython -m pip install torch==2.9.0 torchvision==0.24.0 --index-url https://download.pytorch.org/whl/cu128
& $venvPython -m pip install -e (Join-Path $Root 'external\diffusers')
& $venvPython -m pip install 'accelerate>=1.14,<2' 'transformers>=5.13,<6' 'peft>=0.19,<1' 'safetensors>=0.8' tensorboard ftfy

& $venvPython -c "import torch; assert torch.cuda.is_available(), 'CUDA is not available'; print(torch.__version__, torch.cuda.get_device_name(0))"
