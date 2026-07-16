"""Source-bound P1 deliberation record for choosing one media candidate.

This contract deliberately stops before image planning.  A model may select a
candidate label, but it cannot freeze an opportunity, allocate image budget,
or reinterpret the candidate's source event.  Those effects belong to the
separate accepted batch that pins this record.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import Field, model_validator

from .media_selection import MediaSelection, media_selection_hash
from .media_v2 import PhotoCandidate, media_digest
from .schema_core import FrozenModel
from .schemas import LedgerProjection


MEDIA_SELECTION_PROPOSAL_POLICY_VERSION = "media-selection-proposal.1"
MEDIA_SELECTION_PROPOSAL_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "contract": MEDIA_SELECTION_PROPOSAL_POLICY_VERSION,
            "candidate": "available_p1_source_bound_candidate",
            "selection": "public_life_share_preview_only",
            "model_can": "choose_one_existing_candidate_or_no_op",
            "model_cannot": ("freeze_opportunity", "allocate_budget", "send_media"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()

# Keep the installed P1 digest readable for persisted proposals.  P2 is an
# additive, independently named ordinary-character lane; its digest makes a
# replayed acceptance prove which selection surface was in force.
MEDIA_SELECTION_P2_PROPOSAL_POLICY_VERSION = "media-selection-proposal.2"
MEDIA_SELECTION_P2_PROPOSAL_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "contract": MEDIA_SELECTION_P2_PROPOSAL_POLICY_VERSION,
            "candidate": "available_source_bound_character_candidate",
            "selection": "ordinary_character_preview_only",
            "model_can": "choose_one_existing_candidate_or_no_op",
            "model_cannot": (
                "freeze_opportunity", "allocate_budget", "send_media",
                "choose_capture_authority", "add_recipient_or_private_basis",
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def media_candidate_authority_hash(candidate: PhotoCandidate) -> str:
    """Hash every persisted candidate coordinate an acceptance must preserve."""

    return media_digest(candidate.model_dump(mode="json"))


def media_selection_proposed_change_hash(
    *,
    change_id: str,
    candidate_id: str,
    expected_candidate_revision: int,
    candidate_authority_hash: str,
    evaluated_world_revision: int,
    evaluated_deliberation_revision: int,
    evaluated_ledger_sequence: int,
    selection_hash: str,
    catalog_version: str,
) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "candidate_authority_hash": candidate_authority_hash,
                "candidate_id": candidate_id,
                "catalog_version": catalog_version,
                "change_id": change_id,
                "evaluated_deliberation_revision": evaluated_deliberation_revision,
                "evaluated_ledger_sequence": evaluated_ledger_sequence,
                "evaluated_world_revision": evaluated_world_revision,
                "expected_candidate_revision": expected_candidate_revision,
                "selection_hash": selection_hash,
            }
        ).encode("utf-8")
    ).hexdigest()


class MediaSelectionProposalRecordedPayload(FrozenModel):
    proposal_id: str = Field(min_length=1, max_length=256)
    change_id: str = Field(min_length=1, max_length=256)
    evaluated_world_revision: int = Field(ge=0)
    evaluated_deliberation_revision: int = Field(ge=0)
    evaluated_ledger_sequence: int = Field(ge=0)
    candidate_id: str = Field(min_length=1, max_length=256)
    expected_candidate_revision: int = Field(ge=1)
    candidate_authority_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection: MediaSelection
    selection_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_version: str = Field(min_length=1, max_length=128)
    proposed_change_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model: str = Field(min_length=1, max_length=256)
    raw_output_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    normalized_output_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def proposal_is_closed(self) -> "MediaSelectionProposalRecordedPayload":
        if self.selection.candidate_id != self.candidate_id:
            raise ValueError("media selection proposal selection must bind candidate")
        if self.selection_hash != media_selection_hash(self.selection):
            raise ValueError("media selection proposal selection hash is invalid")
        expected_policy = (
            MEDIA_SELECTION_PROPOSAL_POLICY_DIGEST
            if self.selection.family == "life_share"
            else MEDIA_SELECTION_P2_PROPOSAL_POLICY_DIGEST
        )
        if self.policy_digest != expected_policy:
            raise ValueError("media selection proposal policy is not installed")
        if self.proposed_change_hash != media_selection_proposed_change_hash(
            change_id=self.change_id,
            candidate_id=self.candidate_id,
            expected_candidate_revision=self.expected_candidate_revision,
            candidate_authority_hash=self.candidate_authority_hash,
            evaluated_world_revision=self.evaluated_world_revision,
            evaluated_deliberation_revision=self.evaluated_deliberation_revision,
            evaluated_ledger_sequence=self.evaluated_ledger_sequence,
            selection_hash=self.selection_hash,
            catalog_version=self.catalog_version,
        ):
            raise ValueError("media selection proposal change hash is invalid")
        return self


class MediaSelectionProposalError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"media_selection_proposal.{code}"
        super().__init__(self.code)


class MediaSelectionProposalCompiler:
    """Re-derive a model's selection at one pinned projection.

    The compiler deliberately accepts neither a candidate revision nor any
    source evidence from its caller.  It selects the already persisted
    aggregate by ID, then derives every authority-bearing field itself.
    """

    def __init__(self, *, catalog_version: str) -> None:
        if not catalog_version:
            raise ValueError("media selection proposal requires a catalog version")
        self._catalog_version = catalog_version

    def compile(
        self,
        *,
        projection: LedgerProjection,
        selection: MediaSelection,
        model: str,
        raw_output_hash: str,
        normalized_output_hash: str,
    ) -> MediaSelectionProposalRecordedPayload:
        candidate = next(
            (item for item in projection.photo_candidates if item.candidate_id == selection.candidate_id),
            None,
        )
        if (
            candidate is None
            or projection.logical_time is None
            or candidate.status != "available"
            or candidate.opened_at is None
            or candidate.expires_at is None
            or candidate.expires_at <= projection.logical_time
            or candidate.opened_event_ref is None
            or candidate.opened_event_payload_hash is None
            or not candidate.source_events
        ):
            raise MediaSelectionProposalError("candidate_not_current")
        p1 = (
            selection.family,
            selection.delivery_mode,
            selection.media_privacy_ceiling,
            selection.expression_charge_ceiling,
        ) == ("life_share", "preview", "ordinary", "none")
        p2 = (
            (selection.family, selection.delivery_mode, selection.media_privacy_ceiling,
             selection.expression_charge_ceiling) == ("character_media", "preview", "ordinary", "none")
            and selection.recipient_ref is None
            and selection.private_expression_basis_ref is None
            and candidate.character_media_contract is not None
        )
        if not p1 and not p2:
            raise MediaSelectionProposalError("ordinary_preview_only")
        if candidate.family != selection.family:
            raise MediaSelectionProposalError("selection_family_does_not_match_candidate")
        candidate_hash = media_candidate_authority_hash(candidate)
        selection_hash = media_selection_hash(selection)
        identity = media_digest(
            {
                "candidate_authority_hash": candidate_hash,
                "catalog_version": self._catalog_version,
                "cursor": {
                    "world_revision": projection.world_revision,
                    "deliberation_revision": projection.deliberation_revision,
                    "ledger_sequence": projection.ledger_sequence,
                },
                "selection_hash": selection_hash,
                "world_id": projection.world_id,
            }
        )
        change_id = "change:media-selection:" + identity
        return MediaSelectionProposalRecordedPayload(
            proposal_id="proposal:media-selection:" + identity,
            change_id=change_id,
            evaluated_world_revision=projection.world_revision,
            evaluated_deliberation_revision=projection.deliberation_revision,
            evaluated_ledger_sequence=projection.ledger_sequence,
            candidate_id=candidate.candidate_id,
            expected_candidate_revision=candidate.entity_revision,
            candidate_authority_hash=candidate_hash,
            selection=selection,
            selection_hash=selection_hash,
            catalog_version=self._catalog_version,
            proposed_change_hash=media_selection_proposed_change_hash(
                change_id=change_id,
                candidate_id=candidate.candidate_id,
                expected_candidate_revision=candidate.entity_revision,
                candidate_authority_hash=candidate_hash,
                evaluated_world_revision=projection.world_revision,
                evaluated_deliberation_revision=projection.deliberation_revision,
                evaluated_ledger_sequence=projection.ledger_sequence,
                selection_hash=selection_hash,
                catalog_version=self._catalog_version,
            ),
            policy_digest=(
                MEDIA_SELECTION_PROPOSAL_POLICY_DIGEST
                if p1 else MEDIA_SELECTION_P2_PROPOSAL_POLICY_DIGEST
            ),
            model=model,
            raw_output_hash=raw_output_hash,
            normalized_output_hash=normalized_output_hash,
        )


__all__ = [
    "MEDIA_SELECTION_PROPOSAL_POLICY_DIGEST", "MEDIA_SELECTION_P2_PROPOSAL_POLICY_DIGEST",
    "MEDIA_SELECTION_PROPOSAL_POLICY_VERSION",
    "MediaSelectionProposalRecordedPayload",
    "MediaSelectionProposalCompiler",
    "MediaSelectionProposalError",
    "media_candidate_authority_hash",
    "media_selection_proposed_change_hash",
]
