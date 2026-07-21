"""Pure `.16` GoalAuthority reducer.

DORMANT — no producer: no production ledger holds a committed ``V2Goal*``
event and no runtime constructs these payloads (the tests below guard replay
semantics only).  Before wiring a producer, read the Producer-First Authority
rule in CONTEXT.md and record the activation verdict in
``configs/mechanism_closure.yaml`` (``v16-situation-constituents``).

The interface accepts immutable heads/history plus exact authority projections;
all lifecycle, provenance, and replay rules stay inside this Module.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .goal_authority_events import V2GoalChangedPayload, V2GoalExpiredPayload
from .actor_authority_reducers import ACTOR_AUTHORITY_V2_POLICY_DIGEST
from .clock_authority import resolve_latest_clock, validate_clock_history
from .goal_authority_contract import require_goal_event_operation
from .goal_situation_schemas import (
    CompensationCauseAuthority,
    ClockCauseAuthority,
    CommittedEvidenceBasis,
    DeliberativeCauseAuthority,
    DomainOperatorAuthorityBinding,
    GoalExpiryCorrectionBasis,
    InternalIntentionBasis,
    RandomDrawProjection,
    SettledEventCauseAuthority,
    V2GoalProjection,
    V2GoalAbandonedTerminalReason,
    V2GoalCompletedTerminalReason,
    V2GoalCompletionContract,
    V2GoalFactCompletionEvidence,
    V2GoalExpiredTerminalReason,
    V2GoalOccurrenceCompletionEvidence,
    V2_GOAL_EVIDENCE_PARSER_BY_KIND,
    V2_GOAL_CONTRACT_SCHEMA_BY_KIND,
    V2_GOAL_EVIDENCE_SCHEMA_BY_KIND,
    V2GoalTransitionProjection,
)
from .schemas import (
    ActorAuthorityProjection,
    ClockTransitionProjection,
    CommittedWorldEventRef,
    ExperienceProjection,
    FactProjection,
    WorldOccurrenceProjection,
)


V2_GOAL_POLICY_REFS = ("policy:v2-goal-authority.1",)
V2_GOAL_POLICY_VERSION = "v2-goal-authority-policy.1"
V2_GOAL_OPERATOR_OPERATION = "v2_goal_governance"
V2_GOAL_INTERNAL_BASIS_POLICY_VERSION = "v2-goal-internal-intention.1"
V2_GOAL_INTERNAL_BASIS_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "version": V2_GOAL_INTERNAL_BASIS_POLICY_VERSION,
            "internal_intention_operations": [
                "abandon",
                "block",
                "open",
                "pause",
                "resume",
                "revise",
                "unblock",
            ],
            "committed_basis_only_operations": ["complete", "progress"],
            "intention_kind_catalog": [
                "attention_choice",
                "goal_choice",
                "goal_governance",
                "resource_self_regulation",
            ],
            "intention_class_catalog": [
                "constraint_response",
                "priority_reassessment",
                "self_direction",
                "uncertainty_management",
                "value_alignment",
            ],
            "goal_intention_kinds": ["goal_choice", "goal_governance"],
            "privacy_floor": "private",
            "rationale_privacy": "strictest-meet",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
V2_GOAL_COMPLETION_CONTRACT_POLICY_VERSION = "v2-goal-completion-contract.1"
V2_GOAL_COMPLETION_CONTRACT_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "version": V2_GOAL_COMPLETION_CONTRACT_POLICY_VERSION,
            "completion_kinds": [
                "active_fact_predicate",
                "settled_occurrence_outcome",
            ],
            "cutoff": "strictly-greater-world-revision",
            "allowed_event_types": [
                "FactCommitted",
                "FactCorrected",
                "WorldOccurrenceSettled",
            ],
            "contract_schema_by_kind": V2_GOAL_CONTRACT_SCHEMA_BY_KIND,
            "evidence_parser_by_kind": V2_GOAL_EVIDENCE_PARSER_BY_KIND,
            "evidence_schema_by_kind": V2_GOAL_EVIDENCE_SCHEMA_BY_KIND,
            "evidence_union_by_kind": {
                "active_fact_predicate": "fact_state",
                "settled_occurrence_outcome": "occurrence_settlement",
            },
            "matching_rules": [
                "actor_exact",
                "outcome_exact",
                "privacy_strictest_meet",
                "world_revision_strictly_after_cutoff",
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
V2_GOAL_EXPIRY_POLICY_VERSION = "v2-goal-expiry.1"
V2_GOAL_EXPIRY_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "version": V2_GOAL_EXPIRY_POLICY_VERSION,
            "authority": "exact-latest-installed-clock-projection",
            "eligible_statuses": ["active", "blocked", "paused"],
            "due_rule": "clock-logical-time-to-greater-than-or-equal-due-end",
            "after_rule": "exact-expired-terminal-at-clock-time",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
V2_GOAL_EXPIRY_CORRECTION_POLICY_VERSION = "v2-goal-expiry-correction.1"
V2_GOAL_EXPIRY_CORRECTION_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {
            "version": V2_GOAL_EXPIRY_CORRECTION_POLICY_VERSION,
            "correction_classes": [
                "due_window",
                "operator_import_error",
            ],
            "reserved_fail_closed_classes": [
                "clock_transition",
                "policy_application",
            ],
            "requirements": [
                "exact-original-clock-and-expiry-transition",
                "at-least-one-post-target-typed-committed-source",
                "current-v2-goal-operator-reauthorization",
                "strictest-privacy-floor",
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
_POLICY_ARTIFACT = {
    "version": V2_GOAL_POLICY_VERSION,
    "terminal_statuses": ["abandoned", "completed", "expired"],
    "progress_10000_is_not_completion": True,
    "partial_unblock_preserves_blocked": True,
    "paused_or_blocked_progress_preserves_status": True,
    "clock_expiry": "latest_clock_at_current_logical_time",
    "compensation": "exact_latest_non_open_with_effective_authority_lineage",
    "installed_selection_modes": ["direct"],
    "reserved_fail_closed_selection_modes": ["random_draw"],
    "zero_cascade": True,
}
V2_GOAL_POLICY_DIGEST = hashlib.sha256(
    json.dumps(_POLICY_ARTIFACT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()

# Stable fail-closed domain codes.  Keep “not installed” separate from the
# existing “lacks exact … authority” errors: callers may retry malformed
# evidence after correcting it, but they must not retry an uninstalled lane as
# though another evidence reference could make it legal.
GOAL_AUTHORITY_LANE_NOT_INSTALLED = "goal_authority_lane_not_installed"
GOAL_SETTLEMENT_WRITE_AUTHORITY_NOT_INSTALLED = (
    "goal_settlement_write_authority_not_installed"
)
GOAL_CHARACTER_CORE_SOURCE_NOT_INSTALLED = (
    "goal_character_core_source_not_installed"
)
GOAL_CLOCK_WRITE_AUTHORITY_NOT_INSTALLED = "goal_clock_write_authority_not_installed"

# This is deliberately narrower than the shared V16 authority union.  The
# union is shared by several domains and preserves versioned wire shapes; it
# is not itself a capability grant.  In particular a settled event can be a
# *deliberative basis* for a Goal, but it cannot write a Goal directly.
_INSTALLED_GOAL_LANES_BY_OPERATION: dict[str, frozenset[str]] = {
    "open": frozenset({"deliberative", "operator"}),
    "revise": frozenset({"deliberative", "operator"}),
    "progress": frozenset({"deliberative"}),
    "pause": frozenset({"deliberative"}),
    "resume": frozenset({"deliberative"}),
    "block": frozenset({"deliberative"}),
    "unblock": frozenset({"deliberative"}),
    "complete": frozenset({"deliberative", "operator"}),
    "abandon": frozenset({"deliberative"}),
    "compensate": frozenset({"compensation"}),
}
_EXPECTED_GOAL_CAUSE_BY_LANE = {
    "deliberative": DeliberativeCauseAuthority,
    "operator": DomainOperatorAuthorityBinding,
    "settlement": SettledEventCauseAuthority,
    "clock_runtime": ClockCauseAuthority,
    "compensation": CompensationCauseAuthority,
}


def _require_installed_goal_transition_authority(
    payload: V2GoalChangedPayload,
) -> None:
    """Defend the frozen Goal lane matrix even for bypassed Pydantic payloads.

    Typed proposals are validated before they arrive here, but reducers are a
    replay boundary too.  Do not let ``model_construct``/legacy deserializers
    turn a wire-union member into an installed authority.
    """

    installed_lanes = _INSTALLED_GOAL_LANES_BY_OPERATION.get(payload.operation)
    if installed_lanes is None:
        raise ValueError(f"unsupported GoalAuthority operation {payload.operation!r}")
    if payload.authority_lane not in installed_lanes:
        raise ValueError(
            f"{GOAL_AUTHORITY_LANE_NOT_INSTALLED}: "
            "Goal authority lane is not installed for this operation"
        )
    expected_cause = _EXPECTED_GOAL_CAUSE_BY_LANE.get(payload.authority_lane)
    if expected_cause is None or not isinstance(payload.cause_authority, expected_cause):
        raise ValueError("Goal authority cause kind does not match its installed lane")

def reduce_v2_goal(
    goals: tuple[V2GoalProjection, ...],
    history: tuple[V2GoalTransitionProjection, ...],
    payload: V2GoalChangedPayload,
    *,
    event_type: str,
    event_id: str,
    logical_time: datetime,
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    random_draws: tuple[RandomDrawProjection, ...],
    world_occurrences: tuple[WorldOccurrenceProjection, ...],
    facts: tuple[FactProjection, ...] = (),
    experiences: tuple[ExperienceProjection, ...] = (),
    clock_transition_history: tuple[ClockTransitionProjection, ...] = (),
) -> tuple[tuple[V2GoalProjection, ...], tuple[V2GoalTransitionProjection, ...]]:
    require_goal_event_operation(event_type=event_type, operation=payload.operation)
    if payload.policy_refs != V2_GOAL_POLICY_REFS or (
        payload.policy_version != V2_GOAL_POLICY_VERSION
        or payload.policy_digest != V2_GOAL_POLICY_DIGEST
    ):
        raise ValueError("goal mutation references an uninstalled policy")
    _require_installed_goal_transition_authority(payload)
    after = payload.goal_after
    if (
        after.origin.accepted_event_ref != event_id
        or after.updated_at != logical_time
        or after.origin.policy_refs != V2_GOAL_POLICY_REFS
    ):
        raise ValueError("goal after image is not pinned to mutation event time")
    if any(item.transition_id == payload.transition_id for item in history):
        raise ValueError("goal transition identity already exists")
    if any(item.change_id == payload.change_id for item in history):
        raise ValueError("goal change identity already exists")

    source_privacy = _resolve_cause(
        payload,
        actor_authorities=actor_authorities,
        committed_events=committed_events,
        logical_time=logical_time,
        world_occurrences=world_occurrences,
        facts=facts,
        experiences=experiences,
    )
    additional_privacies = _resolve_additional_bases(
        payload,
        committed_events=committed_events,
        logical_time=logical_time,
        world_occurrences=world_occurrences,
        facts=facts,
        experiences=experiences,
    )
    if additional_privacies:
        source_privacy = max(
            tuple(item for item in (source_privacy, *additional_privacies) if item),
            key=_privacy_rank,
        )
    _resolve_random_draw(
        payload,
        random_draws=random_draws,
        committed_events=committed_events,
    )
    _validate_subjective_privacy(payload)

    current = next((item for item in goals if item.goal_id == after.goal_id), None)
    if payload.operation == "open":
        if current is not None:
            raise ValueError("goal identity already exists")
        if (
            payload.goal_before is not None
            or payload.expected_entity_revision != 0
            or after.entity_revision != 1
            or after.values.status != "active"
            or after.opened_at != logical_time
            or after.closed_at is not None
        ):
            raise ValueError("goal open must create one active revision")
        contract = after.values.completion_contract
        if contract is not None:
            _validate_installed_contract(
                contract,
                actor_ref=after.actor_ref,
                cutoff=payload.evaluated_world_revision,
            )
        if source_privacy is not None and _privacy_rank(
            after.values.privacy_class
        ) < _privacy_rank(source_privacy):
            raise ValueError("goal privacy is weaker than its source authority")
        supersedes = after.values.supersedes_goal_authority
        if supersedes is not None:
            target = next((item for item in goals if item.goal_id == supersedes.goal_id), None)
            accepted = next(
                (
                    item
                    for item in committed_events
                    if item.event_id == supersedes.accepted_event_ref
                ),
                None,
            )
            if (
                supersedes.goal_id == after.goal_id
                or target is None
                or target.actor_ref != after.actor_ref
                or target.values.status not in {"completed", "abandoned", "expired"}
                or target.entity_revision != supersedes.entity_revision
                or target.origin.accepted_event_ref != supersedes.accepted_event_ref
                or target.semantic_fingerprint != supersedes.target_head_semantic_hash
                or target.values.privacy_class != supersedes.privacy_class
                or _privacy_rank(after.values.privacy_class)
                < _privacy_rank(supersedes.privacy_class)
                or supersedes.actor_ref != target.actor_ref
                or accepted is None
                or accepted.world_revision != supersedes.accepted_world_revision
                or accepted.payload_hash != supersedes.accepted_payload_hash
                or any(
                    item.values.supersedes_goal_id == supersedes.goal_id for item in goals
                )
            ):
                raise ValueError("goal supersede lineage is not exact and unique")
        updated = (*goals, after)
    else:
        before = payload.goal_before
        if current is None or before is None or current != before:
            raise ValueError("goal before image does not match current authority")
        if (
            payload.expected_entity_revision != current.entity_revision
            or after.entity_revision != current.entity_revision + 1
        ):
            raise ValueError("goal entity revision compare-and-swap failed")
        if (
            after.goal_id != current.goal_id
            or after.actor_ref != current.actor_ref
            or after.opened_at != current.opened_at
            or after.values.outcome_ref != current.values.outcome_ref
            or after.values.supersedes_goal_id != current.values.supersedes_goal_id
            or after.values.supersedes_goal_authority
            != current.values.supersedes_goal_authority
        ):
            raise ValueError("goal transition changed immutable identity or outcome")
        if after.updated_at < current.updated_at:
            raise ValueError("goal transition time cannot move backward")
        if current.values.status in {"completed", "abandoned", "expired"} and (
            payload.operation != "compensate"
        ):
            raise ValueError("terminal goal cannot transition")
        if source_privacy is not None and _privacy_rank(
            after.values.privacy_class
        ) < _privacy_rank(source_privacy):
            raise ValueError("goal privacy is weaker than its source authority")
        if _privacy_rank(after.values.privacy_class) < _privacy_rank(
            current.values.privacy_class
        ):
            raise ValueError("goal transition cannot weaken established privacy")
        if payload.operation == "progress":
            _validate_progress(current, after, payload)
        elif payload.operation == "revise":
            _validate_revise(current, after, payload)
        elif payload.operation in {"pause", "resume", "abandon"}:
            _validate_lifecycle(current, after, payload)
        elif payload.operation == "block":
            _validate_block(current, after, payload)
        elif payload.operation == "unblock":
            _validate_unblock(current, after, payload)
        elif payload.operation == "complete":
            _validate_complete(
                current,
                after,
                payload,
                committed_events=committed_events,
                world_occurrences=world_occurrences,
                facts=facts,
            )
        elif payload.operation == "compensate":
            _validate_compensate(
                current,
                after,
                payload,
                history=history,
                committed_events=committed_events,
                source_privacy=source_privacy,
                facts=facts,
                world_occurrences=world_occurrences,
                clock_transition_history=clock_transition_history,
                logical_time=logical_time,
            )
        else:
            # ``V2GoalChangedPayload`` is a closed operation union.  Keep a
            # stable domain failure here for corrupt/replayed bypass payloads
            # rather than exposing an implementation-status exception.
            raise ValueError(f"unsupported GoalAuthority operation {payload.operation!r}")
        updated = tuple(after if item.goal_id == after.goal_id else item for item in goals)

    transition = V2GoalTransitionProjection(
        transition_id=payload.transition_id,
        goal_id=after.goal_id,
        entity_revision=after.entity_revision,
        operation=payload.operation,
        authority_lane=payload.authority_lane,
        selection_mode=payload.selection_mode,
        values_before=(payload.goal_before.values if payload.goal_before else None),
        values_after=after.values,
        semantic_fingerprint_after=after.semantic_fingerprint,
        change_id=payload.change_id,
        policy_refs=payload.policy_refs,
        accepted_event_ref=event_id,
        accepted_at=logical_time,
        cause_authority=payload.cause_authority,
        revise_kind=payload.revise_kind,
        progress_assessment=payload.progress_assessment,
        lifecycle_reason=payload.lifecycle_reason,
        completion_evidence=payload.completion_evidence,
        blocker_resolutions=payload.blocker_resolutions,
        terminal_reason=payload.terminal_reason,
        removed_blocker_fingerprints=payload.removed_blocker_fingerprints,
        random_draw_binding=payload.random_draw_binding,
        compensates_transition_id=(
            payload.compensation_target.target_transition_id
            if payload.compensation_target is not None
            else None
        ),
    )
    return updated, (*history, transition)


def reduce_v2_goal_expiry(
    goals: tuple[V2GoalProjection, ...],
    history: tuple[V2GoalTransitionProjection, ...],
    payload: V2GoalExpiredPayload,
    *,
    event_type: str,
    event_id: str,
    logical_time: datetime,
    clock_transition_history: tuple[ClockTransitionProjection, ...],
) -> tuple[tuple[V2GoalProjection, ...], tuple[V2GoalTransitionProjection, ...]]:
    if event_type != "V2GoalExpired":
        raise ValueError("goal expiry event type is not mechanical expiry")
    if payload.policy_refs != V2_GOAL_POLICY_REFS or (
        payload.policy_version != V2_GOAL_EXPIRY_POLICY_VERSION
        or payload.policy_digest != V2_GOAL_EXPIRY_POLICY_DIGEST
    ):
        raise ValueError("goal expiry references an uninstalled policy")
    before, after = payload.goal_before, payload.goal_after
    current = next((item for item in goals if item.goal_id == after.goal_id), None)
    if (
        current is None
        or before != current
        or payload.expected_entity_revision != current.entity_revision
        or after.entity_revision != current.entity_revision + 1
        or after.origin.accepted_event_ref != event_id
        or after.origin.policy_refs != V2_GOAL_POLICY_REFS
        or after.updated_at != logical_time
        or after.updated_at < current.updated_at
        or after.goal_id != current.goal_id
        or after.actor_ref != current.actor_ref
        or after.opened_at != current.opened_at
        or current.values.status not in {"active", "paused", "blocked"}
    ):
        raise ValueError("goal expiry before/after CAS is not exact")
    if any(item.transition_id == payload.transition_id for item in history) or any(
        item.change_id == payload.change_id for item in history
    ):
        raise ValueError("goal expiry transition identity already exists")

    latest = resolve_latest_clock(
        clock_transition_history,
        current_logical_time=logical_time,
    )
    cause = payload.cause_authority
    if (
        cause.clock_event_ref != latest.clock_event_ref
        or cause.clock_world_revision != latest.computed_world_revision
        or cause.clock_payload_hash != latest.payload_hash
        or cause.logical_time_from != latest.logical_time_from
        or cause.logical_time_to != latest.logical_time_to
        or cause.policy_version != latest.installed_policy_version
        or cause.policy_digest != latest.installed_policy_digest
        or payload.evaluated_world_revision != latest.computed_world_revision
    ):
        raise ValueError("goal expiry does not bind the exact latest Clock authority")
    due = current.values.due_window
    terminal = payload.terminal_reason
    if (
        due is None
        or logical_time < due.ends_at
        or not isinstance(terminal, V2GoalExpiredTerminalReason)
        or terminal.due_window != due
        or terminal.clock_projection_ref != latest.clock_event_ref
        or terminal.policy_digest != V2_GOAL_EXPIRY_POLICY_DIGEST
        or terminal.privacy_class != current.values.privacy_class
    ):
        raise ValueError("goal expiry is not due under the installed Clock policy")
    expected_removed = tuple(
        sorted(item.blocker_semantic_hash for item in current.values.blockers)
    )
    if payload.removed_blocker_fingerprints != expected_removed:
        raise ValueError("goal expiry did not explicitly clear exact current blockers")
    expected_values = current.values.model_copy(
        update={"status": "expired", "blockers": (), "terminal_reason": terminal}
    )
    if (
        after.values != expected_values
        or after.closed_at != logical_time
        or after.values.privacy_class != current.values.privacy_class
    ):
        raise ValueError("goal expiry after image is not the exact terminal state")

    updated = tuple(after if item.goal_id == after.goal_id else item for item in goals)
    transition = V2GoalTransitionProjection(
        transition_id=payload.transition_id,
        goal_id=after.goal_id,
        entity_revision=after.entity_revision,
        operation="expire",
        authority_lane="clock_runtime",
        selection_mode="direct",
        values_before=before.values,
        values_after=after.values,
        semantic_fingerprint_after=after.semantic_fingerprint,
        change_id=payload.change_id,
        policy_refs=payload.policy_refs,
        accepted_event_ref=event_id,
        accepted_at=logical_time,
        cause_authority=cause,
        terminal_reason=terminal,
        removed_blocker_fingerprints=payload.removed_blocker_fingerprints,
    )
    return updated, (*history, transition)


def _resolve_cause(
    payload: V2GoalChangedPayload,
    *,
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    logical_time: datetime,
    world_occurrences: tuple[WorldOccurrenceProjection, ...],
    facts: tuple[FactProjection, ...],
    experiences: tuple[ExperienceProjection, ...],
) -> str | None:
    cause = payload.cause_authority
    if isinstance(cause, DeliberativeCauseAuthority):
        basis = cause.basis
        if isinstance(basis, InternalIntentionBasis):
            allowed = {
                "open",
                "revise",
                "pause",
                "resume",
                "block",
                "unblock",
                "abandon",
            }
            if (
                payload.operation not in allowed
                or basis.actor_ref != payload.goal_after.actor_ref
                or basis.intention_kind not in {"goal_choice", "goal_governance"}
                or basis.evaluated_world_revision != payload.evaluated_world_revision
                or basis.logical_time != logical_time
                or basis.policy_version != V2_GOAL_INTERNAL_BASIS_POLICY_VERSION
                or basis.policy_digest != V2_GOAL_INTERNAL_BASIS_POLICY_DIGEST
            ):
                raise ValueError("internal intention cannot authorize this Goal transition")
            return max(
                (basis.privacy_class, basis.rationale.privacy_class),
                key=_privacy_rank,
            )
        if not isinstance(basis, CommittedEvidenceBasis):
            raise TypeError("unknown Goal deliberative basis")
        source_privacies: list[str] = []
        for source in basis.sources:
            if source.source_kind == "character_core":
                # CharacterCore is intentionally not an installed Goal
                # deliberative source in this bundle.  Its shared wire shape
                # is not a capability grant and accepting it would allow a
                # slow identity revision to impersonate a current outcome.
                raise ValueError(
                    f"{GOAL_CHARACTER_CORE_SOURCE_NOT_INSTALLED}: "
                    "character_core is not an installed Goal deliberative source"
                )
            committed = next(
                (item for item in committed_events if item.event_id == source.event_ref),
                None,
            )
            if (
                committed is None
                or committed.world_revision != source.world_revision
                or committed.payload_hash != source.payload_hash
                or source.world_revision > payload.evaluated_world_revision
            ):
                raise ValueError("goal deliberative cause lacks exact committed authority")
            if source.source_kind == "settled_world_event":
                if committed.event_type != "WorldOccurrenceSettled":
                    raise ValueError("settled deliberative source event type is not installed")
                occurrence = next(
                    (
                        item
                        for item in world_occurrences
                        if item.occurrence_id == source.source_entity_ref
                        and item.entity_revision == source.source_entity_revision
                        and item.settlement_event_ref == committed.event_id
                        and item.settlement_world_revision == committed.world_revision
                        and item.settlement_payload_hash == committed.payload_hash
                        and item.status == "settled"
                    ),
                    None,
                )
                if occurrence is None or (
                    payload.goal_after.actor_ref not in occurrence.participant_refs
                    and occurrence.visibility not in {"public", "shareable"}
                ):
                    raise ValueError("goal basis lacks accessible exact settled occurrence")
                source_privacies.append(occurrence.visibility)
            elif source.source_kind == "fact":
                fact = next(
                    (
                        item
                        for item in facts
                        if item.fact_id == source.source_entity_ref
                        and item.entity_revision == source.source_entity_revision
                    ),
                    None,
                )
                if (
                    committed.event_type not in {"FactCommitted", "FactCorrected"}
                    or fact is None
                    or fact.values.status != "active"
                    or fact.origin.accepted_event_ref != committed.event_id
                ):
                    raise ValueError("goal basis lacks exact active Fact authority")
                source_privacies.append(fact.values.privacy_class)
            elif source.source_kind == "experience":
                experience = next(
                    (
                        item
                        for item in experiences
                        if item.experience_id == source.source_entity_ref
                        and item.entity_revision == source.source_entity_revision
                    ),
                    None,
                )
                if (
                    committed.event_type != "ExperienceCommitted"
                    or experience is None
                    or experience.origin.accepted_event_ref != committed.event_id
                    or (
                        payload.goal_after.actor_ref
                        not in experience.values.participant_refs
                        and experience.values.privacy_class not in {"public", "shareable"}
                    )
                ):
                    raise ValueError("goal basis lacks accessible exact Experience authority")
                source_privacies.append(experience.values.privacy_class)
            elif source.source_kind in {"world_started", "clock_advanced"}:
                expected = {
                    "world_started": "WorldStarted",
                    "clock_advanced": "ClockAdvanced",
                }[source.source_kind]
                if committed.event_type != expected:
                    raise ValueError("goal internal world basis event type is not exact")
                source_privacies.append("private")
            else:
                raise ValueError("Goal deliberative source resolver is not installed")
        return max(source_privacies, key=_privacy_rank)
    if isinstance(cause, DomainOperatorAuthorityBinding):
        authority = next(
            (
                item
                for item in actor_authorities
                if item.authority_id == cause.authority_id
                and item.entity_revision == cause.authority_revision
            ),
            None,
        )
        committed = next(
            (
                item
                for item in committed_events
                if item.event_id == cause.authority_event_ref
            ),
            None,
        )
        if (
            authority is None
            or committed is None
            or committed.event_type
            not in {
                "ActorAuthorityBootstrapped",
                "ActorAuthorityRotated",
                "ActorAuthorityCompensated",
            }
            or committed.world_revision != cause.authority_world_revision
            or committed.payload_hash != cause.authority_payload_hash
            or authority.origin.event_ref != committed.event_id
            or authority.values.principal_ref != cause.principal_ref
            or authority.values.principal_kind != "deployment_operator"
            or authority.values.status != "active"
            or authority.values.valid_from > logical_time
            or (
                authority.values.expires_at is not None
                and authority.values.expires_at <= logical_time
            )
            or cause.required_operation != V2_GOAL_OPERATOR_OPERATION
            or cause.required_operation not in authority.values.allowed_operations
            or cause.authority_values_hash != _canonical_hash(authority.values)
            or authority.policy_version != "actor-authority-policy.2"
            or authority.policy_digest != ACTOR_AUTHORITY_V2_POLICY_DIGEST
            or cause.authority_policy_digest != authority.policy_digest
        ):
            raise ValueError("goal operator cause lacks active exact ActorAuthority")
        return None
    if isinstance(cause, SettledEventCauseAuthority):
        # A committed settlement remains usable through
        # ``CommittedEvidenceBasis`` in a later accepted deliberation.  It is
        # not a direct Goal write lane in the frozen .16 capability matrix.
        raise ValueError(
            f"{GOAL_SETTLEMENT_WRITE_AUTHORITY_NOT_INSTALLED}: "
            "goal settlement write authority is not installed; "
            "use an accepted deliberative committed-evidence basis"
        )
    if isinstance(cause, CompensationCauseAuthority):
        target_event = next(
            (
                item
                for item in committed_events
                if item.event_id == cause.target_accepted_event_ref
            ),
            None,
        )
        if (
            target_event is None
            or target_event.world_revision != cause.target_accepted_world_revision
            or target_event.payload_hash != cause.target_accepted_payload_hash
            or target_event.world_revision > payload.evaluated_world_revision
        ):
            raise ValueError("goal compensation target event is not exact and prior")
        correction_privacy = _resolve_compensation_correction_basis(
            payload,
            cause,
            committed_events=committed_events,
            logical_time=logical_time,
            world_occurrences=world_occurrences,
            facts=facts,
            experiences=experiences,
        )
        if cause.operator_authority is not None:
            temporary = payload.model_copy(
                update={"cause_authority": cause.operator_authority}
            )
            _resolve_cause(
                temporary,
                actor_authorities=actor_authorities,
                committed_events=committed_events,
                logical_time=logical_time,
                world_occurrences=world_occurrences,
                facts=facts,
                experiences=experiences,
            )
        return max(
            (correction_privacy, cause.correction_rationale.privacy_class, "private"),
            key=_privacy_rank,
        )
    if isinstance(cause, ClockCauseAuthority):
        # Expiry has a separate mechanical payload/reducer; it is never a
        # V2GoalChanged mutation and cannot be smuggled through this path.
        raise ValueError(
            f"{GOAL_CLOCK_WRITE_AUTHORITY_NOT_INSTALLED}: "
            "Clock authority is only installed for mechanical Goal expiry"
        )
    raise ValueError("Goal cause authority kind is not installed")


def _resolve_random_draw(
    payload: V2GoalChangedPayload,
    *,
    random_draws: tuple[RandomDrawProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
) -> None:
    binding = payload.random_draw_binding
    if binding is None:
        return
    del random_draws, committed_events
    raise ValueError("random_draw Goal selection is disabled until RandomAuthority is installed")


def _resolve_compensation_correction_basis(
    payload: V2GoalChangedPayload,
    cause: CompensationCauseAuthority,
    *,
    committed_events: tuple[CommittedWorldEventRef, ...],
    logical_time: datetime,
    world_occurrences: tuple[WorldOccurrenceProjection, ...],
    facts: tuple[FactProjection, ...],
    experiences: tuple[ExperienceProjection, ...],
) -> str:
    basis = cause.correction_basis
    if isinstance(basis, InternalIntentionBasis):
        if (
            basis.actor_ref != payload.goal_after.actor_ref
            or basis.intention_kind not in {"goal_choice", "goal_governance"}
            or basis.evaluated_world_revision != payload.evaluated_world_revision
            or basis.logical_time != logical_time
            or basis.policy_version != V2_GOAL_INTERNAL_BASIS_POLICY_VERSION
            or basis.policy_digest != V2_GOAL_INTERNAL_BASIS_POLICY_DIGEST
        ):
            raise ValueError("internal intention cannot authorize this Goal correction")
        return max(
            (basis.privacy_class, basis.rationale.privacy_class),
            key=_privacy_rank,
        )
    expiry_basis = basis if isinstance(basis, GoalExpiryCorrectionBasis) else None
    if expiry_basis is not None:
        if (
            expiry_basis.policy_version
            != V2_GOAL_EXPIRY_CORRECTION_POLICY_VERSION
            or expiry_basis.policy_digest
            != V2_GOAL_EXPIRY_CORRECTION_POLICY_DIGEST
            or expiry_basis.rationale != cause.correction_rationale
            or expiry_basis.operator_authority != cause.operator_authority
            or expiry_basis.correction_class
            not in {"due_window", "operator_import_error"}
            or any(
                item.world_revision <= cause.target_accepted_world_revision
                for item in expiry_basis.sources.sources
            )
        ):
            raise ValueError("goal expiry correction basis is not exact and post-target")
        basis = expiry_basis.sources
    if not isinstance(basis, CommittedEvidenceBasis):
        raise TypeError("unknown Goal correction basis")
    if len(basis.sources) == 1 and (
        basis.sources[0].event_ref == cause.target_accepted_event_ref
    ):
        raise ValueError("goal target event cannot be its sole correction basis")
    if any(
        item.source_kind not in {"settled_world_event", "fact", "experience"}
        for item in basis.sources
    ):
        raise ValueError("goal correction basis lacks an installed correction capability")
    privacies = []
    for source in basis.sources:
        committed = next(
            (item for item in committed_events if item.event_id == source.event_ref),
            None,
        )
        if (
            committed is None
            or committed.world_revision != source.world_revision
            or committed.payload_hash != source.payload_hash
            or committed.world_revision > payload.evaluated_world_revision
        ):
            raise ValueError("goal correction basis lacks exact committed authority")
        if source.source_kind == "fact":
            fact = next(
                (
                    item
                    for item in facts
                    if item.fact_id == source.source_entity_ref
                    and item.entity_revision == source.source_entity_revision
                ),
                None,
            )
            if (
                fact is None
                or fact.origin.accepted_event_ref != committed.event_id
                or committed.event_type
                not in {"FactCommitted", "FactCorrected", "FactWithdrawn"}
            ):
                raise ValueError("goal correction lacks exact current Fact authority")
            privacies.append(fact.values.privacy_class)
        elif source.source_kind == "settled_world_event":
            occurrence = next(
                (
                    item
                    for item in world_occurrences
                    if item.occurrence_id == source.source_entity_ref
                    and item.entity_revision == source.source_entity_revision
                    and item.settlement_event_ref == committed.event_id
                    and item.settlement_world_revision == committed.world_revision
                    and item.settlement_payload_hash == committed.payload_hash
                    and item.status == "settled"
                ),
                None,
            )
            if occurrence is None or (
                payload.goal_after.actor_ref not in occurrence.participant_refs
                and occurrence.visibility not in {"public", "shareable"}
            ):
                raise ValueError("goal correction lacks exact settled occurrence authority")
            privacies.append(occurrence.visibility)
        elif source.source_kind == "experience":
            experience = next(
                (
                    item
                    for item in experiences
                    if item.experience_id == source.source_entity_ref
                    and item.entity_revision == source.source_entity_revision
                ),
                None,
            )
            if (
                experience is None
                or experience.origin.accepted_event_ref != committed.event_id
                or committed.event_type != "ExperienceCommitted"
            ):
                raise ValueError("goal correction lacks exact Experience authority")
            privacies.append(experience.values.privacy_class)
        else:
            raise ValueError("goal correction basis lacks an installed correction capability")
    if expiry_basis is not None:
        expected_expiry_privacy = max(
            (*privacies, expiry_basis.rationale.privacy_class),
            key=_privacy_rank,
        )
        if expiry_basis.privacy_class != expected_expiry_privacy:
            raise ValueError("goal expiry correction privacy is not exactly derived")
        privacies.extend(
            (
                expiry_basis.privacy_class,
                expiry_basis.rationale.privacy_class,
            )
        )
    return max(privacies, key=_privacy_rank)


def _resolve_additional_bases(
    payload: V2GoalChangedPayload,
    *,
    committed_events: tuple[CommittedWorldEventRef, ...],
    logical_time: datetime,
    world_occurrences: tuple[WorldOccurrenceProjection, ...],
    facts: tuple[FactProjection, ...],
    experiences: tuple[ExperienceProjection, ...],
) -> tuple[str, ...]:
    entries: list[tuple[object, str | None]] = []
    before_ids = (
        {item.blocker_id for item in payload.goal_before.values.blockers}
        if payload.goal_before is not None
        else set()
    )
    if payload.operation == "block":
        entries.extend(
            (item.basis, item.blocker_id)
            for item in payload.goal_after.values.blockers
            if item.blocker_id not in before_ids
        )
    entries.extend((item.basis, None) for item in payload.blocker_resolutions)
    privacies = []
    for basis, blocker_id in entries:
        temporary = payload.model_copy(
            update={"cause_authority": DeliberativeCauseAuthority(basis=basis)}
        )
        privacy = _resolve_cause(
            temporary,
            actor_authorities=(),
            committed_events=committed_events,
            logical_time=logical_time,
            world_occurrences=world_occurrences,
            facts=facts,
            experiences=experiences,
        )
        if privacy is None:
            raise ValueError("subjective Goal basis did not resolve a privacy floor")
        if blocker_id is not None:
            blocker = next(
                item
                for item in payload.goal_after.values.blockers
                if item.blocker_id == blocker_id
            )
            blocker_floor = max(
                (privacy, blocker.rationale.privacy_class), key=_privacy_rank
            )
            if _privacy_rank(blocker.privacy_class) < _privacy_rank(blocker_floor):
                raise ValueError("goal blocker privacy is weaker than its basis")
        privacies.append(privacy)
    return tuple(privacies)


def _validate_progress(
    current: V2GoalProjection,
    after: V2GoalProjection,
    payload: V2GoalChangedPayload,
) -> None:
    before_values = current.values
    after_values = after.values
    if payload.progress_delta_bp is None or (
        before_values.progress_bp + payload.progress_delta_bp
        != after_values.progress_bp
    ):
        raise ValueError("goal progress delta does not conserve exact progress")
    frozen = (
        "outcome_ref",
        "importance_bp",
        "due_window",
        "blockers",
        "completion_contract",
        "status",
        "terminal_reason",
        "supersedes_goal_id",
        "supersedes_goal_authority",
    )
    if any(getattr(before_values, field) != getattr(after_values, field) for field in frozen):
        raise ValueError("goal progress cannot change lifecycle or contract fields")
    if after.closed_at is not None:
        raise ValueError("goal progress cannot close the goal")


def _validate_installed_contract(
    contract: V2GoalCompletionContract,
    *,
    actor_ref: str,
    cutoff: int,
) -> None:
    if (
        contract.expected_actor_ref != actor_ref
        or contract.evidence_cutoff_world_revision != cutoff
        or contract.policy_version != V2_GOAL_COMPLETION_CONTRACT_POLICY_VERSION
        or contract.policy_digest != V2_GOAL_COMPLETION_CONTRACT_POLICY_DIGEST
        or contract.contract_schema_ref
        != V2_GOAL_CONTRACT_SCHEMA_BY_KIND[contract.completion_kind]
        or contract.completion_parser_ref
        != V2_GOAL_EVIDENCE_PARSER_BY_KIND[contract.completion_kind]
        or contract.evidence_schema_ref
        != V2_GOAL_EVIDENCE_SCHEMA_BY_KIND[contract.completion_kind]
    ):
        raise ValueError("goal completion contract is not installed or cutoff-pinned")


def _validate_revise(
    current: V2GoalProjection,
    after: V2GoalProjection,
    payload: V2GoalChangedPayload,
) -> None:
    before_values = current.values
    after_values = after.values
    allowed_field = {
        "reprioritize": "importance_bp",
        "reschedule": "due_window",
        "recontract": "completion_contract",
    }[payload.revise_kind]  # type: ignore[index]
    fields = tuple(type(before_values).model_fields)
    changed = {
        field
        for field in fields
        if getattr(before_values, field) != getattr(after_values, field)
    }
    if (
        allowed_field not in changed
        or changed - {allowed_field, "privacy_class"}
        or after_values.status != before_values.status
    ):
        raise ValueError("goal revision changed fields outside its explicit revise kind")
    if payload.revise_kind == "recontract":
        before_contract = before_values.completion_contract
        after_contract = after_values.completion_contract
        if (
            after_contract is None
            or after_contract.evidence_cutoff_world_revision
            != payload.evaluated_world_revision
            or (
                before_contract is not None
                and (
                    after_contract.contract_id == before_contract.contract_id
                    or after_contract.evidence_cutoff_world_revision
                    <= before_contract.evidence_cutoff_world_revision
                )
            )
        ):
            raise ValueError("goal recontract must install a new current-cutoff contract")
        _validate_installed_contract(
            after_contract,
            actor_ref=after.actor_ref,
            cutoff=payload.evaluated_world_revision,
        )
    if after.closed_at is not None:
        raise ValueError("goal revision cannot close the goal")


def _validate_lifecycle(
    current: V2GoalProjection,
    after: V2GoalProjection,
    payload: V2GoalChangedPayload,
) -> None:
    reason = payload.lifecycle_reason
    if reason is None:
        raise ValueError("goal lifecycle transition lacks its typed reason")
    before_values = current.values
    if payload.operation == "pause":
        if before_values.status != "active":
            raise ValueError("only an active goal may pause")
        expected_values = before_values.model_copy(
            update={"status": "paused", "privacy_class": after.values.privacy_class}
        )
        if after.values != expected_values or after.closed_at is not None:
            raise ValueError("goal pause after image is not exact")
    elif payload.operation == "resume":
        if before_values.status != "paused":
            raise ValueError("only a paused goal may resume")
        expected_values = before_values.model_copy(
            update={"status": "active", "privacy_class": after.values.privacy_class}
        )
        if after.values != expected_values or after.closed_at is not None:
            raise ValueError("goal resume after image is not exact")
    else:
        terminal = payload.terminal_reason
        if not isinstance(terminal, V2GoalAbandonedTerminalReason) or (
            terminal.reason != reason
        ):
            raise ValueError("goal abandon terminal reason is not exact")
        expected_values = before_values.model_copy(
            update={
                "status": "abandoned",
                "blockers": (),
                "terminal_reason": terminal,
                "privacy_class": after.values.privacy_class,
            }
        )
        if after.values != expected_values or after.closed_at != after.updated_at:
            raise ValueError("goal abandon after image is not exact")


def _validate_block(
    current: V2GoalProjection,
    after: V2GoalProjection,
    payload: V2GoalChangedPayload,
) -> None:
    cause = payload.cause_authority
    if not isinstance(cause, DeliberativeCauseAuthority):
        raise ValueError("goal block requires deliberative authority")
    before_values = current.values
    after_values = after.values
    before_by_id = {item.blocker_id: item for item in before_values.blockers}
    after_by_id = {item.blocker_id: item for item in after_values.blockers}
    retained = set(before_by_id) & set(after_by_id)
    additions = set(after_by_id) - set(before_by_id)
    removals = set(before_by_id) - set(after_by_id)
    resolutions = {item.blocker_id: item for item in payload.blocker_resolutions}
    if (
        before_values.status not in {"active", "blocked"}
        or not additions
        or any(before_by_id[item] != after_by_id[item] for item in retained)
        or any(
            _privacy_rank(after_values.privacy_class)
            < _privacy_rank(after_by_id[item].rationale.privacy_class)
            for item in additions
        )
        or after_values.status != "blocked"
        or set(resolutions) != removals
        or any(
            resolutions[item].blocker_semantic_hash
            != before_by_id[item].blocker_semantic_hash
            for item in removals
        )
    ):
        raise ValueError("goal block must install an exact typed blocker diff")
    if before_values.status == "active" and removals:
        raise ValueError("initial goal block cannot remove blockers")
    frozen = (
        "outcome_ref",
        "importance_bp",
        "progress_bp",
        "due_window",
        "completion_contract",
        "terminal_reason",
        "supersedes_goal_id",
        "supersedes_goal_authority",
    )
    if any(
        getattr(before_values, field) != getattr(after_values, field)
        for field in frozen
    ) or after.closed_at is not None:
        raise ValueError("goal block changed fields outside blocker lifecycle")


def _validate_unblock(
    current: V2GoalProjection,
    after: V2GoalProjection,
    payload: V2GoalChangedPayload,
) -> None:
    before_values = current.values
    after_values = after.values
    before_by_id = {item.blocker_id: item for item in before_values.blockers}
    after_by_id = {item.blocker_id: item for item in after_values.blockers}
    removed = set(before_by_id) - set(after_by_id)
    retained = set(before_by_id) & set(after_by_id)
    resolutions = {item.blocker_id: item for item in payload.blocker_resolutions}
    if (
        before_values.status != "blocked"
        or not removed
        or set(after_by_id) - set(before_by_id)
        or any(before_by_id[item] != after_by_id[item] for item in retained)
        or set(resolutions) != removed
        or any(
            resolutions[item].blocker_semantic_hash
            != before_by_id[item].blocker_semantic_hash
            for item in removed
        )
        or after_values.status != ("blocked" if after_by_id else "active")
    ):
        raise ValueError("goal unblock must remove an exact non-empty blocker diff")
    frozen = (
        "outcome_ref",
        "importance_bp",
        "progress_bp",
        "due_window",
        "completion_contract",
        "terminal_reason",
        "supersedes_goal_id",
        "supersedes_goal_authority",
    )
    if any(
        getattr(before_values, field) != getattr(after_values, field)
        for field in frozen
    ) or after.closed_at is not None:
        raise ValueError("goal unblock changed fields outside blocker lifecycle")


def _validate_complete(
    current: V2GoalProjection,
    after: V2GoalProjection,
    payload: V2GoalChangedPayload,
    *,
    committed_events: tuple[CommittedWorldEventRef, ...],
    world_occurrences: tuple[WorldOccurrenceProjection, ...],
    facts: tuple[FactProjection, ...],
) -> None:
    contract = current.values.completion_contract
    evidence = payload.completion_evidence
    if contract is None or evidence is None:
        raise ValueError("goal completion requires an installed contract and evidence")
    _validate_installed_contract(
        contract,
        actor_ref=current.actor_ref,
        cutoff=contract.evidence_cutoff_world_revision,
    )
    committed = next(
        (item for item in committed_events if item.event_id == evidence.evidence_ref),
        None,
    )
    if (
        committed is None
        or committed.world_revision != evidence.evidence_world_revision
        or committed.payload_hash != evidence.evidence_payload_hash
        or committed.world_revision <= contract.evidence_cutoff_world_revision
        or committed.world_revision > payload.evaluated_world_revision
        or committed.event_type not in contract.allowed_settled_event_types
    ):
        raise ValueError("goal completion evidence is not an exact post-cutoff event")
    if isinstance(evidence, V2GoalOccurrenceCompletionEvidence):
        occurrence_matches = tuple(
            item
            for item in world_occurrences
            if item.occurrence_id == evidence.occurrence_id
        )
        occurrence = occurrence_matches[0] if len(occurrence_matches) == 1 else None
        if (
            contract.completion_kind != "settled_occurrence_outcome"
            or committed.event_type != "WorldOccurrenceSettled"
            or occurrence is None
            or occurrence.entity_revision != evidence.occurrence_entity_revision
            or occurrence.status != "settled"
            or occurrence.settlement_event_ref != committed.event_id
            or occurrence.settlement_world_revision != committed.world_revision
            or occurrence.settlement_payload_hash != committed.payload_hash
            or occurrence.settled_outcome_ref is None
            or evidence.evidence_schema_ref != contract.evidence_schema_ref
            or contract.expected_actor_ref not in occurrence.participant_refs
            or evidence.resolved_actor_ref != contract.expected_actor_ref
            or evidence.resolved_outcome_ref != occurrence.settled_outcome_ref
            or contract.outcome_ref != occurrence.settled_outcome_ref
            or evidence.privacy_class != occurrence.visibility
        ):
            raise ValueError("occurrence completion does not satisfy exact contract")
    elif isinstance(evidence, V2GoalFactCompletionEvidence):
        fact_matches = tuple(item for item in facts if item.fact_id == evidence.fact_id)
        fact = fact_matches[0] if len(fact_matches) == 1 else None
        if (
            contract.completion_kind != "active_fact_predicate"
            or committed.event_type not in {"FactCommitted", "FactCorrected"}
            or fact is None
            or fact.entity_revision != evidence.fact_entity_revision
            or fact.values.status != "active"
            or evidence.evidence_schema_ref != contract.evidence_schema_ref
            or fact.origin.accepted_event_ref != committed.event_id
            or fact.values.subject_ref != contract.expected_actor_ref
            or fact.values.value_ref != contract.outcome_ref
            or fact.values.predicate_code != contract.required_fact_predicate
            or fact.values.value_hash != contract.required_fact_value_hash
            or evidence.resolved_actor_ref != fact.values.subject_ref
            or evidence.resolved_outcome_ref != fact.values.value_ref
            or evidence.resolved_fact_predicate != fact.values.predicate_code
            or evidence.resolved_fact_value_hash != fact.values.value_hash
            or evidence.privacy_class != fact.values.privacy_class
        ):
            raise ValueError("Fact completion does not satisfy exact contract")
    else:
        raise TypeError("unknown Goal completion evidence kind")
    before_values = current.values
    terminal_reason = payload.terminal_reason
    if not isinstance(terminal_reason, V2GoalCompletedTerminalReason) or (
        terminal_reason.contract_id != contract.contract_id
        or terminal_reason.contract_digest != contract.contract_digest
        or terminal_reason.completion_evidence_ref != evidence.evidence_ref
        or after.values.terminal_reason != terminal_reason
    ):
        raise ValueError("goal completion requires a structured verified reason")
    expected_removed = tuple(
        sorted(item.blocker_semantic_hash for item in before_values.blockers)
    )
    if payload.removed_blocker_fingerprints != expected_removed:
        raise ValueError("goal completion did not explicitly clear current blockers")
    expected_values = before_values.model_copy(
        update={
            "status": "completed",
            "blockers": (),
            "terminal_reason": terminal_reason,
            "privacy_class": after.values.privacy_class,
        }
    )
    if after.values != expected_values or after.closed_at != after.updated_at:
        raise ValueError("goal completion after image is not the exact terminal transition")


def _validate_compensate(
    current: V2GoalProjection,
    after: V2GoalProjection,
    payload: V2GoalChangedPayload,
    *,
    history: tuple[V2GoalTransitionProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    source_privacy: str | None,
    facts: tuple[FactProjection, ...],
    world_occurrences: tuple[WorldOccurrenceProjection, ...],
    clock_transition_history: tuple[ClockTransitionProjection, ...],
    logical_time: datetime,
) -> None:
    cause = payload.compensation_target
    if cause is None:
        raise ValueError("goal compensation lacks its exact target")
    same_goal = tuple(item for item in history if item.goal_id == current.goal_id)
    target = next(
        (item for item in same_goal if item.transition_id == cause.target_transition_id),
        None,
    )
    target_event = next(
        (
            item
            for item in committed_events
            if item.event_id == cause.target_accepted_event_ref
        ),
        None,
    )
    effective_lane, effective_operation, original = _effective_compensation_authority(
        target, same_goal
    ) if target is not None else (None, None, None)
    expected_event_type = {
        "revise": "V2GoalRevised",
        "progress": "V2GoalProgressed",
        "pause": "V2GoalPaused",
        "resume": "V2GoalResumed",
        "block": "V2GoalBlocked",
        "unblock": "V2GoalUnblocked",
        "complete": "V2GoalCompleted",
        "abandon": "V2GoalAbandoned",
        "expire": "V2GoalExpired",
        "compensate": "V2GoalTransitionCompensated",
    }.get(target.operation if target is not None else "")
    if (
        target is None
        or not same_goal
        or same_goal[-1] != target
        or target.operation == "open"
        or target.entity_revision != cause.target_entity_revision
        or target.entity_revision != current.entity_revision
        or target.accepted_event_ref != cause.target_accepted_event_ref
        or effective_lane != cause.target_authority_lane
        or target_event is None
        or target_event.event_type != expected_event_type
        or target_event.world_revision != cause.target_accepted_world_revision
        or target_event.payload_hash != cause.target_accepted_payload_hash
        or target.values_before is None
        or current.values != target.values_after
        or any(
            item.compensates_transition_id == target.transition_id for item in same_goal
        )
    ):
        raise ValueError("goal compensation target is not exact latest transition")
    if isinstance(cause.correction_basis, GoalExpiryCorrectionBasis) and (
        effective_operation != "expire"
    ):
        raise ValueError("goal expiry correction authority cannot cross operation domains")
    requires_operator = effective_lane == "operator" or effective_operation == "expire"
    if effective_operation == "complete":
        if original is None:
            raise ValueError("goal completion compensation lineage is incomplete")
        if isinstance(original.completion_evidence, V2GoalFactCompletionEvidence):
            _validate_fact_completion_correction(
                target,
                original,
                cause,
                facts=facts,
                committed_events=committed_events,
            )
        elif isinstance(
            original.completion_evidence, V2GoalOccurrenceCompletionEvidence
        ):
            requires_operator = True
            if target.values_before.status == "completed":
                _validate_occurrence_completion_still_current(
                    original,
                    committed_events=committed_events,
                    world_occurrences=world_occurrences,
                )
        else:
            raise ValueError("goal completion compensation lacks typed original evidence")
    elif effective_operation == "expire":
        if original is None or not isinstance(
            original.cause_authority, ClockCauseAuthority
        ):
            raise ValueError("goal expiry compensation lacks original Clock authority")
        validate_clock_history(
            clock_transition_history,
            current_logical_time=logical_time,
        )
        original_clock = original.cause_authority
        expiry_basis = cause.correction_basis
        original_clock_event = next(
            (
                item
                for item in committed_events
                if item.event_id == original_clock.clock_event_ref
            ),
            None,
        )
        original_event = next(
            (
                item
                for item in committed_events
                if item.event_id == original.accepted_event_ref
            ),
            None,
        )
        if not isinstance(expiry_basis, GoalExpiryCorrectionBasis) or (
            expiry_basis.original_clock != original_clock
            or expiry_basis.target_expiry_transition_id != original.transition_id
            or expiry_basis.target_expiry_event_ref != original.accepted_event_ref
            or original_event is None
            or original_event.event_type != "V2GoalExpired"
            or expiry_basis.target_expiry_world_revision
            != original_event.world_revision
            or expiry_basis.target_expiry_payload_hash != original_event.payload_hash
            or original_clock_event is None
            or original_clock_event.event_type != "ClockAdvanced"
            or original_clock_event.world_revision
            != original_clock.clock_world_revision
            or original_clock_event.payload_hash != original_clock.clock_payload_hash
            or original_clock_event.logical_time != original_clock.logical_time_to
        ):
            raise ValueError("goal expiry compensation lacks typed correction authority")
        clock = next(
            (
                item
                for item in clock_transition_history
                if item.clock_event_ref == original_clock.clock_event_ref
                and item.computed_world_revision
                == original_clock.clock_world_revision
            ),
            None,
        )
        if (
            clock is None
            or clock.payload_hash != original_clock.clock_payload_hash
            or clock.logical_time_from != original_clock.logical_time_from
            or clock.logical_time_to != original_clock.logical_time_to
            or clock.installed_policy_version != original_clock.policy_version
            or clock.installed_policy_digest != original_clock.policy_digest
        ):
            raise ValueError("goal expiry compensation lost its exact original Clock")
    if requires_operator != (cause.operator_authority is not None):
        raise ValueError("goal compensation effective operator lane is not reauthorized")
    if effective_operation == "complete" and not isinstance(
        cause.correction_basis, CommittedEvidenceBasis
    ):
        raise ValueError("objective terminal compensation requires committed correction evidence")
    if effective_operation == "expire" and not isinstance(
        cause.correction_basis, GoalExpiryCorrectionBasis
    ):
        raise ValueError("goal expiry compensation requires typed correction evidence")
    restored_privacy = max(
        (
            current.values.privacy_class,
            target.values_before.privacy_class,
            cause.correction_rationale.privacy_class,
            source_privacy or "public",
            "private",
        ),
        key=_privacy_rank,
    )
    expected_values = target.values_before.model_copy(
        update={"privacy_class": restored_privacy}
    )
    expected_closed_at = (
        after.updated_at
        if expected_values.status in {"completed", "abandoned", "expired"}
        else None
    )
    if after.values != expected_values or after.closed_at != expected_closed_at:
        raise ValueError("goal compensation must restore exact prior values and privacy floor")


def _effective_compensation_authority(
    target: V2GoalTransitionProjection,
    history: tuple[V2GoalTransitionProjection, ...],
) -> tuple[str, str, V2GoalTransitionProjection]:
    by_id = {item.transition_id: item for item in history}
    current = target
    visited: set[str] = set()
    while current.operation == "compensate" or current.authority_lane == "compensation":
        if (
            current.transition_id in visited
            or current.operation != "compensate"
            or current.authority_lane != "compensation"
        ):
            raise ValueError("goal compensation authority lineage is invalid or cyclic")
        visited.add(current.transition_id)
        parent_id = current.compensates_transition_id
        parent = by_id.get(parent_id or "")
        if parent is None or parent.goal_id != target.goal_id:
            raise ValueError("goal compensation authority lineage is incomplete")
        current = parent
    return current.authority_lane, current.operation, current


def _validate_fact_completion_correction(
    target: V2GoalTransitionProjection,
    original: V2GoalTransitionProjection,
    cause: CompensationCauseAuthority,
    *,
    facts: tuple[FactProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
) -> None:
    evidence = original.completion_evidence
    contract = original.values_after.completion_contract
    if (
        not isinstance(evidence, V2GoalFactCompletionEvidence)
        or contract is None
        or not isinstance(cause.correction_basis, CommittedEvidenceBasis)
    ):
        raise ValueError("Fact completion correction lacks its original contract")
    matches = tuple(item for item in facts if item.fact_id == evidence.fact_id)
    current_fact = matches[0] if len(matches) == 1 else None
    if current_fact is None:
        raise ValueError("Fact completion correction lacks one current Fact head")
    source = next(
        (
            item
            for item in cause.correction_basis.sources
            if item.source_kind == "fact"
            and item.source_entity_ref == current_fact.fact_id
            and item.source_entity_revision == current_fact.entity_revision
            and item.event_ref == current_fact.origin.accepted_event_ref
        ),
        None,
    )
    committed = next(
        (
            item
            for item in committed_events
            if source is not None and item.event_id == source.event_ref
        ),
        None,
    )
    still_satisfies = (
        current_fact.values.status == "active"
        and current_fact.values.subject_ref == contract.expected_actor_ref
        and current_fact.values.value_ref == contract.outcome_ref
        and current_fact.values.predicate_code == contract.required_fact_predicate
        and current_fact.values.value_hash == contract.required_fact_value_hash
    )
    if (
        source is None
        or committed is None
        or committed.event_type not in {"FactCorrected", "FactWithdrawn"}
        or committed.world_revision != source.world_revision
        or committed.payload_hash != source.payload_hash
    ):
        raise ValueError(
            "Fact completion correction lacks exact current correction authority"
        )
    restores_completed = target.values_before.status == "completed"
    if restores_completed:
        raise ValueError(
            "Fact completion terminal evidence cannot be rebound by compensation; "
            "use a new completion transition"
        )
    if still_satisfies:
        raise ValueError(
            "Fact completion correction does not invalidate the original contract"
        )


def _validate_occurrence_completion_still_current(
    original: V2GoalTransitionProjection,
    *,
    committed_events: tuple[CommittedWorldEventRef, ...],
    world_occurrences: tuple[WorldOccurrenceProjection, ...],
) -> None:
    evidence = original.completion_evidence
    contract = original.values_after.completion_contract
    if not isinstance(evidence, V2GoalOccurrenceCompletionEvidence) or contract is None:
        raise ValueError("occurrence completion lineage lacks original exact evidence")
    matches = tuple(
        item
        for item in world_occurrences
        if item.occurrence_id == evidence.occurrence_id
    )
    occurrence = matches[0] if len(matches) == 1 else None
    committed = next(
        (item for item in committed_events if item.event_id == evidence.evidence_ref),
        None,
    )
    if (
        occurrence is None
        or committed is None
        or committed.event_type != "WorldOccurrenceSettled"
        or committed.world_revision != evidence.evidence_world_revision
        or committed.payload_hash != evidence.evidence_payload_hash
        or occurrence.entity_revision != evidence.occurrence_entity_revision
        or occurrence.status != "settled"
        or occurrence.settlement_event_ref != committed.event_id
        or occurrence.settlement_world_revision != committed.world_revision
        or occurrence.settlement_payload_hash != committed.payload_hash
        or occurrence.settled_outcome_ref != evidence.resolved_outcome_ref
        or contract.outcome_ref != evidence.resolved_outcome_ref
        or contract.expected_actor_ref != evidence.resolved_actor_ref
        or evidence.resolved_actor_ref not in occurrence.participant_refs
        or evidence.privacy_class != occurrence.visibility
        or evidence.evidence_schema_ref != contract.evidence_schema_ref
    ):
        raise ValueError("occurrence completion evidence is no longer exact and current")


def _validate_subjective_privacy(payload: V2GoalChangedPayload) -> None:
    privacy = payload.goal_after.values.privacy_class
    privacy_sources = []
    cause = payload.cause_authority
    if isinstance(cause, DeliberativeCauseAuthority) and isinstance(
        cause.basis, InternalIntentionBasis
    ):
        privacy_sources.append(cause.basis.rationale.privacy_class)
    if isinstance(cause, CompensationCauseAuthority):
        privacy_sources.append(cause.correction_rationale.privacy_class)
        if isinstance(cause.correction_basis, InternalIntentionBasis):
            privacy_sources.append(cause.correction_basis.rationale.privacy_class)
    if payload.progress_assessment is not None:
        privacy_sources.append(payload.progress_assessment.rationale.privacy_class)
    if payload.goal_after.values.completion_contract is not None:
        privacy_sources.append(
            payload.goal_after.values.completion_contract.privacy_class
        )
    if payload.completion_evidence is not None:
        privacy_sources.append(payload.completion_evidence.privacy_class)
    if payload.lifecycle_reason is not None:
        privacy_sources.extend(
            (
                payload.lifecycle_reason.privacy_class,
                payload.lifecycle_reason.rationale.privacy_class,
            )
        )
    for blocker in payload.goal_after.values.blockers:
        privacy_sources.extend((blocker.privacy_class, blocker.rationale.privacy_class))
    for resolution in payload.blocker_resolutions:
        privacy_sources.append(resolution.rationale.privacy_class)
    terminal = payload.goal_after.values.terminal_reason
    if terminal is not None:
        if terminal.terminal_kind == "abandoned":
            privacy_sources.extend(
                (
                    terminal.reason.privacy_class,
                    terminal.reason.rationale.privacy_class,
                )
            )
        else:
            privacy_sources.append(terminal.privacy_class)
    if any(_privacy_rank(privacy) < _privacy_rank(item) for item in privacy_sources):
        raise ValueError("goal privacy is weaker than its subjective rationale")


def _canonical_hash(value: object) -> str:
    material = value.model_dump(mode="json")  # type: ignore[attr-defined]
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _privacy_rank(value: str) -> int:
    return {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}[
        value
    ]
