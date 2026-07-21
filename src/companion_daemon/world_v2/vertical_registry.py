"""Closed registry of every bounded decision vertical (hand-rolled included).

The scattered enumerations stay authoritative and hard-coded — the
``process_kind`` Literal, the reducers' claim/open whitelists, the
event-identity claim set and the production proposal grammar are the replay
frozen roots and are deliberately *not* generated from this table.  This
module adds the missing cross-check: one declarative row per vertical, and
:func:`assert_bounded_vertical_coverage`, which compares every scattered
enumeration against the registry and **names the file that drifted**.

Owner decision 4 (2026-07-20) makes registration a hard gate: every vertical
(including ``hand_rolled=True`` wells) must own a row here, and a
non-hand-rolled row must resolve to a real :class:`VerticalSpec`.  The escape
hatch stays: any well may remain hand-written by registering
``hand_rolled=True``; the assertion only guarantees the declaration is
complete, never the implementation style.

The assertion runs in tests and at composition-root startup (mirroring
``assert_production_proposal_grammar_coverage``), so "a gate without a
reviewer" class incidents surface as a refused start naming the missing
wiring instead of an Opened-only backlog discovered in the ledger.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Literal, get_args

from .schemas import TriggerProcess


_WORLD_V2 = Path(__file__).resolve().parent

VerticalShape = Literal[
    "anchored_trigger",  # A: durable event-anchored TriggerProcess with lease
    "daily_check",       # B: clock-check identity, durable check events
    "inline_once",       # C: same-turn, audit-prefix dedupe, silent failures
    "infrastructure",    # ledger/runtime plumbing kinds, not decision lanes
]


@dataclass(frozen=True, slots=True)
class VerticalRegistration:
    """One vertical's complete declaration; the assertion audits every field."""

    lane_id: str
    summary: str
    shape: VerticalShape
    hand_rolled: bool
    module: str
    # TriggerProcess kinds this vertical owns (may be empty for B/C shapes
    # whose durability lives in check events or audit-prefix dedupe).
    process_kinds: tuple[str, ...] = ()
    # Production proposal grammar lanes this vertical speaks through.
    grammar_lanes: tuple[str, ...] = ()
    # Scattered-enumeration facts, cross-checked against the actual sources.
    lease_starts_at_logical_time: tuple[str, ...] = ()
    open_before_claim_enforced: tuple[str, ...] = ()
    opened_identity_reducer_validated: tuple[str, ...] = ()
    claim_identity_from_domain_key: tuple[str, ...] = ()
    may_carry_source_evidence: tuple[str, ...] = ()
    # Substrings that must appear in runtime.py (drain chain) and
    # production_turn_application.py (composition root assembly).
    runtime_drain_markers: tuple[str, ...] = ()
    composition_markers: tuple[str, ...] = ()
    drain_site: str = ""
    # For framework-backed verticals: where the frozen VerticalSpec lives.
    spec_module: str | None = None
    spec_builder: str | None = None
    notes: tuple[str, ...] = field(default=())


VERTICAL_REGISTRY: tuple[VerticalRegistration, ...] = (
    # ------------------------------------------------------------------
    # Framework-backed pilots (BoundedDecisionVertical P1)
    # ------------------------------------------------------------------
    VerticalRegistration(
        lane_id="quick_reaction",
        summary="Same-turn bounded QQ reaction on the just-committed message",
        shape="inline_once",
        hand_rolled=False,
        module="quick_reaction_vertical.py",
        process_kinds=(),
        grammar_lanes=("quick_reaction",),
        runtime_drain_markers=("self._quick_reaction_worker.run_observation",),
        composition_markers=(
            "QuickReactionWorker if _bdv_pilot_disabled() else QuickReactionVerticalWorker",
            "quick_reaction_class(",
        ),
        drain_site="WorldRuntime.ingest (inline, before reply cursor pin)",
        spec_module="companion_daemon.world_v2.quick_reaction_vertical",
        spec_builder="quick_reaction_spec",
        notes=(
            "Hand-written twin quick_reaction.py stays frozen in tree for the"
            " WORLD_V2_BDV_PILOT_DISABLED hot rollback window.",
        ),
    ),
    VerticalRegistration(
        lane_id="afterthought",
        summary="One optional recorded tail after her settled reply",
        shape="anchored_trigger",
        hand_rolled=False,
        module="afterthought_author_vertical.py",
        process_kinds=("afterthought_author",),
        grammar_lanes=("proactive",),
        may_carry_source_evidence=("afterthought_author",),
        runtime_drain_markers=("self._afterthought_author.drain_one",),
        composition_markers=(
            "AfterthoughtAuthorRuntime",
            "AfterthoughtVerticalRuntime",
            "afterthought_runtime = afterthought_class(",
        ),
        drain_site="WorldRuntime.drain_background_once (early, short horizon)",
        spec_module="companion_daemon.world_v2.afterthought_author_vertical",
        spec_builder="AfterthoughtVerticalRuntime",
        notes=(
            "Hand-written twin afterthought_author.py stays frozen in tree for"
            " the WORLD_V2_BDV_PILOT_DISABLED hot rollback window.",
        ),
    ),
    # ------------------------------------------------------------------
    # Hand-rolled A-shape wells (event-anchored durable triggers)
    # ------------------------------------------------------------------
    VerticalRegistration(
        lane_id="silence_appraisal",
        summary="Per-delivered-reply silence anchor appraisal",
        shape="anchored_trigger",
        hand_rolled=True,
        module="silence_appraisal_trigger.py",
        process_kinds=("silence_appraisal",),
        grammar_lanes=("silence_appraisal",),
        lease_starts_at_logical_time=("silence_appraisal",),
        open_before_claim_enforced=("silence_appraisal",),
        opened_identity_reducer_validated=("silence_appraisal",),
        claim_identity_from_domain_key=("silence_appraisal",),
        may_carry_source_evidence=("silence_appraisal",),
        runtime_drain_markers=(
            "SilenceAppraisalTriggerOpener(",
            "SilenceAppraisalTriggerRuntime(",
        ),
        composition_markers=("silence_appraisal_turn",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="plan_disruption_appraisal",
        summary="Per-abandoned-plan disruption appraisal",
        shape="anchored_trigger",
        hand_rolled=True,
        module="plan_disruption_appraisal_trigger.py",
        process_kinds=("plan_disruption_appraisal",),
        grammar_lanes=("plan_disruption_appraisal",),
        lease_starts_at_logical_time=("plan_disruption_appraisal",),
        open_before_claim_enforced=("plan_disruption_appraisal",),
        opened_identity_reducer_validated=("plan_disruption_appraisal",),
        claim_identity_from_domain_key=("plan_disruption_appraisal",),
        may_carry_source_evidence=("plan_disruption_appraisal",),
        runtime_drain_markers=(
            "PlanDisruptionAppraisalTriggerOpener(",
            "PlanDisruptionAppraisalTriggerRuntime(",
        ),
        composition_markers=("plan_disruption_appraisal_turn",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="interaction_appraisal",
        summary="Per-observed-message interaction appraisal",
        shape="anchored_trigger",
        hand_rolled=True,
        module="appraisal_trigger.py",
        process_kinds=("interaction_appraisal",),
        grammar_lanes=("interaction_appraisal",),
        lease_starts_at_logical_time=("interaction_appraisal",),
        open_before_claim_enforced=("interaction_appraisal",),
        opened_identity_reducer_validated=("interaction_appraisal",),
        claim_identity_from_domain_key=("interaction_appraisal",),
        may_carry_source_evidence=("interaction_appraisal",),
        runtime_drain_markers=("InteractionAppraisalTriggerRuntime(",),
        composition_markers=("interaction_appraisal_turn=",),
        drain_site="WorldRuntime.ingest (immediate path) and drain_background_once",
    ),
    VerticalRegistration(
        lane_id="npc_world_appraisal",
        summary="Settled world occurrence appraisal (her subjectivity)",
        shape="anchored_trigger",
        hand_rolled=True,
        module="npc_world_appraisal_trigger_runtime.py",
        process_kinds=("npc_world_appraisal",),
        grammar_lanes=("settled_world_appraisal",),
        lease_starts_at_logical_time=("npc_world_appraisal",),
        open_before_claim_enforced=("npc_world_appraisal",),
        opened_identity_reducer_validated=("npc_world_appraisal",),
        claim_identity_from_domain_key=("npc_world_appraisal",),
        may_carry_source_evidence=("npc_world_appraisal",),
        runtime_drain_markers=("NpcWorldAppraisalTriggerRuntime(",),
        composition_markers=("npc_world_appraisal_turn",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="interaction_fact",
        summary="Source-bound user fact extraction per observed message",
        shape="anchored_trigger",
        hand_rolled=True,
        module="fact_trigger.py",
        process_kinds=("interaction_fact",),
        lease_starts_at_logical_time=("interaction_fact",),
        open_before_claim_enforced=("interaction_fact",),
        opened_identity_reducer_validated=("interaction_fact",),
        claim_identity_from_domain_key=("interaction_fact",),
        may_carry_source_evidence=("interaction_fact",),
        runtime_drain_markers=("InteractionFactTriggerRuntime(",),
        composition_markers=("fact_acceptance=",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="private_impression",
        summary="Internal-only impression from an accepted appraisal",
        shape="anchored_trigger",
        hand_rolled=True,
        module="private_impression_producer.py",
        process_kinds=("private_impression_deliberation",),
        lease_starts_at_logical_time=("private_impression_deliberation",),
        open_before_claim_enforced=("private_impression_deliberation",),
        opened_identity_reducer_validated=("private_impression_deliberation",),
        claim_identity_from_domain_key=("private_impression_deliberation",),
        may_carry_source_evidence=("private_impression_deliberation",),
        runtime_drain_markers=(
            "PrivateImpressionTriggerOpener(",
            "PrivateImpressionTriggerRuntime(",
        ),
        composition_markers=("private_impression_adapter=",),
        drain_site="WorldRuntime.drain_background_once",
        notes=("Uses the one-corrective-retry bounded model failure policy.",),
    ),
    VerticalRegistration(
        lane_id="affect",
        summary="Affect episode deliberation from an accepted appraisal",
        shape="anchored_trigger",
        hand_rolled=True,
        module="affect_trigger.py",
        process_kinds=("affect_deliberation",),
        grammar_lanes=("affect",),
        lease_starts_at_logical_time=("affect_deliberation",),
        open_before_claim_enforced=("affect_deliberation",),
        opened_identity_reducer_validated=("affect_deliberation",),
        claim_identity_from_domain_key=("affect_deliberation",),
        may_carry_source_evidence=("affect_deliberation",),
        runtime_drain_markers=("AffectTriggerRuntime(",),
        composition_markers=("affect_worker=",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="relationship",
        summary="Relationship deliberation from an accepted appraisal",
        shape="anchored_trigger",
        hand_rolled=True,
        module="relationship_trigger.py",
        process_kinds=("relationship_deliberation",),
        grammar_lanes=("relationship",),
        lease_starts_at_logical_time=("relationship_deliberation",),
        open_before_claim_enforced=("relationship_deliberation",),
        opened_identity_reducer_validated=("relationship_deliberation",),
        claim_identity_from_domain_key=("relationship_deliberation",),
        may_carry_source_evidence=("relationship_deliberation",),
        runtime_drain_markers=("RelationshipTriggerRuntime(",),
        composition_markers=("relationship_worker=",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="relationship_adjustment",
        summary="Deterministic relationship adjustment per accepted signal",
        shape="anchored_trigger",
        hand_rolled=True,
        module="relationship_adjustment_trigger.py",
        process_kinds=("relationship_adjustment",),
        lease_starts_at_logical_time=("relationship_adjustment",),
        open_before_claim_enforced=("relationship_adjustment",),
        opened_identity_reducer_validated=("relationship_adjustment",),
        claim_identity_from_domain_key=("relationship_adjustment",),
        may_carry_source_evidence=("relationship_adjustment",),
        runtime_drain_markers=("RelationshipAdjustmentTriggerRuntime(",),
        composition_markers=("relationship_adjustment_worker=",),
        drain_site="WorldRuntime.drain_background_once",
        notes=("No model: a deterministic compiler owns the whole deliberation.",),
    ),
    VerticalRegistration(
        lane_id="outcome",
        summary="World occurrence outcome deliberation",
        shape="anchored_trigger",
        hand_rolled=True,
        module="outcome_trigger.py",
        process_kinds=("outcome_deliberation",),
        grammar_lanes=("outcome",),
        lease_starts_at_logical_time=("outcome_deliberation",),
        open_before_claim_enforced=("outcome_deliberation",),
        opened_identity_reducer_validated=("outcome_deliberation",),
        claim_identity_from_domain_key=("outcome_deliberation",),
        may_carry_source_evidence=("outcome_deliberation",),
        runtime_drain_markers=("OutcomeTriggerRuntime(",),
        composition_markers=("outcome_deliberation_turn=",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="interaction_bid",
        summary="Media-delivery interaction bid deliberation",
        shape="anchored_trigger",
        hand_rolled=True,
        module="interaction_bid_trigger_runtime.py",
        process_kinds=("media_delivery_interaction",),
        grammar_lanes=("interaction_bid",),
        opened_identity_reducer_validated=("media_delivery_interaction",),
        claim_identity_from_domain_key=("media_delivery_interaction",),
        may_carry_source_evidence=("media_delivery_interaction",),
        runtime_drain_markers=("InteractionBidTriggerRuntime(",),
        composition_markers=("interaction_bid_turn=",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="expression_reconsideration",
        summary="Frozen-beat reconsideration gate per user interjection",
        shape="anchored_trigger",
        hand_rolled=True,
        module="expression_reconsideration.py",
        process_kinds=("expression_reconsideration",),
        lease_starts_at_logical_time=("expression_reconsideration",),
        open_before_claim_enforced=("expression_reconsideration",),
        opened_identity_reducer_validated=("expression_reconsideration",),
        claim_identity_from_domain_key=("expression_reconsideration",),
        may_carry_source_evidence=("expression_reconsideration",),
        runtime_drain_markers=("ExpressionReconsiderationRuntime(",),
        composition_markers=("expression_reconsideration_reviewer",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="external_result",
        summary="Accepted tool result deliberation",
        shape="anchored_trigger",
        hand_rolled=True,
        module="external_result_trigger_runtime.py",
        process_kinds=("external_result_deliberation",),
        opened_identity_reducer_validated=("external_result_deliberation",),
        claim_identity_from_domain_key=("external_result_deliberation",),
        may_carry_source_evidence=("external_result_deliberation",),
        runtime_drain_markers=("ExternalResultTriggerRuntime(",),
        composition_markers=("external_result_deliberator=",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="read_only_tool",
        summary="Read-only tool query deliberation",
        shape="anchored_trigger",
        hand_rolled=True,
        module="read_only_tool_trigger_runtime.py",
        process_kinds=("read_only_tool_deliberation",),
        may_carry_source_evidence=("read_only_tool_deliberation",),
        runtime_drain_markers=("self._read_only_tool_trigger_runtime.drain_one",),
        composition_markers=("read_only_tool_trigger_runtime",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="perception",
        summary="Perception (vision/transcription) deliberation",
        shape="anchored_trigger",
        hand_rolled=True,
        module="perception_trigger_runtime.py",
        process_kinds=("perception_deliberation",),
        may_carry_source_evidence=("perception_deliberation",),
        runtime_drain_markers=("self._perception_trigger_runtime.drain_one",),
        composition_markers=("perception_trigger_runtime",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="perception_result",
        summary="Perception result deliberation",
        shape="anchored_trigger",
        hand_rolled=True,
        module="perception_result_trigger_runtime.py",
        process_kinds=("perception_result_deliberation",),
        may_carry_source_evidence=("perception_result_deliberation",),
        runtime_drain_markers=("PerceptionResultTriggerRuntime(",),
        composition_markers=("perception_result_deliberator=",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="social_action",
        summary="Delayed social effect of an observed message",
        shape="anchored_trigger",
        hand_rolled=True,
        module="social_action_worker.py",
        process_kinds=("social_action_deliberation",),
        opened_identity_reducer_validated=("social_action_deliberation",),
        claim_identity_from_domain_key=("social_action_deliberation",),
        may_carry_source_evidence=("social_action_deliberation",),
        runtime_drain_markers=("self._social_action_worker.drain_one",),
        composition_markers=("social_action_worker=",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="memory_candidate_review",
        summary="Withdrawn-fact memory candidate review",
        shape="anchored_trigger",
        hand_rolled=True,
        module="memory_withdrawal_review.py",
        process_kinds=("memory_candidate_review",),
        lease_starts_at_logical_time=("memory_candidate_review",),
        open_before_claim_enforced=("memory_candidate_review",),
        opened_identity_reducer_validated=("memory_candidate_review",),
        claim_identity_from_domain_key=("memory_candidate_review",),
        may_carry_source_evidence=("memory_candidate_review",),
        runtime_drain_markers=("self._memory_withdrawal_review.drain_one",),
        composition_markers=("memory_withdrawal_review=",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="proactive_action",
        summary="Evidence-bound proactive initiative deliberation",
        shape="anchored_trigger",
        hand_rolled=True,
        module="proactive_action.py",
        process_kinds=("proactive_action_deliberation",),
        grammar_lanes=("proactive",),
        may_carry_source_evidence=("proactive_action_deliberation",),
        runtime_drain_markers=("self._proactive_action_runtime.drain_one",),
        composition_markers=("proactive_action_runtime=",),
        drain_site="WorldRuntime.drain_background_once",
    ),
    VerticalRegistration(
        lane_id="life_ecology",
        summary="Life ecology followup coordination trigger",
        shape="anchored_trigger",
        hand_rolled=True,
        module="life_ecology_runtime.py",
        process_kinds=("life_ecology",),
        lease_starts_at_logical_time=("life_ecology",),
        open_before_claim_enforced=("life_ecology",),
        opened_identity_reducer_validated=("life_ecology",),
        claim_identity_from_domain_key=("life_ecology",),
        may_carry_source_evidence=("life_ecology",),
        composition_markers=("LifeEcologyRuntime(", "advance_life_ecology_once"),
        drain_site="WorldV2TurnApplication.tick -> advance_life_ecology_once",
    ),
    VerticalRegistration(
        lane_id="media_continuation",
        summary="Settled media plan/artifact continuation",
        shape="anchored_trigger",
        hand_rolled=True,
        module="media_execution_runtime.py",
        process_kinds=("media_continuation",),
        opened_identity_reducer_validated=("media_continuation",),
        may_carry_source_evidence=("media_continuation",),
        composition_markers=("MediaContinuationWorker(",),
        drain_site="WorldV2TurnApplication.drain_media_continuation_once",
    ),
    VerticalRegistration(
        lane_id="media_repair",
        summary="Repairable media inspection failure repair",
        shape="anchored_trigger",
        hand_rolled=True,
        module="media_execution_runtime.py",
        process_kinds=("media_repair",),
        opened_identity_reducer_validated=("media_repair",),
        may_carry_source_evidence=("media_repair",),
        composition_markers=("MediaExecutionWorker(",),
        drain_site="WorldV2TurnApplication.drain_media_results_once",
    ),
    # ------------------------------------------------------------------
    # Hand-rolled B-shape wells (clock checks, no TriggerProcess)
    # ------------------------------------------------------------------
    VerticalRegistration(
        lane_id="npc_initiative",
        summary="Reviewed NPC enters her day uninvited (daily 2 slots)",
        shape="daily_check",
        hand_rolled=True,
        module="npc_initiative.py",
        composition_markers=("NpcInitiativeRuntime(", "npc_initiative_followup="),
        drain_site="LifeEcologyRuntime followup on clock wake",
    ),
    VerticalRegistration(
        lane_id="aspiration",
        summary="Aspiration seeding and maintenance draws (daily check)",
        shape="daily_check",
        hand_rolled=True,
        module="aspiration_runtime.py",
        composition_markers=("AspirationRuntime(", "aspiration_followup="),
        drain_site="LifeEcologyRuntime followup on clock wake",
    ),
    VerticalRegistration(
        lane_id="shared_private_invitation",
        summary="Shared-private future opening invitation (daily check)",
        shape="daily_check",
        hand_rolled=True,
        module="shared_private_invitation.py",
        composition_markers=(
            "SharedPrivateInvitationRuntime(",
            "shared_private_followup=",
        ),
        drain_site="LifeEcologyRuntime followup on clock wake",
    ),
    VerticalRegistration(
        lane_id="future_life_author",
        summary="One successful future plan authored per local day",
        shape="daily_check",
        hand_rolled=True,
        module="future_life_author.py",
        composition_markers=(
            "FutureLifeAuthorRuntime(",
            "future_life_author_followup=",
        ),
        drain_site="LifeEcologyRuntime followup on clock wake",
    ),
    # ------------------------------------------------------------------
    # Infrastructure process kinds (not decision verticals)
    # ------------------------------------------------------------------
    VerticalRegistration(
        lane_id="chat_reply",
        summary="The visible reply turn (main deliberation lane)",
        shape="infrastructure",
        hand_rolled=True,
        module="runtime.py",
        process_kinds=("observation",),
        grammar_lanes=("chat_reply",),
        runtime_drain_markers=("async def ingest",),
        composition_markers=("PinnedTurnCompiler(",),
        drain_site="WorldRuntime.ingest",
    ),
    VerticalRegistration(
        lane_id="clock",
        summary="Durable clock advancement trigger",
        shape="infrastructure",
        hand_rolled=True,
        module="clock_authority.py",
        process_kinds=("clock",),
        runtime_drain_markers=("async def advance",),
        drain_site="WorldRuntime.advance",
    ),
    VerticalRegistration(
        lane_id="settlement",
        summary="Provider receipt settlement trigger",
        shape="infrastructure",
        hand_rolled=True,
        module="settlement.py",
        process_kinds=("settlement",),
        runtime_drain_markers=("SettlementPlanner(",),
        drain_site="WorldRuntime.settle",
    ),
    VerticalRegistration(
        lane_id="recovery",
        summary="Generic durable recovery trigger",
        shape="infrastructure",
        hand_rolled=True,
        module="action_lifecycle.py",
        process_kinds=("recovery",),
        drain_site="WorldRuntime recovery paths",
    ),
)


class VerticalRegistryError(RuntimeError):
    """Raised when the registry and the scattered enumerations disagree."""


def _registry_kinds() -> dict[str, VerticalRegistration]:
    owners: dict[str, VerticalRegistration] = {}
    for row in VERTICAL_REGISTRY:
        for kind in row.process_kinds:
            if kind in owners:
                raise VerticalRegistryError(
                    f"vertical_registry.py: process_kind {kind!r} is claimed by both "
                    f"{owners[kind].lane_id!r} and {row.lane_id!r}"
                )
            owners[kind] = row
    return owners


def _module_source(name: str) -> str:
    path = _WORLD_V2 / name
    if not path.exists():
        raise VerticalRegistryError(f"vertical_registry.py: expected module {name} is missing")
    return path.read_text(encoding="utf-8")


def _string_set_literals(tree: ast.AST, universe: frozenset[str]) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Set):
            values = [
                item.value
                for item in node.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            ]
            if values and all(value in universe for value in values):
                found.append(tuple(sorted(values)))
    return found

def _function_tree(source: str, function_name: str, *, file_label: str) -> ast.AST:
    module = ast.parse(source)
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            return node
    raise VerticalRegistryError(
        f"{file_label}: expected function {function_name!r} was not found; the "
        "vertical registry's extraction anchors must move with it"
    )


def _equality_compared_kinds(tree: ast.AST, universe: frozenset[str]) -> tuple[str, ...]:
    kinds: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "process_kind"
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, str)
            and node.comparators[0].value in universe
        ):
            kinds.add(node.comparators[0].value)
    return tuple(sorted(kinds))


def _expect_equal(
    *, label: str, file: str, actual: tuple[str, ...], declared: tuple[str, ...]
) -> list[str]:
    problems: list[str] = []
    missing = sorted(set(actual) - set(declared))
    extra = sorted(set(declared) - set(actual))
    if missing:
        problems.append(
            f"{file}: {label} contains {missing} but no registry row declares them "
            "(update vertical_registry.py)"
        )
    if extra:
        problems.append(
            f"vertical_registry.py: rows declare {extra} for {label} but {file} "
            "does not list them (update the registry row or the source)"
        )
    return problems


def assert_bounded_vertical_coverage() -> None:
    """Executable gate: registry rows and scattered enumerations agree exactly.

    Runs in tests and at composition-root startup.  Failure messages name the
    file that must change, so a missing reviewer/owner refuses to start
    instead of stranding Opened-only triggers in the production ledger.
    """

    problems: list[str] = []
    literal_kinds = tuple(get_args(TriggerProcess.model_fields["process_kind"].annotation))
    universe = frozenset(literal_kinds)
    owners = _registry_kinds()

    # 1. Every process_kind in the schemas Literal has exactly one owner row.
    unclaimed = sorted(set(literal_kinds) - set(owners))
    if unclaimed:
        problems.append(
            "schemas.py: TriggerProcess.process_kind contains "
            f"{unclaimed} with no vertical_registry.py row (hard gate: every "
            "process_kind must be registered, hand_rolled or framework-backed)"
        )
    phantom = sorted(set(owners) - set(literal_kinds))
    if phantom:
        problems.append(
            f"vertical_registry.py: rows claim process kinds {phantom} that are "
            "not in the schemas.py TriggerProcess Literal"
        )

    # 2. reducers.py claim whitelists.
    reducers_source = _module_source("reducers.py")
    claimed_tree = _function_tree(
        reducers_source, "_trigger_process_claimed", file_label="reducers.py"
    )
    claimed_sets = _string_set_literals(claimed_tree, universe)
    if len(claimed_sets) != 2:
        problems.append(
            "reducers.py: _trigger_process_claimed no longer contains exactly two "
            "process-kind set literals; realign vertical_registry.py extraction"
        )
    else:
        declared_lease = tuple(
            sorted(
                kind
                for row in VERTICAL_REGISTRY
                for kind in row.lease_starts_at_logical_time
            )
        )
        declared_open = tuple(
            sorted(
                kind
                for row in VERTICAL_REGISTRY
                for kind in row.open_before_claim_enforced
            )
        )
        problems.extend(
            _expect_equal(
                label="the lease-starts-at-logical-time whitelist",
                file="reducers.py (_trigger_process_claimed)",
                actual=claimed_sets[0],
                declared=declared_lease,
            )
        )
        problems.extend(
            _expect_equal(
                label="the open-before-claim whitelist",
                file="reducers.py (_trigger_process_claimed)",
                actual=claimed_sets[1],
                declared=declared_open,
            )
        )

    # 3. reducers.py opened-identity validations.
    opened_tree = _function_tree(
        reducers_source, "_trigger_process_opened", file_label="reducers.py"
    )
    opened_kinds = _equality_compared_kinds(opened_tree, universe)
    declared_opened = tuple(
        sorted(
            kind
            for row in VERTICAL_REGISTRY
            for kind in row.opened_identity_reducer_validated
        )
    )
    problems.extend(
        _expect_equal(
            label="the opened-trigger identity validations",
            file="reducers.py (_trigger_process_opened)",
            actual=opened_kinds,
            declared=declared_opened,
        )
    )

    # 4. schemas.py source-evidence whitelist.
    schemas_source = _module_source("schemas.py")
    validator_tree = _function_tree(
        schemas_source, "active_attempt_matches_lease", file_label="schemas.py"
    )
    validator_sets = _string_set_literals(validator_tree, universe)
    if len(validator_sets) != 1:
        problems.append(
            "schemas.py: active_attempt_matches_lease no longer contains exactly "
            "one process-kind set literal; realign vertical_registry.py extraction"
        )
    else:
        declared_evidence = tuple(
            sorted(
                kind
                for row in VERTICAL_REGISTRY
                for kind in row.may_carry_source_evidence
            )
        )
        problems.extend(
            _expect_equal(
                label="the source-evidence whitelist",
                file="schemas.py (active_attempt_matches_lease)",
                actual=validator_sets[0],
                declared=declared_evidence,
            )
        )

    # 5. event_identity.py claim identity whitelist.
    identity_source = _module_source("event_identity.py")
    identity_tree = _function_tree(
        identity_source, "_life_identity_components", file_label="event_identity.py"
    )
    identity_sets = [
        candidate
        for candidate in _string_set_literals(identity_tree, universe)
        if len(candidate) > 3
    ]
    if len(identity_sets) != 1:
        problems.append(
            "event_identity.py: _life_identity_components no longer contains exactly "
            "one claim process-kind set literal; realign vertical_registry.py extraction"
        )
    else:
        declared_claim = tuple(
            sorted(
                kind
                for row in VERTICAL_REGISTRY
                for kind in row.claim_identity_from_domain_key
            )
        )
        problems.extend(
            _expect_equal(
                label="the claim domain-identity whitelist",
                file="event_identity.py (_life_identity_components)",
                actual=identity_sets[0],
                declared=declared_claim,
            )
        )

    # 6. Grammar lane coverage (authority stays with the grammar catalogue).
    from .production_proposal_grammar import PRODUCTION_PROPOSAL_GRAMMARS

    grammar_lanes = set(PRODUCTION_PROPOSAL_GRAMMARS)
    declared_lanes = {
        lane for row in VERTICAL_REGISTRY for lane in row.grammar_lanes
    }
    unknown_lanes = sorted(declared_lanes - grammar_lanes)
    if unknown_lanes:
        problems.append(
            f"vertical_registry.py: rows reference grammar lanes {unknown_lanes} "
            "that production_proposal_grammar.py does not define"
        )
    orphan_lanes = sorted(grammar_lanes - declared_lanes)
    if orphan_lanes:
        problems.append(
            f"production_proposal_grammar.py: lanes {orphan_lanes} have no "
            "vertical_registry.py row claiming them"
        )

    # 7. Drain chain and composition assembly markers.
    runtime_source = _module_source("runtime.py")
    composition_source = _module_source("production_turn_application.py")
    for row in VERTICAL_REGISTRY:
        for marker in row.runtime_drain_markers:
            if marker not in runtime_source:
                problems.append(
                    f"runtime.py: drain marker {marker!r} for vertical "
                    f"{row.lane_id!r} is missing (the drain chain lost this well)"
                )
        for marker in row.composition_markers:
            if marker not in composition_source:
                problems.append(
                    f"production_turn_application.py: composition marker {marker!r} "
                    f"for vertical {row.lane_id!r} is missing (the composition root "
                    "no longer assembles this well)"
                )

    # 8. Hard gate: a non-hand-rolled row must resolve to a real VerticalSpec
    #    surface inside a module that imports the framework.
    from .bounded_decision_vertical import VerticalSpec

    for row in VERTICAL_REGISTRY:
        if row.hand_rolled:
            continue
        if not row.spec_module or not row.spec_builder:
            problems.append(
                f"vertical_registry.py: framework row {row.lane_id!r} must name "
                "spec_module and spec_builder"
            )
            continue
        module = import_module(row.spec_module)
        surface = getattr(module, row.spec_builder, None)
        if surface is None:
            problems.append(
                f"{row.module}: framework vertical {row.lane_id!r} does not expose "
                f"{row.spec_builder!r}"
            )
            continue
        if not (isinstance(surface, VerticalSpec) or callable(surface)):
            problems.append(
                f"{row.module}: {row.spec_builder!r} is neither a VerticalSpec nor "
                "a spec-producing callable"
            )
        module_source = _module_source(row.module)
        if "bounded_decision_vertical" not in module_source:
            problems.append(
                f"{row.module}: framework vertical {row.lane_id!r} does not import "
                "bounded_decision_vertical (hard gate: non-hand-rolled wells must "
                "run on the framework)"
            )

    if problems:
        raise VerticalRegistryError(
            "bounded decision vertical registry drift:\n- " + "\n- ".join(problems)
        )


__all__ = [
    "VERTICAL_REGISTRY",
    "VerticalRegistration",
    "VerticalRegistryError",
    "assert_bounded_vertical_coverage",
]
