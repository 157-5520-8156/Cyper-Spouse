param(
  [string]$Root = (Split-Path -Parent $PSScriptRoot),
  [string]$StartedAt = (Get-Date).ToString('o'),
  [int]$ExpectedSecondsPerStep = 15
)

$totalSteps = 600
$start = [DateTime]::Parse($StartedAt)
$Host.UI.RawUI.WindowTitle = 'Celia v3 LoRA - CUDA training progress'

while ($true) {
  $elapsed = ((Get-Date) - $start).TotalSeconds
  $step = [Math]::Min($totalSteps - 1, [Math]::Floor($elapsed / $ExpectedSecondsPerStep))
  $percent = [Math]::Min(100, [Math]::Round(($step / $totalSteps) * 100, 1))
  $trainer = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*train_dreambooth_lora_sdxl.py*' }

  Clear-Host
  Write-Host 'Celia v3 LoRA · RTX 4060' -ForegroundColor Cyan
  Write-Progress -Activity 'Training (estimated)' -Status "$step / $totalSteps steps" -PercentComplete $percent
  Write-Host "Estimated: $step / $totalSteps steps  ($percent%)" -ForegroundColor Green
  Write-Host "Elapsed: $([TimeSpan]::FromSeconds($elapsed).ToString('hh\\:mm\\:ss'))" -ForegroundColor DarkGray
  Write-Host ''

  if (-not $trainer -and $elapsed -gt 30) {
    Write-Host 'Training process is no longer running; inspect the training log for final status.' -ForegroundColor Yellow
    break
  }
  Start-Sleep -Seconds 2
}

Write-Host 'This window can be closed.' -ForegroundColor Green
