"""Offline executable evidence for the frozen World v2 scenario corpus.

This runner is deliberately narrower than the human-likeness evaluator.  It
uses a fixed fake chat model and a deterministic fake provider, invokes only
``WorldV2TurnApplication`` for ingress/delivery, then exports replay evidence.
It can prove that the 120 frozen fixtures exercise the v2 authority chain; it
cannot prove a real model or a person finds the output human-like.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Literal

from companion_daemon.llm import FakeCompanionModel

from .activity_plan_runtime import ActivityPlanCommand
from .character_interior.production import compose_fixture_character_interior
from .deliberation import ModelRoute, RouteRequest
from .platform_action_executor import PlatformDispatchReceipt, PlatformDispatchRequest
from .production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from .recall_runtime import install_trace_authority_key
from .occurrence_content_coordinator import (
    OccurrenceContentCommitRequest,
    OutcomeCandidateContent,
)
from .replay_evaluator import ReplayEvaluator
from .room_projection import RoomProjectionMaterializer
from .scenario_corpus import (
    SCENARIO_CORPUS_VERSION,
    TEST_ECONOMY_PROFILE_VERSION,
    ScenarioCase,
    ScenarioFault,
    verify_frozen_scenario_corpus,
)
from .simulator_adapters import SimulatorIdentityResolver
from .schemas import (
    DueWindow,
    EvidenceRef,
    LedgerProjection,
    OutcomeObservation,
    WorldOccurrenceProjection,
)


class ScenarioVerificationError(AssertionError):
    """A frozen scenario did not exercise its declared authority predicates."""


# This identifies the executable mechanism baseline independently from the
# frozen input corpus. ``.4`` supersedes a stale ``.3`` manifest after a
# clean-worktree rerun exposed that the hash recorded for ``.3`` did not match
# the executable 120-case suite.  The clean manifest was compared before and
# after the event-ecology composition change: every exported run field is
# identical, so this is a correction of the invalid fixture identity, not a
# claim that event ecology changed frozen observable behaviour.  ``.3`` had
# superseded a stale ``.2`` manifest after the
# enforcement-grade read-only-tool authorization binding was added to
# ``Action``.  Even ordinary Actions now carry the explicit, non-authorizing
# ``read_only_tool_authorization: null`` member in their canonical ledger
# payload.  That deliberately changes acceptance/event/replay identities,
# while the frozen suite proves that its observable scenario behaviour did not
# change.  It is not human-likeness evidence.
# ``.7`` establishes the post-refactor executable baseline after the shared
# expression draft, same-turn affect/fact composition, compact model-facing
# capsule, and proactive/life runtime were connected to the public application
# seam.  Every scenario predicate must still pass; the hash records the changed
# audit/action identities and background work trace, not a human-likeness score.
# ``.8`` records the intentional single-call cognition, affect-expression,
# epistemic claim, and durable two-Fact continuity contracts.  These alter
# proposal/context identities while all fixed scenario predicates remain the
# same executable gate.
# ``.9`` records the ``activity-opening.4`` catalog: ordinary activity
# completion now tracks the accepted schedule window instead of a one-minute
# elapsed floor, so a sixty-minute plan no longer "completes" after one wake.
# Catalog version and opening-token identities change accordingly; committed
# ``activity-opening.3`` and ``.2`` proposals still replay against their
# exact frozen rules and every fixed scenario predicate remains the gate.
# The recorded manifest is the complete fixed fake-suite hash produced by the
# executable corpus; keeping the version while correcting the stale digest is
# intentional and does not claim human-likeness evidence.
# ``.10`` re-baselines after the inner-life coverage verticals landed: the
# Change Phase advisory (and life-author-weight.5's phase prior), the per-NPC
# relationship reading (npc-initiative-weight.2), the pending shared_private
# invitation advisory, the private-impression background producer, and the
# aspiration crystallization seam.  Capsule advisory content and versioned
# weight identities legitimately shift the fixed-suite manifest; every fixed
# scenario predicate still passes unchanged.
# ``.12`` re-baselines the interactive-turn first-valid coordinator.  Fixed
# scenario proposals and predicates are unchanged, but their ModelResult
# audits now carry the explicit primary slot/winner outcome under
# ``model-result-audit.3`` and therefore have new deterministic identities.
# ``.13`` records deterministic semantic continuity selection.  Verified
# current/pending dialogue receives a bounded working-memory floor, related
# older dialogue and source-bound Fact/Memory/Thread text can be reactivated,
# and every retained item remains pinned to its original ledger authority.
# This intentionally changes Context Capsule and downstream proposal/audit
# identities; the fixed scenario predicates remain the executable gate.
# ``.14`` binds pending-interaction acknowledgement to the exact source
# Observation rather than reply delivery time.  Accepted reply authority now
# contributes that causal claim to recent dialogue, so multi-turn Context and
# downstream audit identities legitimately shift while predicates stay fixed.
# ``.15`` records the first source-bound associative-recall policy identity in
# the Context rank digest.  The narrow lexical prefetch can change which
# already-authoritative Fact/Memory/Thread evidence receives the bounded
# working-context floor; it does not change scenario predicates or grant new
# fact/action authority.
# ``.16`` records the completed character-owned recall contract.  Context now
# prepares bounded local attention candidates in parallel, model-visible
# prefetch/pull results carry exact query/source/cursor provenance under
# ``model-result-audit.4``, and paired cognition uses a one-hop transition
# proof.  This intentionally changes audit identities while every fixed
# scenario predicate remains the executable gate; it is not evidence that
# long-term conversational naturalness has already been established.
# ``.18`` adds source-complete private reflection lineage and explicit
# semantic-embedding degradation/cost state without changing scenario
# behavior.
# ``.17`` completes the dual-channel contract: a local prefetch that wins the
# non-blocking race is actually visible in the first cognition pass, uses the
# same Capsule item shape as other source-bound material, and is audited.
# Role-authored private reflection and the credential-gated semantic default
# also change model/capsule identities without changing scenario predicates.
# ``.19`` makes automatic recall actually reach the first cognition pass.  The
# 30-turn recall eval exposed four independent losses: the zero-wait prefetch
# race, successful injections evicting ranked capsule items, in-window
# dialogue echoes crowding out facts, and the global capsule cap repeatedly
# collapsing the fact lane to its deep-eviction floor. The first pass now
# joins once within a small fixed bound, injection supplements current
# dialogue, the corpus excludes the exact provider-visible working window,
# and the verified internal capsule has room for several independently
# sourced facts. Capsule and audit identities legitimately shift; every fixed
# scenario predicate remains the executable gate.
# ``.20`` tightens the model-facing source contract so first-person biography,
# relatives, past experiences, and enduring preferences are explicitly
# included in the existing source-closure boundary. It also makes a missing
# response-expectation assessment non-fatal to an otherwise valid reply; the
# unresolved expectation retains its existing expiry instead of being
# semantically settled by local code.
# ``.21`` keeps the independent expression episode available as an explicit
# experiment but disables its shadow call by default after real-provider
# testing showed queue inflation without a user-visible effect. The expression
# truth contract also states that unlisted people and past occurrences cannot
# be invented merely as conversational analogies.
# ``.22`` adds source-closed correction/withdrawal history and the durable
# hybrid recall seam.  The frozen scenarios do not change their outcomes, but
# their pinned capsule and reducer identities now include those new authority
# fields.
# ``.25`` installs reducer bundle .41 for the source-bound
# ExperienceMemoryDecisionRecorded audit. The event is deliberation-only and
# changes no fixed scenario predicate, but the installed reducer identity is
# intentionally part of every scenario manifest.
# ``.28`` records contextual short-utterance recall attention.  The automatic
# query now includes the recent verified dialogue and withholds an isolated
# sub-three-character lexical cue, preventing e.g. “来了” from outranking the
# current topic merely because an old message contained “回来了”.  A clean
# before/after run changed only six replay hashes; every output hash, model
# call count, terminal state, trigger kind, room view, and scenario predicate
# remained byte-identical.  This is mechanism/replay evidence, not a
# human-likeness claim.
# ``.29`` installs event-sourced Biographical Context and Life Arcs, including
# contextual NPC lifecycle and reducer bundle .41. The fixed conversation
# outputs remain fake-model evidence; changed replay/manifests are expected
# because the authority grammar and bootstrap projection gained real fields.
# ``.33`` installs reducer bundle .44. It repairs the replay-derived
# contextual-life work index during .43 migration; the index is deliberately
# absent from the semantic payload, so all scenario predicates and visible
# output hashes remain governed by the existing assertions while the installed
# reducer identity changes every replay hash.
# ``.35`` records the split between complete Proposal audit identity and
# effect identity.  PrivateTurnState remains in immutable proposal bytes, while
# the same visible Expression mints the same TypedChange/Action identities
# regardless of that audit-only state.  Visible replies and scenario
# predicates remain unchanged.
# ``.36`` adds the per-Observation Expression reliability lifecycle.
# Open/claim is atomic with ingress and terminal completion is replay-visible;
# this changes mechanism hashes without changing the character-authored reply.
# ``.38`` installs reducer bundle .45.  It adds exact newer-Observation
# authority for superseding a stale Expression retry and model-owned
# consolidation/supersession of private impressions.  The fixed suite retains
# the same visible fake-model outputs and all explicit scenario predicates,
# while the installed reducer/authority identity changes replay manifests.
# ``.39`` records clock-correct claim identity for appraisal-owned background
# triggers and the author-excluding source-review boundary for model-authored
# life facts.  A delayed worker now claims at the current World time while the
# opened trigger retains the source appraisal's logical time; fact-bearing
# life development also fails closed when no independent reviewer is wired.
# These change event/replay identities while all 120 fixed scenario predicates
# and visible fake-model outputs still pass unchanged.
# ``.40`` records the model-owned Private Turn State -> selective Recall ->
# role-free ordered Expression contract and the independently qualified
# Inventory/V7 source-review topology.  A complete pre-bump audit kept all 120
# executable predicates and every cold replay green; the changed Context,
# Proposal and provider-audit identities are therefore intentional mechanism
# drift, not evidence of human-likeness or permission to relax a truth gate.
# ``.41`` stops treating JSON object member order as causal evidence for the
# required Private Turn State.  A side-by-side run against ``.40`` kept every
# visible output, event type, Action state, model-call count and explicit
# predicate identical across all 120 cases; only replay hashes changed because
# the model request/audit identity now records the order-independent contract.
# ``.42`` upgrades the replay reducer to ``.47`` for compact pending Fact-batch
# decision recovery.  The complete fixed suite keeps all 120 explicit
# predicates and visible fake-model outputs unchanged; its replay/audit hashes
# intentionally change because the authenticated projection schema changed.
# ``.43`` records the model-owned conversational posture and the bounded
# endpoint-attention advisory in the verified trigger/prompt contract. The
# endpoint model remains advisory-only; the baseline moves because prompt and
# proposal audit identities intentionally include the new decision coordinate.
# ``.44`` records reducer .48 and the retirement of World-Author-owned life
# directions. The fixed suite has no such transition, so this is an audit and
# projection-version baseline change rather than a changed visible scenario.
# ``.45`` separates subjective Character direction from source-reviewed
# objective biographical consequence and advances open-life proposal authority
# to `.7`; the fixed suite still has no visible life-transition branch.
# ``.46`` removes factual source-declaration bookkeeping from proactive role
# authorship and moves it to a separately audited post-authorship binder. The
# frozen scenarios retain their explicit visible behavior predicates; prompt,
# provider-subcall and proposal audit identities intentionally change.
# ``.47`` records reducer .49 and its authenticated .48 head migration after
# external-perception projections became part of the conditional World state.
# The frozen corpus does not ingest external signals, so its explicit visible
# behavior predicates remain unchanged; projection and replay identities move
# because the reducer bundle is intentionally versioned rather than silently
# changing the .48 state-hash protocol.
# ``.48`` binds public-information authority to the exact domestic source
# registry under reducer .50. A differential run across all 17 scenario
# families kept visible output, event types, Action states, model-call counts,
# room views and explicit predicates unchanged; only authenticated replay
# hashes move with the reducer identity.
# ``.49`` removes the synchronous post-authorship proactive claim binder.
# Factual permission metadata now travels in the role's original structured
# decision and still crosses local closure plus independent truth review. The
# fixed suite retains its explicit behavior predicates; prompt and provider
# audit identities intentionally change.
# ``.50`` gives proactive authors the same compact, source-alias hard-boundary
# manifest already used by inbound expression and expands only those aliases
# before local closure. The manifest selects no behavior; frozen visible
# predicates remain unchanged while prompt/audit hashes intentionally move.
# ``.51`` adds source-bound NPC identity/state projections and composes the
# model-owned NPC ecology behind Life Ecology. The frozen corpus contains no
# NPC ecology wake, so its visible predicates and provider-call counts remain
# unchanged; authenticated projection identities move with reducer .51.
# ``.52`` cuts the seeded world-outcome fixture over to one CharacterInterior
# world-stimulus author.  Appraisal and optional immediate Affect now share one
# source-bound role result, so the old independent Affect trigger and third
# background model call are deliberately absent.
# ``.53`` keeps those observable predicates unchanged while making the
# structured CharacterInterior wire contract explicit and accepting only
# provider-shaped legacy envelopes whose refs/payload were authored by the
# model.  The fixed fixture consequently gets new request/audit identities;
# no semantic decision, event family, or action state is silently rewritten.
# ``.54`` records the first complete deterministic run after the subsequent
# unified-interior handoff/recovery changes were present together.  The old
# .53 hash was stale (the per-case verification predicates and replay checks
# still pass, but the aggregate request/room/replay manifest moved), so this
# is an explicit audited rebaseline rather than weakening the drift guard.
FROZEN_OFFLINE_SUITE_BASELINE_VERSION = "world-v2-offline-mechanism-baseline.60"

# Filled only after the complete, fixed fake suite has been run. A change to
# this value requires the corresponding baseline-version rationale; it must
# not be rewritten merely to silence a scenario failure.
# 2026-08-06: rebaselined to .52 after the suite's two cross-process
# nondeterminism sources were removed: (1) the expression-episode claim owner
# is now pinned by WorldV2TurnApplicationConfig.expression_episode_owner, and
# (2) the recall-trace HMAC authority key is now pinned by
# install_trace_authority_key() instead of a fresh secrets.token_bytes per
# process. The frozen manifest hash is byte-identical across independent
# processes; this hash was verified twice cross-process.
# 2026-08-07: rebaselined while the frozen fixture remained on the historical
# declared-claims-only source-review lane and the author contract was hardened:
# the reviewer/repair lanes no longer alter
# candidates, the author prompt gained the closed-scope "declare before you
# assert" hard clause and beats-last serialization, and Inner Life Snapshot
# material limits were trimmed. All drift is expected prompt/material input
# change; the deterministic boundary itself is unchanged.
# 2026-08-07 2nd: appraisal output length discipline (brief_rationale /
# behavior_tendency / display_strategy <= 120 chars, meanings <= 2 at <= 64
# chars) shifts the frozen fake-suite prompt; deterministic boundary unchanged.
# 2026-08-07 3rd: fabricated-ref stripping (expression + proposal layers) and
# trigger observation-event binding change accepted-proposal evidence paths;
# the frozen fake-suite manifest shifts accordingly. Deterministic boundary
# unchanged (strip only drops claims that would have failed closed).
# 2026-08-08: compact reply shape (response_text without beats) accepted in
# the stream partition; explicit authored field requirements relaxed to
# defaults (timing/confidence/cadence); compact-shape prompt guidance rolled
# back (model self-selection was unreliable); repair temperature lowered to
# 0.2 and yield/now anti-pattern added to the author contract. Fake-suite
# prompt/partition input shifts; deterministic boundary unchanged.
# 2026-08-08 2nd: the CharacterInterior structured-role contract now states
# the nested generic decision and typed-proposal shapes explicitly.  The
# boundary accepts only a structural legacy-envelope normalization when the
# model already supplied the source refs and complete payload; it never
# fabricates summary, refs, facts, motive, timing, or silence.  The resulting
# fixture request/audit identities move while exported event families,
# visible outputs, action states, and model-call counts remain unchanged.
# 2026-08-09: complete isolated rerun after the unified-interior handoff and
# recovery changes produced the stable manifest below.  Every frozen case
# passed its explicit event/action/replay predicates; only the aggregate
# manifest changed from the stale .53 value.
# 2026-08-09 2nd: world-stimulus appraisal now requires the versioned
# character_role_world_stimulus_appraisal_v1 function.  The fixed scenario
# model advertises and receives that transport; visible predicates remain
# unchanged, but request/audit rows (and therefore the aggregate manifest)
# move.  This is an explicit protocol rebaseline, not a relaxed assertion.
# 2026-08-09 3rd: the expression contract now states that first-person life
# facts require matching pinned world_claims.  The fixed suite's visible
# predicates remain unchanged, while its prompt/audit material moves.  Keep
# the drift guard explicit rather than weakening it for this intentional
# source-boundary change.
# 2026-08-09 4th: the appraisal contract now explicitly keeps trust in the
# relationship signal rather than the affect dimensions.  The fixed suite's
# visible predicates remain unchanged while its prompt/audit material moves;
# the DeepSeek strict-schema projection is transport-only and uses the same
# canonical semantics. Rebaseline the aggregate hash instead of weakening
# the drift guard.
# 2026-08-09 5th: durable CharacterInterior author identity is carried through
# the Deliberation audit boundary.  Fixed scenario predicates remain unchanged,
# but the complete executable manifest is rebaselined to the resulting hash.
# 2026-08-09 6th: inbound, expression-reconsideration, and memory opportunities
# now use the canonical source→opportunity identity.  These are lineage-only
# changes: the frozen event/action/replay predicates remain unchanged, while
# durable audit rows (and therefore the aggregate manifest) move again.
# 2026-08-10: the NPC world-impact fixture now replays the focused actor capsule
# and neutral shared-history projection.  The complete before/after row audit
# changed only that fixture's replay hash; its output, events, actions, trigger
# kinds, model-call counts, restart evidence, and verification predicates are
# byte-identical.  Keep the aggregate drift guard and bind the audited replay.
FROZEN_OFFLINE_SUITE_MANIFEST_HASH = (
    "902abb32309f90a24f88232a848eec82847c2a17935c47e2d9dc223f8e7223f2"
)


class _FixedScenarioRouter:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(
            tier="flash",
            reason_code="phase8_fixed_fake_route",
            router_version="world-v2-scenario-runner.1",
        )


class _FixedScenarioTransport:
    """A fixed receipt provider with a deliberately small fault surface."""

    provider = "scenario:fixed-provider"

    def __init__(self, *, received_at: datetime, fault: ScenarioFault) -> None:
        self._received_at = received_at
        self._fault = fault
        self._receipts: dict[str, PlatformDispatchReceipt] = {}
        self.bodies: list[str] = []

    async def send(self, request: PlatformDispatchRequest) -> PlatformDispatchReceipt:
        existing = self._receipts.get(request.idempotency_key)
        if existing is not None:
            return existing
        status: Literal["delivered", "failed", "unknown"] = (
            "failed"
            if self._fault == "provider_failed"
            else "unknown"
            if self._fault == "provider_unknown"
            else "delivered"
        )
        identity = hashlib.sha256(request.fingerprint.encode("utf-8")).hexdigest()
        receipt = PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:scenario:{identity}",
            provider_ref=f"message:scenario:{identity}",
            status=status,
            error_class=(
                "simulated_provider_timeout"
                if status == "failed"
                else "simulated_provider_unknown"
                if status == "unknown"
                else None
            ),
            received_at=self._received_at,
            raw_payload_hash="sha256:" + hashlib.sha256(request.body.encode("utf-8")).hexdigest(),
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )
        self._receipts[request.idempotency_key] = receipt
        self.bodies.append(request.body)
        return receipt

    async def lookup(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> PlatformDispatchReceipt | None:
        receipt = self._receipts.get(idempotency_key)
        if receipt is not None and receipt.request_fingerprint != request_fingerprint:
            raise ScenarioVerificationError("provider lookup fingerprint mismatch")
        return receipt


class _FixedCharacterInteriorScenarioModel(FakeCompanionModel):
    """One fixture author for both inbound turns and settled-world stimuli.

    The purpose switch mirrors the production CharacterInterior request
    boundary.  It does not construct a second Appraisal or Affect adapter: one
    ``world_stimulus_appraisal`` response owns the private appraisal and its
    optional immediate Affect target together.
    """

    model = "fixture-character-author"
    supports_required_tool_choice = True

    def __init__(self) -> None:
        super().__init__()
        self.outcome_selection_calls = 0
        self.world_stimulus_calls = 0
        self.world_stimulus_requests: list[list[dict[str, str]]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        try:
            request = json.loads(messages[-1]["content"])
            inner_turn = request["inner_turn"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return await super().complete(messages, temperature=temperature)
        if not isinstance(inner_turn, dict):
            return await super().complete(messages, temperature=temperature)
        if inner_turn.get("purpose") == "outcome_selection":
            self.outcome_selection_calls += 1
            capability = request["capability_manifest"]
            source_ref = capability["source_refs"][0]
            selected = capability["payload"]["offered_tokens"][0]
            return json.dumps(
                {
                    "status": "decision",
                    "summary": "这个已经发生的结果现在成了我生活的一部分。",
                    "attended_source_refs": [source_ref],
                    "decision": {
                        "source_refs": [source_ref],
                        "payload": {
                            "selected_token": selected,
                            "character_life_direction": None,
                        },
                    },
                    "recall_query": None,
                    "proposals": [],
                },
                ensure_ascii=False,
            )
        if inner_turn.get("purpose") != "world_stimulus_appraisal":
            return await super().complete(messages, temperature=temperature)

        self.world_stimulus_calls += 1
        self.world_stimulus_requests.append(messages)
        capability = request["capability_manifest"]
        source_ref = capability["source_refs"][0]
        bounds = capability["payload"]["affect_target_lower_bounds"]["bounds"]
        warmth = next(
            item
            for item in bounds
            if isinstance(item, dict) and item.get("dimension") == "warmth"
        )
        minimum = warmth["minimum_target_intensity_bp"]
        if isinstance(minimum, bool) or not isinstance(minimum, int):
            raise ScenarioVerificationError("fixture Affect bound is not an integer")
        target = max(minimum, 3_300)
        return json.dumps(
            {
                "status": "transition",
                "summary": "这件已经发生的事让我心里有了一点踏实和暖意。",
                "attended_source_refs": [source_ref],
                "decision": None,
                "recall_query": None,
                "proposals": [
                    {
                        "proposal_type": "world_stimulus_appraisal_result",
                        "decision": "activate",
                        "brief_rationale": (
                            "The settled private occurrence feels like bounded goal progress."
                        ),
                        "behavior_tendency": "quietly_take_it_in",
                        "stance": "privately_warm",
                        "display_strategy": "withhold",
                        "confidence": 7_100,
                        "meaning_candidates": [
                            {"meaning": "goal_progress", "confidence": 7_000},
                            {"meaning": "care", "confidence": 3_000},
                        ],
                        "attribution": "situation",
                        "severity": 4_300,
                        "expiry": None,
                        "affect_transition": {
                            "operation": "open",
                            "component_targets": [
                                {
                                    "dimension": "warmth",
                                    "target_intensity_bp": target,
                                }
                            ],
                        },
                        "relationship_signal": None,
                    }
                ],
            },
            ensure_ascii=False,
        )

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: object | None = None,
        tool_choice: object | None = None,
    ) -> str:
        if tools is not None:
            request = json.loads(messages[-1]["content"])
            purpose = request.get("inner_turn", {}).get("purpose")
            expected_tool = {
                "world_stimulus_appraisal": (
                    "character_role_world_stimulus_appraisal_v1"
                ),
                "outcome_selection": "character_role_outcome_selection_v1",
                "expression_reconsideration": (
                    "character_role_expression_reconsideration_v1"
                ),
            }.get(purpose)
            if tool_choice != {
                "type": "function",
                "function": {"name": expected_tool},
            }:
                raise ScenarioVerificationError(
                    "scenario fixture received an unexpected required-tool choice"
                )
        return await self.complete(messages, temperature=temperature)


class _FixedDelayedExpressionModel:
    """One model-owned delayed draft, then an ordinary reply after interruption.

    Both inbound turns and each reconsideration cross the same CharacterInterior
    author.  The fixture returns only the current wire contract; it never builds
    DecisionProposal, Action, ExpressionPlan or receipt authority itself.
    """

    supports_required_tool_choice = True

    def __init__(self, *, scenario_turn_id: str) -> None:
        self._scenario_turn_id = scenario_turn_id
        self.calls: list[list[dict[str, str]]] = []
        self._inbound_calls = 0

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del temperature
        self.calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        inner_turn = payload.get("inner_turn")
        if isinstance(inner_turn, dict):
            if inner_turn.get("purpose") != "expression_reconsideration":
                raise ScenarioVerificationError(
                    "delayed expression fixture received an unexpected Interior purpose"
                )
            source_refs = payload["capability_manifest"]["source_refs"]
            return json.dumps(
                {
                    "status": "decision",
                    "summary": "新消息来了，但刚才想说的两句仍然值得接着说完。",
                    "attended_source_refs": source_refs,
                    "decision": {
                        "source_refs": source_refs,
                        "payload": {"disposition": "continue"},
                    },
                    "recall_query": None,
                    "proposals": [],
                },
                ensure_ascii=False,
            )

        self._inbound_calls += 1
        delayed = self._inbound_calls == 1
        expression = {
            "private_turn_state": {
                "inner_state_summary": (
                    "这句话让我想认真接住，但我更想等手头这一点结束后再完整说。"
                    if delayed
                    else "对方回来了，我还记得前一轮，也想先回应这条新消息。"
                ),
                "attended_source_refs": [],
            },
            "timing_choice": "later" if delayed else "now",
            "beats": (
                [
                    {"modality": "text", "text": "我想了想这句话。"},
                    {
                        "modality": "text",
                        "text": "等我把手头这点收完，再认真回到刚刚的话。",
                    },
                ]
                if delayed
                else [
                    {
                        "modality": "text",
                        "text": "我记得。刚才没立刻接，是想等自己能认真说的时候。",
                    }
                ]
            ),
            "cadence": "conversational",
            "stance": "paced" if delayed else "acknowledge_briefly",
            "brief_rationale": (
                "I want to answer both parts after a short real delay."
                if delayed
                else "The new message deserves an immediate acknowledgement."
            ),
            "confidence": 7_800 if delayed else 6_100,
            "world_claims": [],
        }
        if delayed:
            expression.update({"delay_seconds": 120, "expires_after_seconds": 600})
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "The fixture leaves this as an ordinary interaction.",
                    "behavior_tendency": "stay_present",
                    "stance": "paced",
                    "display_strategy": "natural",
                    "confidence": 3_000,
                },
                "expression_draft": expression,
            },
            ensure_ascii=False,
        )

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
        tools: object | None = None,
        tool_choice: object | None = None,
    ) -> str:
        if tools is not None:
            payload = json.loads(messages[-1]["content"])
            purpose = payload.get("inner_turn", {}).get("purpose")
            expected_tool = "character_role_expression_reconsideration_v1"
            if purpose != "expression_reconsideration" or tool_choice != {
                "type": "function",
                "function": {"name": expected_tool},
            }:
                raise ScenarioVerificationError(
                    "delayed expression fixture received an unexpected required-tool choice"
                )
        return await self.complete(messages, temperature=temperature)


@dataclass(frozen=True, slots=True)
class ScenarioRunResult:
    scenario_turn_id: str
    scenario_family: str
    emotional_gold: bool
    fault: ScenarioFault
    world_id: str
    output_hash: str | None
    event_types: tuple[str, ...]
    terminal_action_states: tuple[str, ...]
    replay_hash: str
    replay_passed: bool
    model_calls: int
    observation_count: int
    trigger_kinds: tuple[str, ...]
    room_view_hash: str
    # These fields distinguish the ordinary chat-only controls from the one
    # durable outcome/NPC/Affect continuation.  They are frozen in the suite
    # manifest so a later runner cannot silently stop running the background
    # consumers while preserving its user-facing output hash.
    restarted_after_seed: bool
    background_work_statuses: tuple[str, ...]
    background_model_calls: int
    verification_errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.verification_errors

    def manifest_row(self) -> dict[str, object]:
        return {
            "scenario_turn_id": self.scenario_turn_id,
            "scenario_family": self.scenario_family,
            "emotional_gold": self.emotional_gold,
            "fault": self.fault,
            "world_id": self.world_id,
            "output_hash": self.output_hash,
            "event_types": self.event_types,
            "terminal_action_states": self.terminal_action_states,
            "replay_hash": self.replay_hash,
            "replay_passed": self.replay_passed,
            "model_calls": self.model_calls,
            "observation_count": self.observation_count,
            "trigger_kinds": self.trigger_kinds,
            "room_view_hash": self.room_view_hash,
            "restarted_after_seed": self.restarted_after_seed,
            "background_work_statuses": self.background_work_statuses,
            "background_model_calls": self.background_model_calls,
            "verification_errors": self.verification_errors,
        }


@dataclass(frozen=True, slots=True)
class ScenarioSuiteResult:
    corpus_version: str
    economy_profile_version: str
    mechanism_baseline_version: str
    runs: tuple[ScenarioRunResult, ...]

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.runs)

    @property
    def manifest_hash(self) -> str:
        payload = {
            "corpus_version": self.corpus_version,
            "economy_profile_version": self.economy_profile_version,
            "mechanism_baseline_version": self.mechanism_baseline_version,
            "runs": [item.manifest_row() for item in self.runs],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def export_manifest(self) -> dict[str, object]:
        return {
            "kind": "world-v2-offline-scenario-run.1",
            "corpus_version": self.corpus_version,
            "economy_profile_version": self.economy_profile_version,
            "mechanism_baseline_version": self.mechanism_baseline_version,
            "runner_limitations": (
                "fixed fake model/provider only; this is not a human or model blind evaluation"
            ),
            "passed": self.passed,
            "manifest_hash": self.manifest_hash,
            "runs": [item.manifest_row() for item in self.runs],
        }


class ScenarioRunner:
    """Run a frozen scenario exclusively through the public v2 app seam."""

    def __init__(self, *, workdir: str | Path) -> None:
        self._workdir = Path(workdir)
        self._workdir.mkdir(parents=True, exist_ok=True)

    async def run_case(self, case: ScenarioCase) -> ScenarioRunResult:
        now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
        world_id = f"world:phase8-scenario:{case.entry.scenario_turn_id}"
        database_path = self._workdir / f"{case.entry.scenario_turn_id}.sqlite"
        if database_path.exists():
            database_path.unlink()
        model = (
            _FixedDelayedExpressionModel(scenario_turn_id=case.entry.scenario_turn_id)
            if case.execution == "seeded_expression_delay"
            else (
                _FixedCharacterInteriorScenarioModel()
                if case.execution == "seeded_world_outcome_affect"
                else FakeCompanionModel()
            )
        )
        transport = _FixedScenarioTransport(received_at=now, fault=case.fault)
        # The frozen suite must be byte-deterministic across separate
        # processes.  Production signs recall traces with a fresh
        # process-unique authority key; the suite pins one fixed key so
        # prefetch-trace authority seals (and every snapshot/request hash
        # derived from them) do not vary between processes.
        install_trace_authority_key(
            bytes.fromhex(
                "7068617365382d7363656e6172696f2d74726163652d6b65792d303030303030"
            )
        )

        def build_application():
            return build_sqlite_world_v2_turn_application(
                path=database_path,
                config=WorldV2TurnApplicationConfig(
                    world_id=world_id,
                    companion_actor_ref="agent:companion",
                    reply_target="user:scenario",
                    action_pump_owner="pump:phase8-scenario",
                    # The frozen suite must be byte-deterministic across runs.
                    # Production intentionally claims with a process-unique
                    # episode owner; the suite pins one so claim payloads and
                    # event identities do not vary between processes.
                    expression_episode_owner="worker:phase8-scenario:expression-episode",
                ),
                identities=SimulatorIdentityResolver(canonical_user_id="scenario"),
                router=_FixedScenarioRouter(),
                character_interior=compose_fixture_character_interior(
                    model=model,
                ),
                transport=transport,
                now=now,
            )

        app = build_application()
        restarted_after_seed = False
        background_work_statuses: tuple[str, ...] = ()
        try:
            if case.execution in {"seeded_world_outcome", "seeded_world_outcome_affect"}:
                await self._seed_world_outcome(app=app, case=case, now=now)
            if case.execution == "seeded_world_outcome_affect":
                # The fixture intentionally crashes only after the source
                # observation/trigger were durable.  The restarted app must
                # perform outcome settlement, then one CharacterInterior turn
                # that owns both its appraisal and optional immediate Affect
                # before the next visible user turn is compiled.
                app.close()
                restarted_after_seed = True
                app = build_application()
                statuses: list[str] = []
                for _ in range(2):
                    work = await app.drain_background_once()
                    if work is None or getattr(work, "work_status", None) is None:
                        raise ScenarioVerificationError("seeded outcome continuation did not run")
                    statuses.append(work.work_status)
                background_work_statuses = tuple(statuses)
            # A seeded occurrence advances logical time before its follow-up
            # chat.  The inbound observation must not claim to arrive before
            # that committed authority; otherwise an interaction-appraisal
            # lease would correctly reject the backwards clock.
            inbound_observed_at = (
                now + timedelta(minutes=2)
                if case.execution in {"seeded_world_outcome", "seeded_world_outcome_affect"}
                else now
            )
            for index, turn in enumerate(case.turns, start=1):
                inbound = dict(
                    platform="simulator",
                    platform_user_id="scenario",
                    platform_message_id=f"{case.entry.scenario_turn_id}:{turn.step_id}",
                    text=turn.text,
                    # The scripted sequence is causal order, not a claim that
                    # logical time advanced.  Advancing time has to travel
                    # through ``app.tick`` with its clock authority.
                    observed_at=inbound_observed_at,
                    trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:{turn.step_id}",
                    coalescing_metadata={
                        "scenario_family": case.entry.scenario_family,
                        "scenario_step": turn.step_id,
                    },
                )
                await app.inbound(**inbound)
                if case.fault == "duplicate_ingress" and index == 1:
                    await app.inbound(**inbound)
                if case.execution == "seeded_activity_plan" and index == 1:
                    await app.plan_activity(
                        ActivityPlanCommand(
                            command_id=f"command:phase8:{case.entry.scenario_turn_id}:activity",
                            world_id=world_id,
                            source_observation_id=(
                                "observation:simulator:scenario:"
                                f"{case.entry.scenario_turn_id}:{turn.step_id}"
                            ),
                            plan_id=f"plan:phase8:{case.entry.scenario_turn_id}:museum",
                            activity_id=f"activity:phase8:{case.entry.scenario_turn_id}:museum",
                            activity_kind="museum_visit",
                            importance_bp=4800,
                            location_ref="place:phase8:museum",
                            participant_refs=("agent:companion",),
                            scheduled_window=DueWindow(
                                opens_at=now + timedelta(days=1),
                                closes_at=now + timedelta(days=1, hours=4),
                            ),
                        ),
                        logical_time=inbound_observed_at,
                        created_at=inbound_observed_at,
                        trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:activity-plan",
                        causation_id=f"observation:simulator:scenario:{case.entry.scenario_turn_id}:{turn.step_id}",
                        correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}:activity-plan",
                    )
                if case.execution == "seeded_expression_delay" and index == 1:
                    delayed = await app.drain_actions_once()
                    if getattr(delayed, "status", None) != "not_due":
                        raise ScenarioVerificationError(
                            "delayed expression fixture dispatched before its authored due window"
                        )
                # An interruption deliberately arrives before the old action is
                # dispatched.  Every other scripted turn reaches the same
                # application-owned ActionPump before the next ingress.
                if case.execution not in {"interruption", "seeded_expression_delay"}:
                    await app.drain_actions_once()
            if case.execution == "seeded_expression_delay":
                reconsidered = 0
                for _ in range(64):
                    candidate = await app.drain_background_once()
                    if getattr(candidate, "status", None) == "continued":
                        reconsidered += 1
                        if reconsidered == 2:
                            break
                if reconsidered != 2:
                    raise ScenarioVerificationError(
                        "both delayed expression beats were not explicitly reconsidered"
                    )
                due = now + timedelta(minutes=3)
                await app.tick(
                    tick_id=f"{case.entry.scenario_turn_id}:delayed-beat-due",
                    logical_time_from=now,
                    logical_time_to=due,
                    observed_at=due,
                    trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:delayed-beat-due",
                    causation_id=f"scheduler:phase8:{case.entry.scenario_turn_id}:delayed-beat-due",
                    correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}:delayed-beat",
                    reason="phase8_seeded_delayed_expression_due",
                )
                for _ in range(3):
                    await app.drain_actions_once()
            if case.fault == "restart_before_dispatch":
                app.close()
                app = build_application()
            if case.execution == "interruption":
                await app.drain_actions_once()
            evidence = app.export_replay_evidence()
        finally:
            app.close()

        event_types = tuple(item.event.event_type for item in evidence.events)
        projection = evidence.projection
        action_states = tuple(item.state for item in projection.actions)
        observation_count = sum(item == "ObservationRecorded" for item in event_types)
        trigger_kinds = tuple(sorted({item.process_kind for item in projection.trigger_processes}))
        room_view_json = RoomProjectionMaterializer.materialize(projection).model_dump_json()
        replay = ReplayEvaluator().evaluate(evidence=evidence)
        background_model_calls = (
            int(getattr(model, "outcome_selection_calls", 0))
            + int(getattr(model, "world_stimulus_calls", 0))
        )
        next_context_has_outcome_affect = self._next_context_has_outcome_affect(
            model=model,
            case=case,
            projection=projection,
        )
        errors = self._verify(
            case=case,
            event_types=event_types,
            action_states=action_states,
            replay_passed=replay.passed,
            model_calls=len(model.calls),
            observation_count=observation_count,
            trigger_kinds=trigger_kinds,
            room_view_json=room_view_json,
            restarted_after_seed=restarted_after_seed,
            background_work_statuses=background_work_statuses,
            background_model_calls=background_model_calls,
            next_context_has_outcome_affect=next_context_has_outcome_affect,
        )
        output_hash = (
            hashlib.sha256(transport.bodies[-1].encode("utf-8")).hexdigest()
            if transport.bodies
            else None
        )
        return ScenarioRunResult(
            scenario_turn_id=case.entry.scenario_turn_id,
            scenario_family=case.entry.scenario_family,
            emotional_gold=case.entry.emotional_gold,
            fault=case.fault,
            world_id=world_id,
            output_hash=output_hash,
            event_types=event_types,
            terminal_action_states=action_states,
            replay_hash=projection.semantic_hash,
            replay_passed=replay.passed,
            model_calls=len(model.calls),
            observation_count=observation_count,
            trigger_kinds=trigger_kinds,
            room_view_hash=hashlib.sha256(room_view_json.encode("utf-8")).hexdigest(),
            restarted_after_seed=restarted_after_seed,
            background_work_statuses=background_work_statuses,
            background_model_calls=background_model_calls,
            verification_errors=errors,
        )

    @staticmethod
    def _next_context_has_outcome_affect(
        *,
        model: FakeCompanionModel,
        case: ScenarioCase,
        projection: LedgerProjection,
    ) -> bool:
        """Prove the next reply consumed this exact outcome and Affect episode.

        Merely checking that both slices are non-empty would allow an unrelated
        interaction appraisal or settled occurrence to mask a broken causal
        continuation.  The fixture must bind all three source identifiers
        from the seeded life event into the next app-owned capsule.
        """

        if case.execution != "seeded_world_outcome_affect":
            return True
        if not model.calls:
            return False
        try:
            supplied = json.loads(model.calls[-1][1]["content"])
            materials = supplied["inner_life_snapshot"]["materials"]
            world_life = materials["recent_self_experiences"]["items"]
            affect = materials["affect"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError):
            return False
        occurrence_id = f"occurrence:phase8:{case.entry.scenario_turn_id}"
        result_id = f"result:phase8:{case.entry.scenario_turn_id}:settled"
        settlement_refs = {
            item.event_id
            for item in projection.committed_world_event_refs
            if item.event_type == "WorldOccurrenceSettled"
        }
        appraisal_change_ids = {
            item.origin.change_id
            for item in projection.appraisals
            if item.origin.change_id.startswith(
                "change:character-interior-world-stimulus:appraisal:"
            )
            and any(ref.ref_id in settlement_refs for ref in item.evidence_refs)
        }
        has_settled_outcome = any(
            item.get("occurrence_id") == occurrence_id
            and item.get("result_id") == result_id
            for item in world_life
            if isinstance(item, dict)
        )
        # The compiler assigns the accepted episode a deterministic compiled
        # id.  Its stable causal identity is the Appraisal change authored by
        # the same CharacterInterior world-stimulus result, not an old
        # independently-authored NPC/Affect fixture id.
        has_causal_affect = any(
            appraisal_ref.get("accepted_change_id") in appraisal_change_ids
            for item in affect
            if isinstance(item, dict)
            for component in item.get("components", ())
            if isinstance(component, dict)
            for appraisal_ref in component.get("appraisal_refs", ())
            if isinstance(appraisal_ref, dict)
        )
        return has_settled_outcome and has_causal_affect

    @staticmethod
    async def _seed_world_outcome(
        *, app, case: ScenarioCase, now: datetime
    ) -> None:
        """Seed one durable, private occurrence through application commands.

        This intentionally stops at the open outcome deliberation trigger.
        Outcome settlement and the following CharacterInterior world-stimulus
        turn are separately exercised after restart.  The fixture model
        returns one source-bound private appraisal result with its optional
        Affect target; it does not install independent semantic adapters.
        """

        world_id = f"world:phase8-scenario:{case.entry.scenario_turn_id}"
        first_tick = now + timedelta(minutes=1)
        second_tick = now + timedelta(minutes=2)
        occurrence_id = f"occurrence:phase8:{case.entry.scenario_turn_id}"
        candidate_ref = f"candidate:phase8:{case.entry.scenario_turn_id}:settled"
        result_id = f"result:phase8:{case.entry.scenario_turn_id}:settled"
        await app.tick(
            tick_id=f"{case.entry.scenario_turn_id}:seed",
            logical_time_from=now,
            logical_time_to=first_tick,
            observed_at=first_tick,
            trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:seed",
            causation_id=f"scheduler:phase8:{case.entry.scenario_turn_id}:seed",
            correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}",
            reason="phase8_seeded_outcome",
        )
        candidate = OutcomeCandidateContent(
            candidate_result_ref=candidate_ref,
            result_id=result_id,
            result_payload_ref=f"payload:phase8:{case.entry.scenario_turn_id}:settled",
            result_payload_hash="sha256:" + "e" * 64,
            privacy_class="private",
            content_ref=f"content:phase8:{case.entry.scenario_turn_id}:settled",
            text="这件小事有了一个可验证的结果，但仍然只属于角色自己的生活。",
        )
        await app.commit_occurrence(
            OccurrenceContentCommitRequest(
                world_id=world_id,
                occurrence=WorldOccurrenceProjection(
                    occurrence_id=occurrence_id,
                    entity_revision=1,
                    trigger_ref=f"trigger:phase8:{case.entry.scenario_turn_id}",
                    participant_refs=("agent:companion",),
                    location_ref="room:scenario-private",
                    time_window=DueWindow(
                        opens_at=first_tick, closes_at=first_tick + timedelta(minutes=10)
                    ),
                    candidate_outcome_refs=(candidate_ref,),
                    visibility="private",
                    status="committed",
                ),
                candidate_contents=(candidate,),
                change_id=f"change:phase8:{case.entry.scenario_turn_id}:occurrence",
                transition_id=f"transition:phase8:{case.entry.scenario_turn_id}:occurrence",
                evidence_refs=(
                    EvidenceRef(
                        ref_id=f"clock:{first_tick.isoformat()}",
                        evidence_type="clock_observation",
                        claim_purpose="current_fact",
                    ),
                ),
                logical_time=first_tick,
                created_at=first_tick,
                actor="system:phase8-scenario",
                source="phase8-scenario",
                trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:occurrence",
                causation_id=f"cause:phase8:{case.entry.scenario_turn_id}:occurrence",
                correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}",
            )
        )
        await app.tick(
            tick_id=f"{case.entry.scenario_turn_id}:activate",
            logical_time_from=first_tick,
            logical_time_to=second_tick,
            observed_at=second_tick,
            trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:activate",
            causation_id=f"scheduler:phase8:{case.entry.scenario_turn_id}:activate",
            correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}",
            reason="phase8_activate_seeded_outcome",
        )
        observation = OutcomeObservation(
            schema_version="world-v2.1",
            observation_id=f"observation:phase8:{case.entry.scenario_turn_id}:settled",
            world_id=world_id,
            logical_time=second_tick,
            created_at=second_tick,
            trace_id=f"trace:phase8:{case.entry.scenario_turn_id}:outcome",
            causation_id=f"sensor:phase8:{case.entry.scenario_turn_id}",
            correlation_id=f"correlation:phase8:{case.entry.scenario_turn_id}",
            occurrence_id=occurrence_id,
            source_kind="committed_world_event",
            source_refs=(f"event:trigger:clock:{case.entry.scenario_turn_id}:activate",),
            observed_payload_ref=f"sensor:phase8:{case.entry.scenario_turn_id}:settled",
            observed_payload_hash="a" * 64,
            observed_at=second_tick,
            confidence_bp=9200,
        )
        recorded = await app.record_outcome_observation(observation)
        if await app.record_outcome_observation(observation) != recorded:
            raise ScenarioVerificationError("outcome observation ingress was not effect-once")

    async def run_frozen_suite(self, *, limit: int | None = None) -> ScenarioSuiteResult:
        cases = verify_frozen_scenario_corpus()
        if limit is not None:
            if limit < 1:
                raise ValueError("scenario limit must be positive")
            cases = cases[:limit]
        runs_list: list[ScenarioRunResult] = []
        for case in cases:
            runs_list.append(await self.run_case(case))
        runs = tuple(runs_list)
        suite = ScenarioSuiteResult(
            corpus_version=SCENARIO_CORPUS_VERSION,
            economy_profile_version=TEST_ECONOMY_PROFILE_VERSION,
            mechanism_baseline_version=FROZEN_OFFLINE_SUITE_BASELINE_VERSION,
            runs=runs,
        )
        if limit is None and suite.manifest_hash != FROZEN_OFFLINE_SUITE_MANIFEST_HASH:
            raise ScenarioVerificationError(
                "offline scenario manifest drifted; establish a new versioned mechanism baseline: "
                f"expected={FROZEN_OFFLINE_SUITE_MANIFEST_HASH}, actual={suite.manifest_hash}"
            )
        return suite

    @staticmethod
    def _verify(
        *,
        case: ScenarioCase,
        event_types: tuple[str, ...],
        action_states: tuple[str, ...],
        replay_passed: bool,
        model_calls: int,
        observation_count: int,
        trigger_kinds: tuple[str, ...],
        room_view_json: str,
        restarted_after_seed: bool,
        background_work_statuses: tuple[str, ...],
        background_model_calls: int,
        next_context_has_outcome_affect: bool,
    ) -> tuple[str, ...]:
        errors: list[str] = []
        required = {
            "ObservationRecorded",
            "ActionAuthorized",
            "ExternalObservationRecorded",
            "ExternalObservationProcessed",
        }
        required.update(case.required_event_types)
        missing = sorted(required.difference(event_types))
        if missing:
            errors.append("missing_required_events:" + ",".join(missing))
        expected_terminal = {
            "provider_failed": "failed",
            "provider_unknown": "unknown",
        }.get(case.fault, "delivered")
        if case.execution == "interruption":
            # The fixture's first action belongs to the pre-interruption
            # ingress and the second to the interrupting ingress.  A mere
            # unordered "one delivered, one authorized" check would accept
            # the exact regression this scenario is meant to catch: sending
            # stale content, then stranding the new response.
            expected_interruption_states = ("authorized", "delivered")
            if action_states != expected_interruption_states:
                errors.append("interruption_old_action_was_not_gated")
        elif case.execution == "seeded_expression_delay":
            # First ingress creates two materialized beats; the interruption
            # creates one fresh reply.  Every one must settle, proving that
            # the old delayed beat was gated/reviewed rather than lost or
            # emitted before the Logical Clock made it due.
            if action_states != ("delivered", "delivered", "delivered"):
                errors.append("delayed_expression_lifecycle_incomplete")
        elif action_states != (expected_terminal,) * len(case.turns):
            errors.append("terminal_action_state_mismatch")
        required_terminal_event = {
            "provider_failed": "ActionFailed",
            "provider_unknown": "ActionUnknown",
        }.get(case.fault)
        if required_terminal_event is not None and required_terminal_event not in event_types:
            errors.append("fault_terminal_event_missing")
        if not replay_passed:
            errors.append("replay_evaluator_failed")
        # test-economy-v1: regular chat has exactly one main fake call; this
        # runner deliberately does not configure background audit models.
        expected_model_calls = len(case.turns) + (
            2 if case.execution == "seeded_expression_delay" else 0
        )
        if model_calls != expected_model_calls:
            errors.append("test_economy_model_call_budget_exceeded")
        expected_observations = len(case.turns)
        if observation_count != expected_observations:
            errors.append("ingress_idempotency_failed")
        forbidden = sorted(set(case.forbidden_event_types).intersection(event_types))
        if forbidden:
            errors.append("forbidden_events_present:" + ",".join(forbidden))
        missing_triggers = sorted(set(case.required_trigger_kinds).difference(trigger_kinds))
        if missing_triggers:
            errors.append("missing_required_triggers:" + ",".join(missing_triggers))
        leaked_values = tuple(
            value for value in case.forbidden_room_view_values if value in room_view_json
        )
        if leaked_values:
            errors.append("room_projection_redaction_failed:" + ",".join(leaked_values))
        if case.execution == "seeded_world_outcome_affect":
            if not restarted_after_seed:
                errors.append("outcome_chain_did_not_restart_after_seed")
            if background_work_statuses != ("accepted", "accepted"):
                errors.append("outcome_character_interior_chain_incomplete")
            if background_model_calls != 2:
                errors.append("outcome_character_interior_model_call_budget_exceeded")
            if {"affect_deliberation", "relationship_deliberation"}.intersection(
                trigger_kinds
            ):
                errors.append("legacy_inner_author_trigger_present")
            if not next_context_has_outcome_affect:
                errors.append("next_reply_did_not_consume_outcome_affect_context")
        return tuple(errors)


def run_frozen_suite_sync(*, workdir: str | Path, limit: int | None = None) -> ScenarioSuiteResult:
    """CLI-safe synchronous wrapper; never performs network/model calls."""

    return asyncio.run(ScenarioRunner(workdir=workdir).run_frozen_suite(limit=limit))


__all__ = [
    "FROZEN_OFFLINE_SUITE_BASELINE_VERSION",
    "ScenarioRunResult",
    "ScenarioRunner",
    "ScenarioSuiteResult",
    "ScenarioVerificationError",
    "FROZEN_OFFLINE_SUITE_MANIFEST_HASH",
    "run_frozen_suite_sync",
]
