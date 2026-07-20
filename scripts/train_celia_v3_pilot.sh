#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
steps="${1:-600}"
dataset="${root}/training/datasets/celia-v3-v1/train-v2-enhanced"
captions="${root}/training/datasets/celia-v3-v1/captions-v2-enhanced"
output="${root}/training/runs/celia-v3-v1-512px-pilot"

if [[ ! -d "${dataset}" ]]; then
  echo "missing dataset: ${dataset}" >&2
  exit 1
fi

image_count="$(find "${dataset}" -maxdepth 1 -type f \( -name '*.png' -o -name '*.jpg' -o -name '*.jpeg' \) | wc -l | tr -d ' ')"
caption_count="$(find "${captions}" -maxdepth 1 -type f -name '*.txt' | wc -l | tr -d ' ')"
if [[ "${image_count}" != "16" || "${caption_count}" != "16" ]]; then
  echo "expected exactly 16 image/caption pairs, got ${image_count}/${caption_count}" >&2
  exit 1
fi

exec "${root}/.venv-train/bin/python" \
  "${root}/external/diffusers/examples/dreambooth/train_dreambooth_lora_sdxl.py" \
  --pretrained_model_name_or_path stabilityai/stable-diffusion-xl-base-1.0 \
  --instance_data_dir "${dataset}" \
  --instance_prompt "a photo of celia_v3, an adult East Asian woman" \
  --output_dir "${output}" \
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
  --checkpointing_steps=200 \
  --checkpoints_total_limit=3 \
  --dataloader_num_workers=0 \
  --seed=42 \
  --output_kohya_format \
  --report_to=tensorboard \
  --mixed_precision=fp16
