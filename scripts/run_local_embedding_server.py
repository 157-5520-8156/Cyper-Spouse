#!/usr/bin/env python3
"""OpenAI-compatible local embedding server (MLX + bge).

Exposes POST /v1/embeddings so the daemon's OpenAICompatibleRecallEmbedding
can point at it via WORLD_V2_RECALL_EMBEDDING_BASE_URL without code changes.
Vectors are returned raw; recall_index normalizes them before cosine search.
"""
from __future__ import annotations

import argparse
from typing import Any

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel, Field

import mlx_embeddings as me

app = FastAPI(title="girl-agent-local-embeddings")
_MODEL: Any = None
_TOKENIZER: Any = None
_MODEL_NAME = ""
_DIM = 0


class EmbedRequest(BaseModel):
    model: str = ""
    input: str | list[str] = Field(...)


class EmbeddingData(BaseModel):
    object: str = "embedding"
    index: int = 0
    embedding: list[float]


@app.post("/v1/embeddings")
async def embeddings(req: EmbedRequest) -> dict[str, Any]:
    texts = [req.input] if isinstance(req.input, str) else req.input
    out = me.generate(_MODEL, _TOKENIZER, texts)
    # bge family uses the [CLS] token as the sentence vector.
    lhs = out.last_hidden_state
    cls = lhs[:, 0, :]
    arr = np.asarray(cls.tolist(), dtype="float64")
    # L2-normalize (recall_index normalizes again; idempotent).
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    arr = arr / np.maximum(norms, 1e-9)
    data = [
        EmbeddingData(index=i, embedding=arr[i].tolist())
        for i in range(arr.shape[0])
    ]
    return {
        "object": "list",
        "data": [d.model_dump() for d in data],
        "model": _MODEL_NAME,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="mlx-community/bge-m3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8190)
    args = parser.parse_args()

    import uvicorn

    global _MODEL, _TOKENIZER, _MODEL_NAME, _DIM
    print(f"loading {args.model} ...", flush=True)
    _MODEL, _TOKENIZER = me.load(args.model)
    _MODEL_NAME = args.model
    probe = me.generate(_MODEL, _TOKENIZER, ["ping"])
    _DIM = int(probe.last_hidden_state.shape[-1])
    print(f"READY model={args.model} dim={_DIM} on http://{args.host}:{args.port}/v1/embeddings", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
