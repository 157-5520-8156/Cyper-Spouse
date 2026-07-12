#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
steps="${1:-10}"

exec "${root}/.venv-train/bin/python" \
  "${root}/external/diffusers/examples/dreambooth/train_dreambooth_lora_sdxl.py" \
  --pretrained_model_name_or_path stabilityai/stable-diffusion-xl-base-1.0 \
  --instance_data_dir "${root}/training/datasets/celia-v2-v0.1" \
  --instance_prompt "a photo of celia_v2, a young adult Chinese woman" \
  --output_dir "${root}/training/runs/celia-v2-v0.1-smoke" \
  --resolution=512 \
  --center_crop \
  --train_batch_size=1 \
  --gradient_accumulation_steps=1 \
  --gradient_checkpointing \
  --rank=8 \
  --learning_rate=1e-4 \
  --lr_scheduler=constant \
  --lr_warmup_steps=0 \
  --max_train_steps="${steps}" \
  --checkpointing_steps="${steps}" \
  --checkpoints_total_limit=1 \
  --dataloader_num_workers=0 \
  --seed=42 \
  --output_kohya_format \
  --report_to=tensorboard \
  --mixed_precision=fp16
