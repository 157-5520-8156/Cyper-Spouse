"""Immutable deployment identity facts for the companion role.

Relationship state is deliberately absent: it is a time-varying, source-bound
CharacterInterior facet, not a stable deployment identity or compatibility
prompt field.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field

from .schema_core import FrozenModel


class CompanionIdentityFrame(FrozenModel):
    companion_name: str = Field(min_length=1, max_length=128)
    companion_aliases: tuple[str, ...] = Field(default=(), max_length=8)
    counterpart_name: str = Field(min_length=1, max_length=128)
    stable_identity_facts: tuple[str, ...] = Field(default=(), max_length=16)
    shared_history_facts: tuple[str, ...] = Field(default=(), max_length=16)
    counterpart_history_facts: tuple[str, ...] = Field(default=(), max_length=16)
    personality_frame: str | None = Field(default=None, max_length=2_048)
    values: tuple[str, ...] = Field(default=(), max_length=16)
    speech_frame: str | None = Field(default=None, max_length=2_048)
    style_rules: tuple[str, ...] = Field(default=(), max_length=16)
    boundaries: tuple[str, ...] = Field(default=(), max_length=16)
    role: str = "virtual_companion"
    not_an_assistant: bool = True


def _digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def companion_identity_source_ref(
    identity: CompanionIdentityFrame,
    *,
    scope: Literal[
        "stable_identity",
        "shared_history",
        "counterpart_history",
    ] = "stable_identity",
) -> str:
    """Return one immutable token for one configured semantic lane."""

    if scope == "stable_identity":
        return "identity-frame:sha256:" + _digest(
            identity.model_dump(
                mode="json",
                exclude={
                    "counterpart_name",
                    "shared_history_facts",
                    "counterpart_history_facts",
                },
                exclude_none=True,
            )
        )
    facts = (
        identity.shared_history_facts
        if scope == "shared_history"
        else identity.counterpart_history_facts
    )
    if not facts:
        raise ValueError(f"identity frame has no configured {scope} facts")
    return f"identity-frame:{scope.replace('_', '-')}:sha256:" + _digest(
        {
            "scope": scope,
            "companion_name": identity.companion_name,
            "counterpart_name": identity.counterpart_name,
            "facts": facts,
        }
    )


def companion_identity_source_refs(
    identity: CompanionIdentityFrame,
) -> dict[str, str]:
    """Expose only deployment facts allowed to authorize visible claims."""

    refs = {"stable_identity": companion_identity_source_ref(identity)}
    if identity.shared_history_facts:
        refs["shared_history"] = companion_identity_source_ref(
            identity,
            scope="shared_history",
        )
    return refs


__all__ = [
    "CompanionIdentityFrame",
    "companion_identity_source_ref",
    "companion_identity_source_refs",
]
