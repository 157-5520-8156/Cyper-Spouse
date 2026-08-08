"""Small orchestration result owned by the unified CharacterInterior seam.

This type deliberately contains no model port, prompt, or semantic author. It
lets platform-neutral schedulers report one source-bound Interior work unit
without importing the retired independent Appraisal/Affect runtimes merely to
borrow their result classes.
"""

from __future__ import annotations

from typing import Literal

from ..schema_core import FrozenModel


class CharacterInteriorRunResult(FrozenModel):
    """Outcome of one durable CharacterInterior background settlement unit."""

    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "completed_existing", "processed"]
    work_status: Literal[
        "no_proposal",
        "no_change",
        "accepted",
        "advisory_validation_rejected",
        "technical_failure",
    ] | None = None


__all__ = ["CharacterInteriorRunResult"]
