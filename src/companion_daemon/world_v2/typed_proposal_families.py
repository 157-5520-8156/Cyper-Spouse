"""Store-free ownership manifest for installed typed-proposal families."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal, Protocol

from .affect_events import (
    AffectBaselineAdjustedPayload,
    AffectEpisodeOpenedPayload,
    AffectEpisodeResolvedPayload,
    AffectEpisodeSupersededPayload,
    AffectEpisodeUpdatedPayload,
)
from .appraisal_events import (
    AppraisalAcceptedPayload,
    AppraisalContradictedPayload,
    AppraisalSupersededPayload,
)
from .attention_authority_contract import (
    V2_ATTENTION_MUTATION_EVENT_TYPES,
    require_attention_event_operation,
)
from .attention_authority_events import V2AttentionChangedPayload
from .attention_authority_schemas import V2AttentionProposalProjection
from .commitment_events import CommitmentChangedPayload
from .character_core_events import CHARACTER_CORE_PAYLOAD_MODELS, CharacterCoreChangedPayload
from .fact_events import FACT_PAYLOAD_MODELS, FactChangedPayload
from .goal_authority_events import V2_GOAL_PAYLOAD_MODELS, V2GoalChangedPayload
from .goal_authority_contract import require_goal_event_operation
from .goal_situation_schemas import V2GoalProposalProjection
from .location_authority_contract import (
    V2_LOCATION_MUTATION_EVENT_TYPES,
    require_location_event_operation,
)
from .location_authority_events import V2LocationChangedPayload
from .location_authority_schemas import V2LocationProposalProjection
from .resource_authority_contract import V2_RESOURCE_EVENT_TYPES
from .resource_authority_events import V2_RESOURCE_CODEC
from .experience_events import EXPERIENCE_PAYLOAD_MODELS, ExperienceCommittedPayload
from .life_events import OutcomeProposalRecordedPayload, WorldOccurrenceSettledPayload
from .memory_events import (
    MEMORY_CANDIDATE_PAYLOAD_MODELS,
    MemoryCandidateChangedPayload,
)
from .relationship_events import RELATIONSHIP_PAYLOAD_MODELS
from .thread_events import THREAD_PAYLOAD_MODELS
from .schemas import (
    AffectProposalProjection,
    AppraisalProposalProjection,
    OutcomeProposalProjection,
    CommitmentProposalProjection,
    CharacterCoreProposalProjection,
    FactProposalProjection,
    ExperienceProposalProjection,
    MemoryCandidateProposalProjection,
    RelationshipProposalProjection,
    ThreadProposalProjection,
)
from .typed_proposals import (
    AcceptedMutationBinding,
    DuplicateTypedProposalRegistration,
    ProposalAuthorityBinding,
    RecordSelector,
    TYPED_PROPOSAL_ENCODING,
    TypedProposalRegistryError,
    UnknownTypedProposalContract,
)


RecordMode = Literal["legacy_kind", "dedicated_event", "explicit_contract"]
IdentityComponents = tuple[object, ...] | None


class TypedProposalFamilyCodec(Protocol):
    def decode_record(self, *, event_type: str, payload: dict[str, object]) -> object: ...

    def bind(self, proposal: object) -> ProposalAuthorityBinding: ...

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object: ...

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding: ...

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents: ...

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents: ...


@dataclass(frozen=True, slots=True)
class TypedProposalFamily:
    contract_ref: str
    selector: RecordSelector
    record_mode: RecordMode
    requires_separate_deliberation_commit: bool
    mutation_event_types: tuple[str, ...]
    codec: TypedProposalFamilyCodec


def _nested(payload: dict[str, object], parent: str, child: str) -> object:
    value = payload.get(parent)
    return value.get(child) if isinstance(value, dict) else None


def _validate_json(model: object, payload: dict[str, object]) -> object:
    return model.model_validate_json(  # type: ignore[attr-defined,no-any-return]
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _accepted_binding(
    mutation: object, *, proposal_id_field: str = "proposal_id"
) -> AcceptedMutationBinding:
    return AcceptedMutationBinding(
        proposal_id=str(getattr(mutation, proposal_id_field)),
        acceptance_id=str(getattr(mutation, "acceptance_id")),
        evaluated_world_revision=int(getattr(mutation, "evaluated_world_revision")),
        change_id=str(getattr(mutation, "change_id")),
        accepted_change_hash=str(getattr(mutation, "accepted_change_hash")),
    )


class _AppraisalFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> AppraisalProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("appraisal codec only accepts ProposalRecorded")
        return _validate_json(AppraisalProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, AppraisalProposalProjection):
            raise TypeError("appraisal codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref="proposal-contract:appraisal-legacy.1",
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        model = {
            "AppraisalAccepted": AppraisalAcceptedPayload,
            "AppraisalContradicted": AppraisalContradictedPayload,
            "AppraisalSuperseded": AppraisalSupersededPayload,
        }[event_type]
        return _validate_json(model, payload)

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return world_id, payload.get("proposal_id"), payload.get("change_id")

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        if event_type == "AppraisalAccepted":
            return world_id, _nested(payload, "appraisal", "appraisal_id"), payload.get(
                "transition_id"
            )
        return payload.get("appraisal_id"), payload.get("transition_id")


class _AffectFamilyCodec:
    _MUTATION_MODELS = {
        "AffectEpisodeOpened": AffectEpisodeOpenedPayload,
        "AffectEpisodeUpdated": AffectEpisodeUpdatedPayload,
        "AffectEpisodeResolved": AffectEpisodeResolvedPayload,
        "AffectEpisodeSuperseded": AffectEpisodeSupersededPayload,
        "AffectBaselineAdjusted": AffectBaselineAdjustedPayload,
    }

    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> AffectProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("affect codec only accepts ProposalRecorded")
        return _validate_json(AffectProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, AffectProposalProjection):
            raise TypeError("affect codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref="proposal-contract:affect-legacy.1",
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        return _validate_json(self._MUTATION_MODELS[event_type], payload)

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        if payload.get("authority_contract_ref") == "affect-proposal-compiler.1":
            source = payload.get("source_audit")
            if not isinstance(source, dict):
                return None
            return (
                world_id,
                payload.get("proposal_id"),
                payload.get("change_id"),
                source.get("proposal_event_ref"),
            )
        # Legacy Affect ProposalRecorded events had no installed domain identity.
        return None

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        if event_type == "AffectEpisodeOpened":
            return world_id, _nested(payload, "episode", "episode_id"), payload.get(
                "transition_id"
            )
        if event_type in {"AffectEpisodeUpdated", "AffectEpisodeResolved"}:
            return payload.get("episode_id"), payload.get("transition_id")
        if event_type == "AffectEpisodeSuperseded":
            return (
                payload.get("episode_id"),
                _nested(payload, "successor", "episode_id"),
                payload.get("transition_id"),
            )
        return (
            world_id,
            payload.get("dimension"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )


class _OutcomeFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> OutcomeProposalProjection:
        if event_type != "OutcomeProposalRecorded":
            raise ValueError("outcome codec only accepts OutcomeProposalRecorded")
        recorded = _validate_json(OutcomeProposalRecordedPayload, payload)
        if not isinstance(recorded, OutcomeProposalRecordedPayload):
            raise TypeError("outcome codec decoded an incompatible record")
        return OutcomeProposalProjection.model_validate(recorded.model_dump())

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, OutcomeProposalProjection):
            raise TypeError("outcome codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.outcome_proposal_id,
            proposal_kind="outcome_transition",
            authority_contract_ref="proposal-contract:outcome-legacy.1",
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.evaluated_entity_revision,
            mutation_event_type="WorldOccurrenceSettled",
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        if event_type != "WorldOccurrenceSettled":
            raise ValueError("outcome codec only owns WorldOccurrenceSettled")
        return _validate_json(WorldOccurrenceSettledPayload, payload)

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        return _accepted_binding(mutation, proposal_id_field="outcome_proposal_id")

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return world_id, payload.get("outcome_proposal_id")

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            payload.get("occurrence_id"),
            payload.get("result_id"),
            payload.get("expected_entity_revision"),
        )


class _RelationshipFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> RelationshipProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("relationship codec only accepts ProposalRecorded")
        return _validate_json(RelationshipProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, RelationshipProposalProjection):
            raise TypeError("relationship codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref=proposal.authority_contract_ref,
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        return _validate_json(RELATIONSHIP_PAYLOAD_MODELS[event_type], payload)

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("change_id"),
            payload.get("authority_contract_ref"),
        )

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        if event_type == "RelationshipSignalAccepted":
            return world_id, _nested(payload, "signal", "semantic_fingerprint")
        if event_type == "RelationshipSlowVariableAdjusted":
            return (
                payload.get("relationship_id"),
                payload.get("expected_entity_revision"),
                payload.get("adjustment_id"),
            )
        return (
            _nested(payload, "boundary", "boundary_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )


class _ThreadFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> ThreadProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("thread codec only accepts ProposalRecorded")
        return _validate_json(ThreadProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, ThreadProposalProjection):
            raise TypeError("thread codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref=proposal.authority_contract_ref,
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        return _validate_json(THREAD_PAYLOAD_MODELS[event_type], payload)

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("change_id"),
            payload.get("authority_contract_ref"),
        )

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            _nested(payload, "thread_after", "thread_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )


class _CommitmentFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> CommitmentProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("commitment codec only accepts ProposalRecorded")
        return _validate_json(CommitmentProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, CommitmentProposalProjection):
            raise TypeError("commitment codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref=proposal.authority_contract_ref,
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        return _validate_json(CommitmentChangedPayload, payload)

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("change_id"),
            payload.get("authority_contract_ref"),
        )

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            _nested(payload, "commitment_after", "commitment_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )


class _FactFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> FactProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("fact codec only accepts ProposalRecorded")
        return _validate_json(FactProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, FactProposalProjection):
            raise TypeError("fact codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref=proposal.authority_contract_ref,
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        return _validate_json(FactChangedPayload, payload)

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("change_id"),
            payload.get("authority_contract_ref"),
        )

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            _nested(payload, "fact_after", "fact_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )


class _ExperienceFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> ExperienceProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("experience codec only accepts ProposalRecorded")
        return _validate_json(ExperienceProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, ExperienceProposalProjection):
            raise TypeError("experience codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref=proposal.authority_contract_ref,
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        return _validate_json(ExperienceCommittedPayload, payload)

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("change_id"),
            payload.get("authority_contract_ref"),
        )

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            _nested(payload, "experience", "experience_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )


class _MemoryCandidateFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> MemoryCandidateProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("memory candidate codec only accepts ProposalRecorded")
        return _validate_json(MemoryCandidateProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, MemoryCandidateProposalProjection):
            raise TypeError("memory candidate codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref=proposal.authority_contract_ref,
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        return _validate_json(MemoryCandidateChangedPayload, payload)

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("change_id"),
            payload.get("authority_contract_ref"),
        )

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            _nested(payload, "candidate_after", "candidate_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )


class _CharacterCoreFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> CharacterCoreProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("character core codec only accepts ProposalRecorded")
        return _validate_json(CharacterCoreProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, CharacterCoreProposalProjection):
            raise TypeError("character core codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref=proposal.authority_contract_ref,
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        return _validate_json(CharacterCoreChangedPayload, payload)

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("change_id"),
            payload.get("authority_contract_ref"),
        )

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            _nested(payload, "core_after", "core_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )


class _V2GoalFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> V2GoalProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("Goal codec only accepts ProposalRecorded")
        return _validate_json(V2GoalProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, V2GoalProposalProjection):
            raise TypeError("Goal codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref=proposal.authority_contract_ref,
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        mutation = _validate_json(V2GoalChangedPayload, payload)
        require_goal_event_operation(
            event_type=event_type,
            operation=mutation.operation,  # type: ignore[attr-defined]
        )
        return mutation

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        if not isinstance(mutation, V2GoalChangedPayload):
            raise TypeError("Goal codec received an incompatible mutation")
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("change_id"),
            payload.get("authority_contract_ref"),
        )

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            _nested(payload, "goal_after", "goal_id"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )


class _V2LocationFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> V2LocationProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("Location codec only accepts ProposalRecorded")
        return _validate_json(V2LocationProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, V2LocationProposalProjection):
            raise TypeError("Location codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref=proposal.authority_contract_ref,
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        mutation = _validate_json(V2LocationChangedPayload, payload)
        require_location_event_operation(
            event_type=event_type,
            operation=mutation.operation,  # type: ignore[attr-defined]
        )
        return mutation

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        if not isinstance(mutation, V2LocationChangedPayload):
            raise TypeError("Location codec received an incompatible mutation")
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("change_id"),
            payload.get("authority_contract_ref"),
        )

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            _nested(payload, "location_after", "actor_ref"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )


class _V2AttentionFamilyCodec:
    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> V2AttentionProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("Attention codec only accepts ProposalRecorded")
        return _validate_json(V2AttentionProposalProjection, payload)  # type: ignore[return-value]

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, V2AttentionProposalProjection):
            raise TypeError("Attention codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref=proposal.authority_contract_ref,
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        mutation = _validate_json(V2AttentionChangedPayload, payload)
        require_attention_event_operation(
            event_type=event_type,
            operation=mutation.operation,  # type: ignore[attr-defined]
        )
        return mutation

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        if not isinstance(mutation, V2AttentionChangedPayload):
            raise TypeError("Attention codec received an incompatible mutation")
        return _accepted_binding(mutation)

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("change_id"),
            payload.get("authority_contract_ref"),
        )

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> IdentityComponents:
        return (
            world_id,
            _nested(payload, "attention_after", "actor_ref"),
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )

INSTALLED_TYPED_PROPOSAL_FAMILIES = tuple(
    sorted(
        (
            TypedProposalFamily(
                contract_ref="proposal-contract:appraisal-legacy.1",
                selector=RecordSelector("ProposalRecorded", "appraisal_transition"),
                record_mode="legacy_kind",
                requires_separate_deliberation_commit=True,
                mutation_event_types=(
                    "AppraisalAccepted",
                    "AppraisalContradicted",
                    "AppraisalSuperseded",
                ),
                codec=_AppraisalFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:affect-legacy.1",
                selector=RecordSelector("ProposalRecorded", "affect_transition"),
                record_mode="legacy_kind",
                requires_separate_deliberation_commit=True,
                mutation_event_types=tuple(_AffectFamilyCodec._MUTATION_MODELS),
                codec=_AffectFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:outcome-legacy.1",
                selector=RecordSelector("OutcomeProposalRecorded", "outcome_transition"),
                record_mode="dedicated_event",
                requires_separate_deliberation_commit=False,
                mutation_event_types=("WorldOccurrenceSettled",),
                codec=_OutcomeFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:relationship.1",
                selector=RecordSelector("ProposalRecorded", "relationship_transition"),
                record_mode="explicit_contract",
                requires_separate_deliberation_commit=True,
                mutation_event_types=tuple(RELATIONSHIP_PAYLOAD_MODELS),
                codec=_RelationshipFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:thread.1",
                selector=RecordSelector("ProposalRecorded", "thread_transition"),
                record_mode="explicit_contract",
                requires_separate_deliberation_commit=True,
                mutation_event_types=tuple(THREAD_PAYLOAD_MODELS),
                codec=_ThreadFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:commitment.1",
                selector=RecordSelector("ProposalRecorded", "commitment_transition"),
                record_mode="explicit_contract",
                requires_separate_deliberation_commit=True,
                mutation_event_types=(
                    "PrivateCommitmentOpened",
                    "PrivateCommitmentFulfilled",
                    "PrivateCommitmentBroken",
                    "PrivateCommitmentReleased",
                ),
                codec=_CommitmentFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:fact.1",
                selector=RecordSelector("ProposalRecorded", "fact_transition"),
                record_mode="explicit_contract",
                requires_separate_deliberation_commit=True,
                mutation_event_types=tuple(FACT_PAYLOAD_MODELS),
                codec=_FactFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:experience.1",
                selector=RecordSelector("ProposalRecorded", "experience_transition"),
                record_mode="explicit_contract",
                requires_separate_deliberation_commit=True,
                mutation_event_types=tuple(EXPERIENCE_PAYLOAD_MODELS),
                codec=_ExperienceFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:character-core.1",
                selector=RecordSelector("ProposalRecorded", "character_core_revision"),
                record_mode="explicit_contract",
                requires_separate_deliberation_commit=True,
                mutation_event_types=tuple(CHARACTER_CORE_PAYLOAD_MODELS),
                codec=_CharacterCoreFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:memory-candidate.1",
                selector=RecordSelector(
                    "ProposalRecorded", "memory_candidate_transition"
                ),
                record_mode="explicit_contract",
                requires_separate_deliberation_commit=True,
                mutation_event_types=tuple(MEMORY_CANDIDATE_PAYLOAD_MODELS),
                codec=_MemoryCandidateFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:v2-goal.1",
                selector=RecordSelector("ProposalRecorded", "v2_goal_transition"),
                record_mode="explicit_contract",
                requires_separate_deliberation_commit=True,
                mutation_event_types=tuple(V2_GOAL_PAYLOAD_MODELS),
                codec=_V2GoalFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:v2-location.1",
                selector=RecordSelector("ProposalRecorded", "v2_location_transition"),
                record_mode="explicit_contract",
                requires_separate_deliberation_commit=True,
                mutation_event_types=V2_LOCATION_MUTATION_EVENT_TYPES,
                codec=_V2LocationFamilyCodec(),
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:v2-resource.1",
                selector=RecordSelector("ProposalRecorded", "v2_resource_transition"),
                record_mode="explicit_contract",
                requires_separate_deliberation_commit=True,
                mutation_event_types=V2_RESOURCE_EVENT_TYPES,
                codec=V2_RESOURCE_CODEC,
            ),
            TypedProposalFamily(
                contract_ref="proposal-contract:v2-attention.1",
                selector=RecordSelector("ProposalRecorded", "v2_attention_transition"),
                record_mode="explicit_contract",
                requires_separate_deliberation_commit=True,
                mutation_event_types=V2_ATTENTION_MUTATION_EVENT_TYPES,
                codec=_V2AttentionFamilyCodec(),
            ),
        ),
        key=lambda item: item.contract_ref,
    )
)

def validate_typed_proposal_family_manifest(
    families: tuple[TypedProposalFamily, ...],
) -> tuple[TypedProposalFamily, ...]:
    contracts: set[str] = set()
    selectors: set[RecordSelector] = set()
    mutations: set[str] = set()
    for family in families:
        if family.contract_ref in contracts:
            raise DuplicateTypedProposalRegistration(
                f"duplicate typed proposal contract {family.contract_ref!r}"
            )
        contracts.add(family.contract_ref)
        if family.selector in selectors:
            raise DuplicateTypedProposalRegistration(
                f"duplicate typed proposal record selector {family.selector!r}"
            )
        selectors.add(family.selector)
        for event_type in family.mutation_event_types:
            if event_type in mutations:
                raise DuplicateTypedProposalRegistration(
                    f"duplicate typed proposal mutation owner for {event_type!r}"
                )
            mutations.add(event_type)
    return families


INSTALLED_TYPED_PROPOSAL_FAMILIES = validate_typed_proposal_family_manifest(
    INSTALLED_TYPED_PROPOSAL_FAMILIES
)
_BY_CONTRACT = {item.contract_ref: item for item in INSTALLED_TYPED_PROPOSAL_FAMILIES}
_BY_MUTATION = {
    event_type: item
    for item in INSTALLED_TYPED_PROPOSAL_FAMILIES
    for event_type in item.mutation_event_types
}


def family_for_record(
    event_type: str, payload: dict[str, object]
) -> TypedProposalFamily | None:
    if event_type not in {item.selector.event_type for item in INSTALLED_TYPED_PROPOSAL_FAMILIES}:
        return None
    encoding = payload.get("proposal_encoding")
    if encoding is not None:
        if encoding != TYPED_PROPOSAL_ENCODING:
            raise TypedProposalRegistryError(
                f"typed proposal encoding {encoding!r} is not installed"
            )
        contract_ref = payload.get("authority_contract_ref")
        family = _BY_CONTRACT.get(contract_ref) if isinstance(contract_ref, str) else None
        if family is None:
            raise UnknownTypedProposalContract(
                f"typed proposal contract {contract_ref!r} is not installed"
            )
        if (
            family.record_mode != "explicit_contract"
            or event_type != family.selector.event_type
            or payload.get("proposal_kind") != family.selector.proposal_kind
        ):
            raise TypedProposalRegistryError(
                f"typed proposal contract {contract_ref!r} does not own this record"
            )
        return family
    for family in INSTALLED_TYPED_PROPOSAL_FAMILIES:
        if family.record_mode == "legacy_kind" and (
            event_type == family.selector.event_type
            and payload.get("proposal_kind") == family.selector.proposal_kind
        ):
            return family
        if family.record_mode == "dedicated_event" and event_type == family.selector.event_type:
            return family
    return None


def family_for_mutation(event_type: str) -> TypedProposalFamily | None:
    return _BY_MUTATION.get(event_type)
