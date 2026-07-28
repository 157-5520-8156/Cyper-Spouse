"""Immutable ledger authority for one reviewed biographical timeline."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import Field, model_validator

from .biographical_lifecycle import BiographicalLifecycleDocument
from .schema_core import FrozenModel


def _document_hash(document: BiographicalLifecycleDocument) -> str:
    return hashlib.sha256(
        json.dumps(
            document.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class BiographicalTimelineConfiguredPayload(FrozenModel):
    """The complete reviewed chronology accepted for one World epoch."""

    timeline_id: str = Field(default="biography:primary", min_length=1, max_length=128)
    timezone_name: str = Field(min_length=1, max_length=128)
    document_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    document: BiographicalLifecycleDocument

    @model_validator(mode="after")
    def timeline_is_canonical(self) -> "BiographicalTimelineConfiguredPayload":
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("biographical timeline timezone is unknown") from exc
        if self.document_hash != _document_hash(self.document):
            raise ValueError(
                "biographical timeline document hash does not match its content"
            )
        return self

    @classmethod
    def from_yaml(
        cls, *, path: Path, timezone_name: str
    ) -> "BiographicalTimelineConfiguredPayload | None":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("world seed must be an object")
        document = raw.get("biographical_lifecycle")
        if not isinstance(document, dict):
            return None
        canonical = {
            **document,
            "baseline_context_tags": tuple(
                document.get("baseline_context_tags", ())
            ),
            "academic": {
                **document["academic"],
                "term_windows": tuple(
                    document["academic"].get("term_windows", ())
                ),
                "winter_break_windows": tuple(
                    document["academic"].get("winter_break_windows", ())
                ),
                "summer_break_windows": tuple(
                    document["academic"].get("summer_break_windows", ())
                ),
                "enrollment_context_tags": tuple(
                    document["academic"].get("enrollment_context_tags", ())
                ),
            },
        }
        parsed = BiographicalLifecycleDocument.model_validate(canonical)
        return cls(
            timezone_name=timezone_name,
            document_hash=_document_hash(parsed),
            document=parsed,
        )


BIOGRAPHICAL_TIMELINE_PAYLOAD_MODELS = {
    "BiographicalTimelineConfigured": BiographicalTimelineConfiguredPayload,
}


__all__ = [
    "BIOGRAPHICAL_TIMELINE_PAYLOAD_MODELS",
    "BiographicalTimelineConfiguredPayload",
]
