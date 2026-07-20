param(
  [int]$Steps = 600,
  [string]$Root = (Split-Path -Parent $PSScriptRoot),
  [string]$PythonPath = '',
  [string]$BaseModelPath = 'stabilityai/stable-diffusion-xl-base-1.0',
  [string]$OutputDir = '',
  [string]$DatasetDir = '',
  [switch]$TrainTextEncoder,
  [double]$TextEncoderLearningRate = 0.000005
)

# Diffusers writes normal progress logs to stderr. Treat only Python's exit code,
# not those log lines, as the training failure signal.
$ErrorActionPreference = 'Continue'
Set-Location $Root

$dataset = if ($DatasetDir) { $DatasetDir } else { Join-Path $Root 'training\datasets\celia-v3-v1\train-v2-enhanced' }
$output = if ($OutputDir) { $OutputDir } else { Join-Path $Root 'training\runs\celia-v3-v1-512px-pilot-cuda' }
$logDirectory = Join-Path $output 'logs'
$logPath = Join-Path $logDirectory 'train.log'
$python = if ($PythonPath) { $PythonPath } else { Join-Path $Root '.venv-train\Scripts\python.exe' }
$trainer = Join-Path $Root 'external\diffusers\examples\dreambooth\train_dreambooth_lora_sdxl.py'

if (-not (Test-Path $python)) { throw "missing training Python: $python" }
if (-not (Test-Path $trainer)) { throw "missing Diffusers training script: $trainer" }

# Directly executing a Conda environment's python.exe skips activation hooks.
# Prepend its DLL directory so CUDA loads the matching cuDNN rather than an
# unrelated system copy.
$environmentRoot = Split-Path -Parent $python
$condaLibraryBin = Join-Path $environmentRoot 'Library\bin'
if (Test-Path $condaLibraryBin) {
  $env:PATH = "$condaLibraryBin;$env:PATH"
}

$images = @(Get-ChildItem $dataset -File | Where-Object { $_.Extension -in '.png', '.jpg', '.jpeg' })
if ($images.Count -lt 8) { throw "expected at least 8 training images, found $($images.Count)" }

$null = New-Item -ItemType Directory -Force $logDirectory
$env:PYTORCH_CUDA_ALLOC_CONF = 'expandable_segments:True'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$arguments = @(
  $trainer,
  '--pretrained_model_name_or_path', $BaseModelPath,
  '--instance_data_dir', $dataset,
  '--instance_prompt', 'a photo of celia_v3, an adult East Asian woman',
  '--output_dir', $output,
  '--resolution', '512',
  '--center_crop',
  '--train_batch_size', '1',
  '--gradient_accumulation_steps', '1',
  '--gradient_checkpointing',
  '--rank', '8',
  '--learning_rate', '1e-4',
  '--lr_scheduler', 'constant',
  '--lr_warmup_steps', '0',
  '--max_train_steps', $Steps,
  '--checkpointing_steps', '200',
  '--checkpoints_total_limit', '3',
  '--dataloader_num_workers', '0',
  '--seed', '42',
  '--output_kohya_format',
  '--report_to', 'tensorboard',
  '--mixed_precision', 'fp16',
  '--allow_tf32'
)
if ($TrainTextEncoder) {
  $arguments += '--train_text_encoder'
  $arguments += '--text_encoder_lr'
  $arguments += $TextEncoderLearningRate
}

& $python @arguments *>&1 | Tee-Object -FilePath $logPath -Append

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
