# Celia v2 Local LoRA Training

The first training experiment is an SDXL DreamBooth LoRA smoke test on the
local Apple Silicon machine. It uses only six high-face-coverage, everyday
identity images; it intentionally excludes the legacy v1 references, full-body
style candidates, and relationship-only assets.

## Dataset v0.1

- canonical portrait
- no-hair-clip identity check
- 10-degree angle progression
- two everyday close selfie candidates
- everyday loose low-bun portrait

The trigger phrase is `celia_v2`. The loose-bun image is a hairstyle state of
the canonical shoulder-length hair, not an additional long-hair identity.

## Run the smoke test

```bash
scripts/train_celia_v2_smoke.sh
```

It defaults to 10 steps at 512px only to validate MPS memory, model access,
and artifact export. A successful smoke test is not a usable LoRA. After it
passes, rerun at a larger step count and evaluate generated identity prompts
before adopting any artifact.

The script intentionally starts the official training script directly rather
than through `accelerate launch`: direct single-process MPS execution produced
the verified local checkpoint and exported both Diffusers and Kohya-format
weights in the first smoke test.
