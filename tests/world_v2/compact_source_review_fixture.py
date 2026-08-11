"""Deterministic compact source-review wires for integration fixtures.

These helpers exercise the production visible-Beat contract without restoring
the retired Inventory/full-V7 chat topology. They are deliberately test-only:
production still requires the configured DeepSeek Flash guard.
"""

from __future__ import annotations

import json
from collections.abc import Iterable


def compact_source_review_wire(
    messages: list[dict[str, str]],
    *,
    unclosed_texts: Iterable[str] = (),
) -> str:
    """Return one exhaustive compact verdict for the host-pinned Beats."""

    request = json.loads(messages[1]["content"])
    output_contract = request.get("output_contract", {})
    assert output_contract.get("contract") == "visible-beat-source-verdict.1"
    source_references = request.get("source_references", [])
    blocked = tuple(unclosed_texts)
    decisions: list[dict[str, object]] = []
    for beat in request.get("visible_beats", []):
        text = str(beat["text"])
        if any(fragment in text for fragment in blocked):
            decision = {
                "verdict": "unclosed",
                "semantic_role": "external_proposition",
                "subject_role": "companion",
                "source_ref_indexes": [],
            }
        elif source_references:
            subject_role = source_references[0].get("subject_role")
            if subject_role not in {"companion", "counterpart", "general"}:
                subject_role = "companion"
            decision = {
                "verdict": "closed",
                "semantic_role": "external_proposition",
                "subject_role": subject_role,
                "source_ref_indexes": [0],
            }
        else:
            decision = {
                "verdict": "source_free",
                "semantic_role": "private_state",
                "subject_role": "companion",
                "source_ref_indexes": [],
            }
        decisions.append({"beat_index": beat["beat_index"], **decision})
    return json.dumps(
        {
            "contract": "visible-beat-source-verdict.1",
            "decisions": decisions,
        },
        ensure_ascii=False,
    )
