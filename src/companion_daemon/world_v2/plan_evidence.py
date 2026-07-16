"""Canonical, replay-stable evidence digests for Activity Plan authority.

Catalogs, proposal compilers, acceptance recorders, and reducers all need to
prove the same plan bytes.  This pure module prevents them from acquiring
slightly different local hashing rules over time.
"""

from __future__ import annotations

import hashlib
import json

from .schemas import PlanStateProjection


def canonical_plan_evidence_hash(plan: PlanStateProjection) -> str:
    """Return the exact hash accepted for an ``active_plan`` evidence ref."""

    excluded: set[str] = set()
    if plan.owner_actor_ref == "legacy:unknown-owner":
        excluded = {"owner_actor_ref", "authority_origin"}
    encoded = json.dumps(
        plan.model_dump(mode="json", exclude=excluded),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["canonical_plan_evidence_hash"]
