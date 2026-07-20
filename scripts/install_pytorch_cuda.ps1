param(
  [string]$Root = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $Root '.conda-train\python.exe'
$logDirectory = Join-Path $Root 'training\setup-logs'
$stdout = Join-Path $logDirectory 'pytorch-install.out.log'
$stderr = Join-Path $logDirectory 'pytorch-install.err.log'
$pidFile = Join-Path $logDirectory 'pytorch-install.pid'

if (-not (Test-Path $python)) { throw "missing training Python: $python" }
$null = New-Item -ItemType Directory -Force $logDirectory

$arguments = @(
  '-m', 'pip', 'install', '--force-reinstall',
  '--index-url', 'https://download.pytorch.org/whl/cu128',
  'torch==2.9.0', 'torchvision==0.24.0'
)
$process = Start-Process -FilePath $python -ArgumentList $arguments `
  -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
$process.Id | Set-Content -NoNewline $pidFile
Write-Output "started PyTorch CUDA installer (pid $($process.Id)); logs: $logDirectory"
