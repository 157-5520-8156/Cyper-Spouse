"""Stable logical identity for the one protagonist semantic author.

Physical provider routes and independent source reviewers are deliberately not
part of this identity.  A purpose Faculty may have a different wire/route name
while still representing the same character author; production composition
must nevertheless resolve every such Faculty to one logical identity.
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping


AUTHOR_IDENTITY_CONTRACT = "character-semantic-author-identity.1"


def character_semantic_author_identity(
    *,
    model_id: str,
    model_version: str,
) -> dict[str, str]:
    """Return the canonical identity shared by every protagonist purpose."""

    normalized_model_id = model_id.strip()
    normalized_model_version = model_version.strip()
    if not normalized_model_id or not normalized_model_version:
        raise ValueError("character semantic author model identity is incomplete")
    material = {
        "contract": AUTHOR_IDENTITY_CONTRACT,
        "model_id": normalized_model_id,
        "model_version": normalized_model_version,
    }
    digest = hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        **material,
        "semantic_author_id": f"character-semantic-author:sha256:{digest}",
    }


def supplied_semantic_author_id(identity: Mapping[str, object]) -> str | None:
    """Read a declared logical identity without inferring reviewer authority."""

    value = identity.get("semantic_author_id")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


__all__: list[str] = []
