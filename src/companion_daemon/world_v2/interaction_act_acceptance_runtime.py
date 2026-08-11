"""Atomic event preparation for one accepted interaction-act status mutation."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from pydantic import Field, model_validator

from .accepted_ledger_batch import (
    AcceptedLedgerBatchHandle,
    AcceptedLedgerBatchIssuer,
)
from .interaction_act_acceptance_manifest import (
    INTERACTION_ACT_ACCEPTANCE_POLICY_DIGEST,
    INTERACTION_ACT_ACCEPTANCE_POLICY_VERSION,
    InteractionActAcceptanceManifest,
    build_interaction_act_acceptance_manifest,
)
from .interaction_act_events import (
    InteractionActAcceptedPayload,
    InteractionActProposalRecordedPayload,
    build_interaction_act_accepted_payload,
)
from .ledger import LedgerPort, WorldLedger
from .schema_core import FrozenModel, canonicalize_json_value
from .schemas import CommitResult, ProjectionCursor, WorldEvent
from .sqlite_ledger import SQLiteWorldLedger


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        canonicalize_json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class InteractionActAcceptanceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"interaction_act_acceptance.{code}"
        super().__init__(self.code)


class PreparedInteractionActAcceptance(FrozenModel):
    expected_cursor: ProjectionCursor
    events: tuple[WorldEvent, WorldEvent]
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit_id: str = Field(
        pattern=r"^commit:interaction-act-acceptance:[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def exact_order_and_hashes(self) -> PreparedInteractionActAcceptance:
        acceptance_event, effect_event = self.events
        if (
            acceptance_event.event_type != "AcceptanceRecorded"
            or effect_event.event_type != "InteractionActTransitionAccepted"
            or effect_event.causation_id != acceptance_event.event_id
        ):
            raise ValueError("interaction act accepted event order is invalid")
        manifest = InteractionActAcceptanceManifest.model_validate_json(
            acceptance_event.payload_json,
            strict=True,
        )
        accepted = InteractionActAcceptedPayload.model_validate_json(
            effect_event.payload_json,
            strict=True,
        )
        if (
            manifest.manifest_hash != self.manifest_hash
            or manifest.policy_digest != self.policy_digest
            or manifest.effect_event_id != effect_event.event_id
            or manifest.effect_payload_hash != effect_event.payload_hash
            or accepted.accepted_event_ref != effect_event.event_id
        ):
            raise ValueError("interaction act accepted event hashes are inconsistent")
        return self


class PinnedInteractionActProposalAuthorityHandle:
    """Reader-owned proof of one exact, persisted typed proposal at a cursor."""

    __slots__ = ("__proposal", "__proposal_event", "__cursor", "__issuer")

    def __init__(
        self,
        *,
        proposal: object,
        proposal_event: WorldEvent,
        cursor: ProjectionCursor,
        issuer: object,
    ) -> None:
        self.__proposal = proposal
        self.__proposal_event = proposal_event
        self.__cursor = cursor
        self.__issuer = issuer

    def issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def __reduce__(self) -> object:
        raise TypeError("pinned interaction act proposal handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("pinned interaction act proposal handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("pinned interaction act proposal handles cannot be copied")


class InteractionActProposalAuthorityReader:
    """Pin one exact proposal projection to its committed event envelope."""

    __slots__ = ("__ledger", "__issuer")

    def __init__(self, *, ledger: LedgerPort) -> None:
        self.__ledger = ledger
        self.__issuer = object()

    def pin(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        proposal_event_ref: str,
    ) -> PinnedInteractionActProposalAuthorityHandle:
        if world_id != self.__ledger.world_id:
            raise InteractionActAcceptanceError("authority_world_mismatch")
        projection = self.__ledger.project_at(cursor)
        matches = tuple(
            item
            for item in projection.interaction_act_proposals
            if item.recorded_event_ref == proposal_event_ref
        )
        if len(matches) != 1:
            raise InteractionActAcceptanceError("proposal_not_persisted")
        proposal = matches[0]
        located = self.__ledger.lookup_event_commit(proposal_event_ref)
        if located is None:
            raise InteractionActAcceptanceError("proposal_event_missing")
        proposal_event, commit = located
        if (
            proposal_event.world_id != world_id
            or proposal_event.event_type != "InteractionActProposalRecorded"
            or proposal_event.event_id not in commit.event_ids
            or proposal_event.payload_hash != proposal.recorded_event_payload_hash
            or commit.world_revision > cursor.world_revision
            or commit.deliberation_revision > cursor.deliberation_revision
            or commit.ledger_sequence > cursor.ledger_sequence
        ):
            raise InteractionActAcceptanceError("proposal_event_mismatch")
        try:
            recorded = InteractionActProposalRecordedPayload.model_validate_json(
                proposal_event.payload_json,
                strict=True,
            )
        except ValueError as exc:
            raise InteractionActAcceptanceError("proposal_event_invalid") from exc
        if proposal_event.payload() != recorded.model_dump(mode="json") or (
            proposal.proposal_id != recorded.proposal_id
            or proposal.proposal_hash != recorded.proposal_hash
            or proposal.change_id != recorded.change_id
            or proposal.accepted_change_hash != recorded.accepted_change_hash
            or proposal.evaluated_world_revision
            != recorded.evaluated_world_revision
            or proposal.mutation_payload_hash != recorded.mutation_payload_hash
            or proposal.mutation != recorded.mutation
            or proposal.recorded_event_ref != proposal_event.event_id
        ):
            raise InteractionActAcceptanceError("proposal_event_mismatch")
        return PinnedInteractionActProposalAuthorityHandle(
            proposal=recorded,
            proposal_event=proposal_event,
            cursor=cursor,
            issuer=self.__issuer,
        )

    def owns(self, handle: PinnedInteractionActProposalAuthorityHandle) -> bool:
        return (
            type(handle) is PinnedInteractionActProposalAuthorityHandle
            and handle.issued_by(self.__issuer)
        )


def _lane_idempotency_key(
    *, event_type: str, world_id: str, components: tuple[object, ...]
) -> str:
    encoded = json.dumps(
        [event_type, world_id, *components],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"world-v2:{event_type}:{hashlib.sha256(encoded).hexdigest()}"


class InteractionActAtomicRecorder:
    """Prepare one AcceptanceRecorded plus one participant-status effect."""

    __slots__ = ("__proposal_reader", "__batch_issuer")

    def __init__(
        self,
        *,
        proposal_reader: InteractionActProposalAuthorityReader,
        batch_issuer: AcceptedLedgerBatchIssuer,
    ) -> None:
        if type(proposal_reader) is not InteractionActProposalAuthorityReader:
            raise TypeError("interaction act recorder requires exact proposal reader")
        if type(batch_issuer) is not AcceptedLedgerBatchIssuer:
            raise TypeError("interaction act recorder requires exact accepted-batch issuer")
        self.__proposal_reader = proposal_reader
        self.__batch_issuer = batch_issuer

    def prepare_events(
        self,
        *,
        handle: PinnedInteractionActProposalAuthorityHandle,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> PreparedInteractionActAcceptance:
        if not self.__proposal_reader.owns(handle):
            raise InteractionActAcceptanceError("proposal_handle_untrusted")
        source_proposal_event = object.__getattribute__(
            handle,
            "_PinnedInteractionActProposalAuthorityHandle__proposal_event",
        )
        cursor = object.__getattribute__(
            handle,
            "_PinnedInteractionActProposalAuthorityHandle__cursor",
        )
        if source_proposal_event.world_id == "":
            raise InteractionActAcceptanceError("world_missing")
        acceptance_seed = {
            "contract": "interaction-act-acceptance-id.1",
            "world_id": source_proposal_event.world_id,
            "source_proposal_event_ref": source_proposal_event.event_id,
            "source_proposal_event_payload_hash": source_proposal_event.payload_hash,
        }
        acceptance_id = f"acceptance:interaction-act:{_canonical_hash(acceptance_seed)}"
        try:
            accepted = build_interaction_act_accepted_payload(
                acceptance_id=acceptance_id,
                source_proposal_event=source_proposal_event,
            )
        except ValueError as exc:
            raise InteractionActAcceptanceError("proposal_invalid") from exc
        if accepted.evaluated_world_revision != cursor.world_revision:
            raise InteractionActAcceptanceError("proposal_stale")
        if accepted.world_id != source_proposal_event.world_id:
            raise InteractionActAcceptanceError("world_mismatch")
        for source_ref in accepted.mutation.act_after.source_refs:
            if source_ref.source_world_revision > cursor.world_revision:
                raise InteractionActAcceptanceError("mutation_source_after_cursor")
            proof = source_ref.delivery_proof
            if proof is not None and proof.receipt_world_revision > cursor.world_revision:
                raise InteractionActAcceptanceError("receipt_after_cursor")

        manifest = build_interaction_act_acceptance_manifest(
            accepted_payload=accepted,
            policy_digest=INTERACTION_ACT_ACCEPTANCE_POLICY_DIGEST,
        )
        acceptance_event_id = (
            "event:interaction-act-acceptance:"
            + _canonical_hash(
                {
                    "world_id": accepted.world_id,
                    "acceptance_id": accepted.acceptance_id,
                    "manifest_hash": manifest.manifest_hash,
                }
            )
        )
        manifest_payload = manifest.model_dump(mode="json")
        accepted_payload = accepted.model_dump(mode="json")
        common = {
            "schema_version": "world-v2.1",
            "world_id": accepted.world_id,
            "logical_time": logical_time,
            "created_at": created_at,
            "actor": actor,
            "source": source,
            "trace_id": trace_id,
            "correlation_id": correlation_id,
        }
        acceptance_event = WorldEvent.from_payload(
            **common,
            event_id=acceptance_event_id,
            event_type="AcceptanceRecorded",
            causation_id=source_proposal_event.event_id,
            idempotency_key=_lane_idempotency_key(
                event_type="AcceptanceRecorded",
                world_id=accepted.world_id,
                components=(
                    manifest.manifest_version,
                    manifest.acceptance_id,
                    manifest.manifest_hash,
                ),
            ),
            payload=manifest_payload,
        )
        effect_event = WorldEvent.from_payload(
            **common,
            event_id=accepted.accepted_event_ref,
            event_type="InteractionActTransitionAccepted",
            causation_id=acceptance_event.event_id,
            idempotency_key=_lane_idempotency_key(
                event_type="InteractionActTransitionAccepted",
                world_id=accepted.world_id,
                components=(
                    accepted.proposal_id,
                    accepted.change_id,
                    accepted.mutation_payload_hash,
                ),
            ),
            payload=accepted_payload,
        )
        if effect_event.payload_hash != manifest.effect_payload_hash:
            raise InteractionActAcceptanceError("effect_hash_mismatch")
        events = (acceptance_event, effect_event)
        commit_id = "commit:interaction-act-acceptance:" + _canonical_hash(
            {
                "contract": "interaction-act-accepted-commit.1",
                "world_id": accepted.world_id,
                "cursor": cursor.model_dump(mode="json"),
                "manifest_hash": manifest.manifest_hash,
                "events": tuple(event.model_dump(mode="json") for event in events),
                "policy_digest": INTERACTION_ACT_ACCEPTANCE_POLICY_DIGEST,
            }
        )
        return PreparedInteractionActAcceptance(
            expected_cursor=cursor,
            events=events,
            manifest_hash=manifest.manifest_hash,
            policy_digest=INTERACTION_ACT_ACCEPTANCE_POLICY_DIGEST,
            commit_id=commit_id,
        )

    def prepare_batch(
        self,
        *,
        handle: PinnedInteractionActProposalAuthorityHandle,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> AcceptedLedgerBatchHandle:
        prepared = self.prepare_events(
            handle=handle,
            actor=actor,
            source=source,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return self.__batch_issuer.issue(
            world_id=prepared.events[0].world_id,
            expected_cursor=prepared.expected_cursor,
            events=prepared.events,
            manifest_hash=prepared.manifest_hash,
            registry_digest=prepared.policy_digest,
            commit_id=prepared.commit_id,
        )


class InteractionActAcceptanceRuntime:
    """Composition root for persisted generic interaction-act acceptance."""

    __slots__ = ("ledger", "_reader", "_recorder")

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        batch_issuer: AcceptedLedgerBatchIssuer,
    ) -> None:
        self.ledger = ledger
        self._reader = InteractionActProposalAuthorityReader(ledger=ledger)
        self._recorder = InteractionActAtomicRecorder(
            proposal_reader=self._reader,
            batch_issuer=batch_issuer,
        )

    @classmethod
    def in_memory(cls, *, world_id: str) -> InteractionActAcceptanceRuntime:
        issuer = AcceptedLedgerBatchIssuer()
        return cls(
            ledger=WorldLedger.in_memory(
                world_id=world_id,
                accepted_batch_issuer=issuer,
            ),
            batch_issuer=issuer,
        )

    @classmethod
    def open(
        cls,
        *,
        path: Path,
        world_id: str,
    ) -> InteractionActAcceptanceRuntime:
        issuer = AcceptedLedgerBatchIssuer()
        return cls(
            ledger=SQLiteWorldLedger(
                path=path,
                world_id=world_id,
                accepted_batch_issuer=issuer,
            ),
            batch_issuer=issuer,
        )

    def close(self) -> None:
        close = getattr(self.ledger, "close", None)
        if close is not None:
            close()

    def pin_proposal(
        self,
        *,
        cursor: ProjectionCursor,
        proposal_event_ref: str,
    ) -> PinnedInteractionActProposalAuthorityHandle:
        return self._reader.pin(
            world_id=self.ledger.world_id,
            cursor=cursor,
            proposal_event_ref=proposal_event_ref,
        )

    def accept(
        self,
        *,
        handle: PinnedInteractionActProposalAuthorityHandle,
        actor: str,
        source: str,
        logical_time: datetime,
        created_at: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> CommitResult:
        cursor = object.__getattribute__(
            handle,
            "_PinnedInteractionActProposalAuthorityHandle__cursor",
        )
        batch = self._recorder.prepare_batch(
            handle=handle,
            actor=actor,
            source=source,
            logical_time=logical_time,
            created_at=created_at,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return self.ledger.commit_accepted(batch, expected_cursor=cursor)


__all__ = [
    "INTERACTION_ACT_ACCEPTANCE_POLICY_DIGEST",
    "INTERACTION_ACT_ACCEPTANCE_POLICY_VERSION",
    "InteractionActAcceptanceError",
    "InteractionActAcceptanceRuntime",
    "InteractionActAtomicRecorder",
    "InteractionActProposalAuthorityReader",
    "PinnedInteractionActProposalAuthorityHandle",
    "PreparedInteractionActAcceptance",
]
