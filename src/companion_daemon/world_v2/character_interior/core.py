"""Unified Character Interior orchestration.

This module owns one cursor-pinned private interpretation and one stable inner
turn identity.  It never writes an independent state store and never converts a
provider failure into a character choice.
"""

from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict, deque
from datetime import datetime
import hashlib
import inspect
import json
import time
from dataclasses import dataclass
from typing import Any
from typing import Mapping

from pydantic import ValidationError

from ..recall_audit import PrefetchPresentationAudit, RecallAuditTrace
from ..schema_core import canonicalize_json_value
from ..schemas import ProjectionCursor
from .contracts import (
    FACET_NAMES,
    InnerDecision,
    InnerLifeSnapshot,
    InnerTransition,
    InteriorOpportunity,
    InteriorStimulus,
    _InteriorBinding,
    _InteriorContextView,
    _InteriorFacet,
    _InteriorSourceInventoryItem,
    _InstantPrivateSelf,
    _PrivateSelfLineage,
)
from .faculty_registry import _FacultyRegistry
from .ports import (
    _AuthorityRequest,
    _InteriorRoleRequest,
    _InteriorRoleResult,
    _PrefetchRequest,
    _PrefetchResult,
    _ProjectionMaterial,
    _RecallRequest,
    _RecallResult,
    _RoleResultContractError,
)


_CACHE_LIMIT = 128


def _digest(value: object) -> str:
    material = json.dumps(
        canonicalize_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


async def _resolve(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _turn_cache_key(subject: InteriorStimulus | InteriorOpportunity) -> str:
    capability = subject.capability_manifest
    subject_kind = "stimulus" if isinstance(subject, InteriorStimulus) else "opportunity"
    subject_ref = (
        subject.stimulus_ref if isinstance(subject, InteriorStimulus) else subject.opportunity_ref
    )
    digest = _digest(
        {
            "contract": "character-inner-turn-subject.1",
            "subject_kind": subject_kind,
            "subject_ref": subject_ref,
            "inner_turn_ref": subject.inner_turn_ref,
            "world_id": subject.world_id,
            "actor_ref": subject.actor_ref,
            "trigger_ref": subject.trigger_ref,
            "purpose": subject.purpose,
            "logical_time": subject.logical_time,
            "subject_source_refs": subject.source_refs,
            "context_note": subject.context_note,
            "viewer_scope": subject.viewer_scope,
            "privacy_ceiling": subject.privacy_ceiling,
            "budget_policy_ref": subject.budget_policy_ref,
            "capability_contract": capability.contract if capability else None,
            "capability_ref": capability.capability_ref if capability else None,
            "capability_kind": capability.capability_kind if capability else None,
            "capability_hash": capability.payload_hash if capability else None,
            "capability_source_refs": capability.source_refs if capability else None,
            "cursor": subject.cursor.model_dump(mode="json"),
        }
    )
    return f"character-inner-turn-subject:sha256:{digest}"


def _faculty_identity(faculty: object) -> dict[str, object]:
    supplied = getattr(faculty, "author_identity", None)
    if callable(supplied):
        supplied = supplied()
    if isinstance(supplied, Mapping):
        return {str(key): value for key, value in supplied.items()}
    return {
        "name": str(getattr(faculty, "name", type(faculty).__name__)),
        "version": str(getattr(faculty, "VERSION", getattr(faculty, "version", "unversioned"))),
        "implementation": f"{type(faculty).__module__}.{type(faculty).__qualname__}",
    }


def _inner_turn_id(
    subject: InteriorStimulus | InteriorOpportunity,
    *,
    snapshot: InnerLifeSnapshot | None,
    faculty: object,
) -> str:
    digest = _digest(
        {
            "contract": "character-inner-turn.3",
            "subject_identity": _turn_cache_key(subject),
            "snapshot_id": snapshot.snapshot_id if snapshot is not None else None,
            "snapshot_hash": snapshot.snapshot_hash if snapshot is not None else None,
            "snapshot_compiler": (
                snapshot.snapshot_compiler.model_dump(mode="json") if snapshot is not None else None
            ),
            "context_compiler": (
                snapshot.context_compiler.model_dump(mode="json") if snapshot is not None else None
            ),
            "viewer_scope": (
                snapshot.viewer_scope.model_dump(mode="json")
                if snapshot is not None
                else subject.viewer_scope
            ),
            "privacy_scope": (
                snapshot.privacy_scope.model_dump(mode="json")
                if snapshot is not None
                else subject.privacy_ceiling
            ),
            "budget_policy_ref": subject.budget_policy_ref,
            "author_route": _faculty_identity(faculty),
        }
    )
    return f"character-inner-turn:sha256:{digest}"


def _snapshot_cache_key(subject: InteriorStimulus | InteriorOpportunity) -> str:
    """Identity of one deterministic canonical projection, before Recall.

    Opportunity/stimulus identity is intentionally absent: two private turns
    may share one exact actor/cursor/scope projection while retaining distinct
    author identities.  Trigger and capability scope remain present because
    they can change the verified Capsule material or its redaction.
    """

    capability = subject.capability_manifest
    return "character-inner-snapshot-key:sha256:" + _digest(
        {
            "contract": "character-inner-snapshot-key.1",
            "world_id": subject.world_id,
            "actor_ref": subject.actor_ref,
            "trigger_ref": subject.trigger_ref,
            "cursor": subject.cursor.model_dump(mode="json"),
            "logical_time": subject.logical_time,
            "viewer_scope": subject.viewer_scope,
            "privacy_ceiling": subject.privacy_ceiling,
            "budget_policy_ref": subject.budget_policy_ref,
            "capability": (capability.model_dump(mode="json") if capability is not None else None),
        }
    )


class _InteriorTechnicalError(RuntimeError):
    def __init__(self, code: str, *, snapshot: InnerLifeSnapshot | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.snapshot = snapshot


@dataclass
class _TurnCacheEntry:
    snapshot: InnerLifeSnapshot | None = None
    prefetch_attempted: bool = False
    recall_attempted: bool = False
    correction_attempted: bool = False
    presented_prefetch_traces: list[PrefetchPresentationAudit] | None = None
    transition: InnerTransition | None = None
    decision: InnerDecision | None = None


class CharacterInterior:
    """Deep module for private experience, choice, and source-bound projection."""

    def __init__(
        self,
        *,
        projection: object,
        role: object,
        recall: object | None = None,
        authority: object | None = None,
        faculties: tuple[object, ...] = (),
    ) -> None:
        if not callable(getattr(projection, "project", None)):
            raise TypeError("CharacterInterior projection port must provide project")
        if not callable(getattr(role, "experience", None)) or not callable(
            getattr(role, "consider", None)
        ):
            raise TypeError("CharacterInterior role faculty must support both phases")
        if recall is not None and not callable(getattr(recall, "recall", None)):
            raise TypeError("CharacterInterior recall port must provide recall")
        if authority is not None and not callable(getattr(authority, "submit", None)):
            raise TypeError("CharacterInterior authority port must provide submit")
        self._projection = projection
        self._registry = _FacultyRegistry(primary=role, additional=faculties)
        self._recall = recall
        self._authority = authority
        self._cache: OrderedDict[str, _TurnCacheEntry] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._snapshot_cache: OrderedDict[str, InnerLifeSnapshot] = OrderedDict()
        self._snapshot_locks: dict[str, asyncio.Lock] = {}
        self._background_driver: object | None = None
        self._metrics: Counter[str] = Counter()
        self._snapshot_compile_ms: deque[float] = deque(maxlen=512)
        self._last_turn_metadata: dict[str, object] | None = None
        self._last_snapshot_faculty_state: dict[str, dict[str, object]] = {}
        self._last_terminal_status: str | None = None
        self._last_failure_code: str | None = None

    def _install_recall_port(self, recall: object) -> None:
        """Late-bind the ledger-backed Recall sidecar exactly once."""

        if self._recall is not None:
            raise RuntimeError("CharacterInterior recall port is already installed")
        if not callable(getattr(recall, "recall", None)):
            raise TypeError("CharacterInterior recall port must provide recall")
        self._recall = recall

    def _install_background_driver(self, driver: object) -> None:
        """Install the sole scheduler bridge without exposing its authors."""

        if self._background_driver is not None:
            raise RuntimeError("CharacterInterior background driver is already installed")
        required = (
            "is_bound_to",
            "drain_world_stimulus_once",
            "drain_reconsideration_once",
            "drain_proactive_once",
            "drain_private_impression_once",
        )
        if any(not callable(getattr(driver, name, None)) for name in required):
            raise TypeError("CharacterInterior background driver is incomplete")
        self._background_driver = driver

    def _is_bound_to(self, ledger: object) -> bool:
        driver = self._background_driver
        return bool(driver is not None and getattr(driver, "is_bound_to")(ledger))

    async def _drain_reconsideration_once(self):
        driver = self._background_driver
        if driver is None:
            return None
        return await _resolve(getattr(driver, "drain_reconsideration_once")())

    async def _drain_world_stimulus_once(self):
        driver = self._background_driver
        if driver is None:
            return None
        return await _resolve(getattr(driver, "drain_world_stimulus_once")())

    async def _drain_proactive_once(self):
        driver = self._background_driver
        if driver is None:
            return None
        return await _resolve(getattr(driver, "drain_proactive_once")())

    async def _drain_private_impression_once(self):
        driver = self._background_driver
        if driver is None:
            return None
        return await _resolve(getattr(driver, "drain_private_impression_once")())

    def _register_purpose_capability(
        self,
        purpose: str,
        payload: object,
        **metadata: object,
    ):
        """Let the frozen purpose Faculty seal one process-local capability.

        The application never receives the Faculty.  It hands opaque input to
        the Interior, whose registry dispatches and returns only the bounded
        manifest later consumed by ``consider``/``experience``.
        """

        faculty = self._registry.for_purpose(purpose)
        operation = getattr(faculty, "register_capability", None)
        if not callable(operation):
            raise RuntimeError(f"CharacterInterior purpose has no capability broker: {purpose}")
        return operation(payload, **metadata)

    def _consume_purpose_output(
        self,
        purpose: str,
        *,
        output_ref: str,
        output_hash: str,
    ) -> object:
        """Resolve one exact output without exposing its owning Faculty."""

        faculty = self._registry.for_purpose(purpose)
        operation = getattr(faculty, "consume_output", None)
        if not callable(operation):
            raise RuntimeError(f"CharacterInterior purpose has no output broker: {purpose}")
        return operation(output_ref=output_ref, output_hash=output_hash)

    def _purpose_transport_available(
        self,
        purpose: str,
        *,
        transport: str,
        payload: object,
    ) -> bool:
        """Query a Faculty's physical transport without exposing its author."""

        faculty = self._registry.for_purpose(purpose)
        operation = getattr(faculty, "transport_available", None)
        return bool(callable(operation) and operation(transport=transport, payload=payload))

    async def _continue_purpose_transport(
        self,
        purpose: str,
        *,
        transport: str,
        payload: object,
    ) -> object:
        """Read later bytes from one already-authored purpose decision.

        This is deliberately private and cannot create an InnerTurn or call a
        semantic author.  It only lets a purpose Faculty finish a physical
        transport whose head already crossed :meth:`consider`.
        """

        faculty = self._registry.for_purpose(purpose)
        operation = getattr(faculty, "continue_transport", None)
        if not callable(operation):
            raise RuntimeError(
                f"CharacterInterior purpose has no continuation transport: {purpose}"
            )
        return await _resolve(operation(transport=transport, payload=payload))

    def _publish_purpose_transport(
        self,
        purpose: str,
        *,
        transport: str,
        payload: object,
        output: object | None,
    ) -> None:
        """Publish only the final Interior-approved head to its continuation."""

        faculty = self._registry.for_purpose(purpose)
        operation = getattr(faculty, "publish_transport", None)
        if not callable(operation):
            raise RuntimeError(f"CharacterInterior purpose has no transport publisher: {purpose}")
        operation(transport=transport, payload=payload, output=output)

    def _advance_purpose_attention(
        self,
        purpose: str,
        *,
        attention_ref: str,
    ) -> None:
        """Invalidate unfinished physical bytes after newer durable attention."""

        faculty = self._registry.for_purpose(purpose)
        operation = getattr(faculty, "advance_attention", None)
        if callable(operation):
            operation(attention_ref=attention_ref)

    def runtime_health(self) -> dict[str, object]:
        """Expose topology and aggregate outcomes, never private inner material."""

        primary_author_route = _faculty_identity(self._registry.primary)
        primary_author_model = primary_author_route.get("model_id")
        if not isinstance(primary_author_model, str) or not primary_author_model:
            primary_author_model = "unknown"
        technical_failures = (
            self._metrics["consider:technical_failure"]
            + self._metrics["experience:technical_failure"]
        )
        projection_bound = bool(getattr(self._projection, "is_bound", True))
        authority_bound = bool(
            self._authority is not None and getattr(self._authority, "is_bound", True)
        )
        topology_issues = [
            name
            for name, present in (
                ("projection_unbound", projection_bound),
                ("recall_unbound", self._recall is not None),
                ("authority_unbound", authority_bound),
            )
            if not present
        ]
        prefetch_bound = bool(
            self._recall is not None and callable(getattr(self._recall, "prefetch", None))
        )
        if self._recall is not None and not prefetch_bound:
            topology_issues.append("automatic_prefetch_unbound")
        if self._registry.duplicate_purpose_owner_count:
            topology_issues.append("duplicate_purpose_owner")
        if self._registry.legacy_compatibility_route_names:
            topology_issues.append("legacy_compatibility_route_installed")
        if self._registry.semantic_author_count != 1:
            topology_issues.append("multiple_semantic_authors")
        snapshot_latency = sorted(self._snapshot_compile_ms)

        def percentile(fraction: float) -> float | None:
            if not snapshot_latency:
                return None
            index = min(
                len(snapshot_latency) - 1,
                max(0, int(round((len(snapshot_latency) - 1) * fraction))),
            )
            return round(snapshot_latency[index], 3)

        if technical_failures:
            status = "degraded"
        elif topology_issues:
            status = "not_ready"
        else:
            status = "ready"
        return {
            "contract": "character-interior-runtime-health.2",
            "status": status,
            "topology_issues": topology_issues,
            "installed": True,
            # These fields are derived from the same frozen registry and
            # process-local conflict counters as the rest of this snapshot.
            # Platform health surfaces must forward them, not reconstruct a
            # second view of the protagonist-author topology.
            "semantic_author_count": self._registry.semantic_author_count,
            "primary_author_model": primary_author_model,
            "primary_author_route": primary_author_route,
            "faculty_names": list(self._registry.faculty_names),
            "faculty_registry_frozen": True,
            "primary_author_faculty": self._registry.primary_name,
            "purpose_faculties": list(self._registry.purpose_names),
            "active_route": {
                "character_author": self._registry.primary_name,
                "projection": type(self._projection).__name__,
                "recall": type(self._recall).__name__ if self._recall is not None else None,
                "authority": (
                    type(self._authority).__name__ if self._authority is not None else None
                ),
            },
            "projection_bound": projection_bound,
            "recall_bound": self._recall is not None,
            "automatic_prefetch_bound": prefetch_bound,
            "authority_bound": authority_bound,
            "cached_inner_turns": len(self._cache),
            "cached_snapshots": len(self._snapshot_cache),
            "snapshot_compile_latency_ms": {
                "samples": len(snapshot_latency),
                "p50": percentile(0.50),
                "p95": percentile(0.95),
                "p99": percentile(0.99),
            },
            "snapshot_cache_hits": self._metrics["snapshot_cache_hit"],
            "snapshot_cache_misses": self._metrics["snapshot_cache_miss"],
            "snapshot_hash_divergence_count": self._metrics["snapshot_hash_divergence"],
            "stale_cursor_rebuild_count": self._metrics["stale_cursor_rebuild"],
            "faculty_state": dict(self._last_snapshot_faculty_state),
            "consideration_counts": {
                status: self._metrics[f"consider:{status}"]
                for status in ("decided", "model_silent", "technical_failure")
            },
            "experience_counts": {
                status: self._metrics[f"experience:{status}"]
                for status in ("transitioned", "model_no_change", "technical_failure")
            },
            "recall_attempt_count": self._metrics["recall_attempt"],
            "recall": {
                "requests": self._metrics["recall_attempt"],
                "hits": self._metrics["recall_hit"],
                "empty": self._metrics["recall_empty"],
                "adopted": self._metrics["recall_adopted"],
                "reintegrated": self._metrics["recall_reintegrated"],
                "source_rejections": self._metrics["recall_source_rejection"],
            },
            "automatic_prefetch": {
                "requests": self._metrics["prefetch_attempt"],
                "hits": self._metrics["prefetch_hit"],
                "empty": self._metrics["prefetch_empty"],
                "failures": self._metrics["prefetch_failure"],
                "invalid_policy": self._metrics["prefetch_invalid_policy"],
            },
            "correction_attempt_count": self._metrics["correction_attempt"],
            "purpose_counts": {
                key.removeprefix("purpose:"): value
                for key, value in sorted(self._metrics.items())
                if key.startswith("purpose:")
            },
            "typed_proposal_submitted_count": self._metrics["typed_proposal_submitted"],
            "last_inner_turn": self._last_turn_metadata,
            "last_terminal_status": self._last_terminal_status,
            "last_failure_code": self._last_failure_code,
            "legacy_interface_invocations": self._metrics["legacy_interface_invocation"],
            "parallel_character_author_conflicts": self._metrics[
                "parallel_character_author_conflict"
            ],
            "dual_write_conflicts": self._metrics["dual_write_conflict"],
            "effect_once_join_count": self._metrics["effect_once_join"],
            "topology_evidence": {
                "public_role_entrypoints": ["experience", "consider"],
                "snapshot_entrypoint": "project",
                "purpose_owner_count": len(self._registry.purpose_names),
                "purpose_owner_counts": dict(self._registry.purpose_owner_counts),
                "duplicate_purpose_owner_count": (self._registry.duplicate_purpose_owner_count),
                "legacy_compatibility_route_installed": bool(
                    self._registry.legacy_compatibility_route_names
                ),
                "legacy_compatibility_route_names": list(
                    self._registry.legacy_compatibility_route_names
                ),
                "semantic_author_ids": list(self._registry.semantic_author_ids),
                "purpose_semantic_author_ids": dict(self._registry.purpose_semantic_author_ids),
                "unverified_author_faculty_names": list(
                    self._registry.unverified_author_faculty_names
                ),
                # Source reviewers are deterministic epistemic boundaries and
                # are intentionally outside this protagonist-author registry.
                "semantic_author_scope": "character_purpose_faculties_only",
                "evidence_contract": "frozen-faculty-registry.1",
            },
            "projection_contract": "subject_bound",
        }

    async def project(
        self,
        subject: InteriorStimulus | InteriorOpportunity,
    ) -> InnerLifeSnapshot:
        """Return one canonical snapshot without invoking Recall or a role model."""

        snapshot_key = _snapshot_cache_key(subject)
        lock = self._snapshot_locks.setdefault(snapshot_key, asyncio.Lock())
        async with lock:
            cached = self._snapshot_cache.get(snapshot_key)
            if cached is not None:
                self._validate_subject_sources(subject, cached)
                self._snapshot_cache.move_to_end(snapshot_key)
                self._metrics["snapshot_cache_hit"] += 1
                self._record_snapshot_health(cached)
                return cached
            self._metrics["snapshot_cache_miss"] += 1
            started = time.perf_counter()
            snapshot = await self._compile_snapshot(subject)
            self._snapshot_compile_ms.append((time.perf_counter() - started) * 1000)
            self._validate_subject_sources(subject, snapshot)
            self._record_snapshot_health(snapshot)
            self._snapshot_cache[snapshot_key] = snapshot
            self._snapshot_cache.move_to_end(snapshot_key)
            self._trim_cache()
            return snapshot

    async def experience(self, stimulus: InteriorStimulus) -> InnerTransition:
        """Let the role interpret a stimulus and submit only its sparse typed effects."""

        faculty = self._registry.for_purpose(stimulus.purpose)
        cache_key = _turn_cache_key(stimulus)
        turn_id = _inner_turn_id(stimulus, snapshot=None, faculty=faculty)
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            try:
                canonical_snapshot = await self.project(stimulus)
                turn_id = _inner_turn_id(
                    stimulus,
                    snapshot=canonical_snapshot,
                    faculty=faculty,
                )
                entry = self._cache.get(cache_key)
                if entry is not None and entry.transition is not None:
                    if entry.transition.inner_turn_id != turn_id:
                        self._metrics["parallel_character_author_conflict"] += 1
                        raise _InteriorTechnicalError(
                            "cached_inner_turn_identity_mismatch",
                            snapshot=canonical_snapshot,
                        )
                    self._metrics["effect_once_join"] += 1
                    return entry.transition
                snapshot = await self._snapshot_without_relocking(
                    stimulus,
                    cache_key,
                    canonical_snapshot=canonical_snapshot,
                )
                snapshot = await self._prefetch_for_first_pass(
                    subject=stimulus,
                    faculty=faculty,
                    turn_id=turn_id,
                    snapshot=snapshot,
                    entry=self._cache[cache_key],
                )
                request = _InteriorRoleRequest(
                    inner_turn_id=turn_id,
                    phase="experience",
                    subject_ref=stimulus.stimulus_ref,
                    trigger_ref=stimulus.trigger_ref,
                    purpose=stimulus.purpose,
                    context_note=stimulus.context_note,
                    subject_source_refs=stimulus.source_refs,
                    capability_manifest=stimulus.capability_manifest,
                    snapshot=snapshot,
                    recall_completed=self._cache[cache_key].recall_attempted,
                )
                result, snapshot, private_self_lineage = await self._run_role_phase(
                    method_name="experience",
                    request=request,
                    snapshot=snapshot,
                    entry=self._cache[cache_key],
                    final_statuses={"transition", "no_change"},
                )
                proposal_refs = await self._submit_proposals(
                    turn_id=turn_id,
                    subject=stimulus,
                    snapshot=snapshot,
                    proposals=result.proposals,
                    author_lineage=result.author_lineage,
                    private_self_lineage=private_self_lineage,
                    decision_material=result,
                )
                transition = InnerTransition(
                    inner_turn_id=turn_id,
                    stimulus_ref=stimulus.stimulus_ref,
                    actor_ref=stimulus.actor_ref,
                    cursor=stimulus.cursor,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_hash=snapshot.snapshot_hash,
                    status=("transitioned" if result.status == "transition" else "model_no_change"),
                    summary=result.summary,
                    attended_source_refs=result.attended_source_refs,
                    instant_private_self=_InstantPrivateSelf(
                        summary=result.summary,
                        attended_source_refs=result.attended_source_refs,
                    ),
                    private_self_lineage=private_self_lineage,
                    proposal_refs=proposal_refs,
                    author_lineage=result.author_lineage,
                    presented_prefetch_traces=tuple(
                        self._cache[cache_key].presented_prefetch_traces or ()
                    ),
                    failure_code=None,
                )
            except _InteriorTechnicalError as exc:
                turn_id = _inner_turn_id(
                    stimulus,
                    snapshot=exc.snapshot,
                    faculty=faculty,
                )
                transition = self._failed_transition(stimulus, turn_id=turn_id, error=exc)
            except (ValidationError, TypeError, ValueError):
                transition = self._failed_transition(
                    stimulus,
                    turn_id=turn_id,
                    error=_InteriorTechnicalError("invalid_role_result"),
                )
            except Exception:
                transition = self._failed_transition(
                    stimulus,
                    turn_id=turn_id,
                    error=_InteriorTechnicalError("interior_runtime_failure"),
                )
            entry = self._cache.setdefault(cache_key, _TurnCacheEntry())
            entry.transition = transition
            self._cache.move_to_end(cache_key)
            self._trim_cache()
            self._record_terminal(
                "experience",
                transition.status,
                transition.failure_code,
                purpose=stimulus.purpose,
                inner_turn_id=transition.inner_turn_id,
                cursor=transition.cursor,
                snapshot_hash=transition.snapshot_hash,
            )
            await self._finish_prefetch_turn(stimulus, turn_id=transition.inner_turn_id)
            return transition

    async def consider(self, opportunity: InteriorOpportunity) -> InnerDecision:
        """Let the character choose; a technical error can never become silence."""

        faculty = self._registry.for_purpose(opportunity.purpose)
        cache_key = _turn_cache_key(opportunity)
        turn_id = _inner_turn_id(opportunity, snapshot=None, faculty=faculty)
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            try:
                canonical_snapshot = await self.project(opportunity)
                turn_id = _inner_turn_id(
                    opportunity,
                    snapshot=canonical_snapshot,
                    faculty=faculty,
                )
                entry = self._cache.get(cache_key)
                if entry is not None and entry.decision is not None:
                    if entry.decision.inner_turn_id != turn_id:
                        self._metrics["parallel_character_author_conflict"] += 1
                        raise _InteriorTechnicalError(
                            "cached_inner_turn_identity_mismatch",
                            snapshot=canonical_snapshot,
                        )
                    self._metrics["effect_once_join"] += 1
                    return entry.decision
                snapshot = await self._snapshot_without_relocking(
                    opportunity,
                    cache_key,
                    canonical_snapshot=canonical_snapshot,
                )
                snapshot = await self._prefetch_for_first_pass(
                    subject=opportunity,
                    faculty=faculty,
                    turn_id=turn_id,
                    snapshot=snapshot,
                    entry=self._cache[cache_key],
                )
                request = _InteriorRoleRequest(
                    inner_turn_id=turn_id,
                    phase="consider",
                    subject_ref=opportunity.opportunity_ref,
                    trigger_ref=opportunity.trigger_ref,
                    purpose=opportunity.purpose,
                    context_note=opportunity.context_note,
                    subject_source_refs=opportunity.source_refs,
                    capability_manifest=opportunity.capability_manifest,
                    snapshot=snapshot,
                    recall_completed=self._cache[cache_key].recall_attempted,
                )
                result, snapshot, private_self_lineage = await self._run_role_phase(
                    method_name="consider",
                    request=request,
                    snapshot=snapshot,
                    entry=self._cache[cache_key],
                    final_statuses={"decision", "silent"},
                )
                proposal_refs = await self._submit_proposals(
                    turn_id=turn_id,
                    subject=opportunity,
                    snapshot=snapshot,
                    proposals=result.proposals,
                    author_lineage=result.author_lineage,
                    private_self_lineage=private_self_lineage,
                    decision_material=result,
                )
                decision = InnerDecision(
                    inner_turn_id=turn_id,
                    opportunity_ref=opportunity.opportunity_ref,
                    actor_ref=opportunity.actor_ref,
                    cursor=opportunity.cursor,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_hash=snapshot.snapshot_hash,
                    status="decided" if result.status == "decision" else "model_silent",
                    summary=result.summary,
                    attended_source_refs=result.attended_source_refs,
                    instant_private_self=_InstantPrivateSelf(
                        summary=result.summary,
                        attended_source_refs=result.attended_source_refs,
                    ),
                    private_self_lineage=private_self_lineage,
                    decision=result.decision,
                    proposal_refs=proposal_refs,
                    author_lineage=result.author_lineage,
                    presented_prefetch_traces=tuple(
                        self._cache[cache_key].presented_prefetch_traces or ()
                    ),
                    failure_code=None,
                )
            except _InteriorTechnicalError as exc:
                turn_id = _inner_turn_id(
                    opportunity,
                    snapshot=exc.snapshot,
                    faculty=faculty,
                )
                decision = self._failed_decision(opportunity, turn_id=turn_id, error=exc)
            except (ValidationError, TypeError, ValueError):
                decision = self._failed_decision(
                    opportunity,
                    turn_id=turn_id,
                    error=_InteriorTechnicalError("invalid_role_result"),
                )
            except Exception:
                decision = self._failed_decision(
                    opportunity,
                    turn_id=turn_id,
                    error=_InteriorTechnicalError("interior_runtime_failure"),
                )
            entry = self._cache.setdefault(cache_key, _TurnCacheEntry())
            entry.decision = decision
            self._cache.move_to_end(cache_key)
            self._trim_cache()
            self._record_terminal(
                "consider",
                decision.status,
                decision.failure_code,
                purpose=opportunity.purpose,
                inner_turn_id=decision.inner_turn_id,
                cursor=decision.cursor,
                snapshot_hash=decision.snapshot_hash,
            )
            await self._finish_prefetch_turn(opportunity, turn_id=decision.inner_turn_id)
            return decision

    async def _snapshot_without_relocking(
        self,
        subject: InteriorStimulus | InteriorOpportunity,
        cache_key: str,
        *,
        canonical_snapshot: InnerLifeSnapshot | None = None,
    ) -> InnerLifeSnapshot:
        entry = self._cache.get(cache_key)
        if entry is not None and entry.snapshot is not None:
            self._validate_subject_sources(subject, entry.snapshot)
            return entry.snapshot
        snapshot = canonical_snapshot or await self.project(subject)
        entry = entry or _TurnCacheEntry()
        entry.snapshot = snapshot
        self._cache[cache_key] = entry
        return snapshot

    async def _compile_snapshot(
        self,
        subject: InteriorStimulus | InteriorOpportunity,
    ) -> InnerLifeSnapshot:
        try:
            raw = await _resolve(self._projection.project(subject=subject))
            if isinstance(raw, InnerLifeSnapshot):
                snapshot = InnerLifeSnapshot.model_validate(raw.model_dump(mode="python"))
            else:
                material = _ProjectionMaterial.model_validate(raw)
                snapshot = self._build_snapshot(material)
        except (ValidationError, TypeError, ValueError) as exc:
            raise _InteriorTechnicalError("invalid_projection") from exc
        except Exception as exc:
            raise _InteriorTechnicalError("projection_unavailable") from exc
        if snapshot.world_id != subject.world_id or snapshot.actor_ref != subject.actor_ref:
            raise _InteriorTechnicalError("projection_actor_mismatch")
        if snapshot.cursor != subject.cursor:
            raise _InteriorTechnicalError("projection_cursor_mismatch")
        if snapshot.logical_time != subject.logical_time:
            raise _InteriorTechnicalError("projection_logical_time_mismatch")
        if (
            snapshot.viewer_scope.availability == "available"
            and snapshot.viewer_scope.value != subject.viewer_scope
        ):
            raise _InteriorTechnicalError("projection_viewer_scope_mismatch")
        if (
            snapshot.privacy_scope.availability == "available"
            and snapshot.privacy_scope.value != subject.privacy_ceiling
        ):
            raise _InteriorTechnicalError("projection_privacy_scope_mismatch")
        return self._bind_capability_scope(snapshot, subject)

    async def _recall_once(
        self,
        *,
        request: _InteriorRoleRequest,
        query: str,
        snapshot: InnerLifeSnapshot,
    ) -> InnerLifeSnapshot:
        if self._recall is None:
            raise _InteriorTechnicalError("recall_unavailable", snapshot=snapshot)
        recall_request = _RecallRequest(
            inner_turn_id=request.inner_turn_id,
            world_id=snapshot.world_id,
            actor_ref=snapshot.actor_ref,
            cursor=snapshot.cursor,
            trigger_ref=request.trigger_ref,
            query=query,
            subject_source_refs=request.subject_source_refs,
            snapshot=snapshot,
        )
        try:
            raw_recall = await _resolve(self._recall.recall(recall_request))
            if raw_recall is None:
                self._metrics["recall_empty"] += 1
                return snapshot
            recalled = _RecallResult.model_validate(raw_recall)
        except (ValidationError, TypeError, ValueError) as exc:
            self._metrics["recall_source_rejection"] += 1
            raise _InteriorTechnicalError("invalid_recall_result", snapshot=snapshot) from exc
        except Exception as exc:
            raise _InteriorTechnicalError("recall_unavailable", snapshot=snapshot) from exc
        if recalled.world_id != snapshot.world_id or recalled.actor_ref != snapshot.actor_ref:
            self._metrics["recall_source_rejection"] += 1
            raise _InteriorTechnicalError("recall_actor_mismatch", snapshot=snapshot)
        if recalled.cursor != snapshot.cursor:
            self._metrics["recall_source_rejection"] += 1
            raise _InteriorTechnicalError("recall_cursor_mismatch", snapshot=snapshot)
        if recalled.source_refs:
            self._metrics["recall_hit"] += 1
        else:
            self._metrics["recall_empty"] += 1
        merged = self._merge_recall(snapshot, recalled)
        if recalled.prefetch is not None and recalled.prefetch.source_refs:
            self._validate_prefetch_identity(recalled.prefetch, snapshot)
            merged = self._merge_prefetch(merged, recalled.prefetch)
        self._record_snapshot_health(merged)
        return merged

    async def _prefetch_for_first_pass(
        self,
        *,
        subject: InteriorStimulus | InteriorOpportunity,
        faculty: object,
        turn_id: str,
        snapshot: InnerLifeSnapshot,
        entry: _TurnCacheEntry,
    ) -> InnerLifeSnapshot:
        """Offer scheduled candidates without turning them into a role choice."""

        raw_join = getattr(faculty, "automatic_prefetch_join_seconds", None)
        operation = getattr(self._recall, "prefetch", None)
        if raw_join is None or not callable(operation) or entry.prefetch_attempted:
            return snapshot
        if isinstance(raw_join, bool) or not isinstance(raw_join, (int, float)):
            self._metrics["prefetch_invalid_policy"] += 1
            return snapshot
        entry.prefetch_attempted = True
        self._metrics["prefetch_attempt"] += 1
        request = _PrefetchRequest(
            inner_turn_id=turn_id,
            world_id=subject.world_id,
            actor_ref=subject.actor_ref,
            cursor=subject.cursor,
            trigger_ref=subject.trigger_ref,
            subject_source_refs=subject.source_refs,
            snapshot=snapshot,
            join_seconds=float(raw_join),
        )
        try:
            raw = await _resolve(operation(request))
            if raw is None:
                self._metrics["prefetch_empty"] += 1
                return snapshot
            prefetched = _PrefetchResult.model_validate(raw)
            self._validate_prefetch_identity(prefetched, snapshot)
        except Exception:
            # Automatic attention is optional candidate material.  Its
            # absence must never be misreported as the character's silence or
            # prevent the role model from seeing the pinned base snapshot.
            self._metrics["prefetch_failure"] += 1
            return snapshot
        if not prefetched.source_refs:
            self._metrics["prefetch_empty"] += 1
            return snapshot
        merged = self._merge_prefetch(snapshot, prefetched)
        entry.snapshot = merged
        self._metrics["prefetch_hit"] += 1
        self._record_snapshot_health(merged)
        return merged

    async def _finish_prefetch_turn(
        self,
        subject: InteriorStimulus | InteriorOpportunity,
        *,
        turn_id: str,
    ) -> None:
        operation = getattr(self._recall, "finish_turn", None)
        if not callable(operation):
            return
        try:
            await _resolve(
                operation(
                    inner_turn_id=turn_id,
                    cursor=subject.cursor,
                    trigger_ref=subject.trigger_ref,
                )
            )
        except Exception:
            # Cleanup is generation-token guarded and observational.  A
            # failed cleanup cannot rewrite an already-authored outcome.
            self._metrics["prefetch_cleanup_failure"] += 1

    @staticmethod
    def _validate_prefetch_identity(
        prefetched: _PrefetchResult,
        snapshot: InnerLifeSnapshot,
    ) -> None:
        if prefetched.world_id != snapshot.world_id or prefetched.actor_ref != snapshot.actor_ref:
            raise ValueError("prefetch actor does not match its pinned snapshot")
        if prefetched.cursor != snapshot.cursor:
            raise ValueError("prefetch cursor does not match its pinned snapshot")

    @staticmethod
    def _build_snapshot(material: _ProjectionMaterial) -> InnerLifeSnapshot:
        situation = _InteriorContextView.from_material(
            availability=material.situation.availability,
            content=material.situation.content,
            source_refs=material.situation.source_refs,
        )
        continuity = _InteriorContextView.from_material(
            availability=material.continuity.availability,
            content=material.continuity.content,
            source_refs=material.continuity.source_refs,
        )
        facets = tuple(
            _InteriorFacet(
                name=name,
                **_InteriorContextView.from_material(
                    availability=material.facets[name].availability,
                    content=material.facets[name].content,
                    source_refs=material.facets[name].source_refs,
                ).model_dump(mode="python"),
            )
            for name in FACET_NAMES
        )
        return CharacterInterior._assemble_snapshot(
            world_id=material.world_id,
            actor_ref=material.actor_ref,
            cursor=material.cursor,
            logical_time=material.logical_time,
            situation=situation,
            continuity=continuity,
            facets=facets,
        )

    @staticmethod
    def _merge_recall(
        snapshot: InnerLifeSnapshot,
        recalled: _RecallResult,
    ) -> InnerLifeSnapshot:
        facets: list[_InteriorFacet] = []
        for facet in snapshot.facet_views:
            if facet.name != "selective_memory":
                facets.append(facet)
                continue
            if not recalled.source_refs:
                # An audited empty retrieval still consumes the one bounded
                # pull and is carried to the final model audit, but it cannot
                # manufacture an "available" memory facet without a source.
                facets.append(facet)
                continue
            content = dict(facet.content)
            content.update(recalled.content)
            raw_material_keys = facet.content.get("material_keys")
            material_keys = (
                [key for key in raw_material_keys if isinstance(key, str)]
                if isinstance(raw_material_keys, list)
                else []
            )
            if "selected_recall" not in material_keys:
                material_keys.append("selected_recall")
            # Recall result content may not forge the structural Faculty-to-
            # material binding used by the provider view and health evidence.
            content["material_keys"] = material_keys
            refs = tuple(dict.fromkeys((*facet.source_refs, *recalled.source_refs)))
            view = _InteriorContextView.from_material(
                availability="available",
                content=content,
                source_refs=refs,
            )
            facets.append(
                _InteriorFacet(
                    name="selective_memory",
                    **view.model_dump(mode="python"),
                )
            )
        materials = dict(snapshot.materials)
        materials["selected_recall"] = {
            "content": recalled.content,
            "source_refs": list(recalled.source_refs),
        }
        recalled_hash = _digest(recalled.content)
        inventory = tuple(
            dict.fromkeys(
                (
                    *snapshot.source_inventory,
                    *(
                        _InteriorSourceInventoryItem(
                            source_ref=source_ref,
                            scope="facet:selective_memory:recall",
                            content_hash=recalled_hash,
                        )
                        for source_ref in recalled.source_refs
                    ),
                )
            )
        )
        source_refs = tuple(dict.fromkeys(item.source_ref for item in inventory))
        return InnerLifeSnapshot.create(
            availability="available",
            world_id=snapshot.world_id,
            actor_ref=snapshot.actor_ref,
            cursor=snapshot.cursor,
            logical_time=snapshot.logical_time,
            situation=snapshot.situation,
            continuity=snapshot.continuity,
            facet_views=tuple(facets),
            materials=materials,
            source_refs=source_refs,
            source_inventory=inventory,
            viewer_scope=snapshot.viewer_scope,
            privacy_scope=snapshot.privacy_scope,
            capability_scope=snapshot.capability_scope,
            context_compiler=snapshot.context_compiler,
            snapshot_compiler=snapshot.snapshot_compiler,
            truncation=snapshot.truncation,
            recall_trace_json=recalled.recall_trace_json,
            prefetch_trace_json=snapshot.prefetch_trace_json,
        )

    @staticmethod
    def _merge_prefetch(
        snapshot: InnerLifeSnapshot,
        prefetched: _PrefetchResult,
    ) -> InnerLifeSnapshot:
        """Replace the automatic candidate slice without selecting it.

        A later semantic result may supersede a local first-pass candidate for
        the same InnerTurn.  The selected-Recall material, if any, remains a
        distinct key and is never inferred from this automatic environment.
        """

        old_inventory = tuple(
            item for item in snapshot.source_inventory if item.scope != "automatic_prefetch"
        )
        remaining_refs = {item.source_ref for item in old_inventory}
        content_hash = _digest(prefetched.content)
        inventory = (
            *old_inventory,
            *(
                _InteriorSourceInventoryItem(
                    source_ref=source_ref,
                    scope="automatic_prefetch",
                    content_hash=content_hash,
                )
                for source_ref in prefetched.source_refs
            ),
        )
        facets: list[_InteriorFacet] = []
        for facet in snapshot.facet_views:
            if facet.name != "selective_memory":
                facets.append(facet)
                continue
            raw_keys = facet.content.get("material_keys")
            material_keys = (
                [key for key in raw_keys if isinstance(key, str) and key != "automatic_prefetch"]
                if isinstance(raw_keys, list)
                else []
            )
            material_keys.insert(0, "automatic_prefetch")
            content = dict(facet.content)
            content["material_keys"] = material_keys
            refs = tuple(
                dict.fromkeys(
                    (
                        *(ref for ref in facet.source_refs if ref in remaining_refs),
                        *prefetched.source_refs,
                    )
                )
            )
            view = _InteriorContextView.from_material(
                availability="available",
                content=content,
                source_refs=refs,
            )
            facets.append(
                _InteriorFacet(
                    name="selective_memory",
                    **view.model_dump(mode="python"),
                )
            )
        materials = dict(snapshot.materials)
        materials["automatic_prefetch"] = prefetched.content
        source_refs = tuple(dict.fromkeys(item.source_ref for item in inventory))
        return InnerLifeSnapshot.create(
            availability="available",
            world_id=snapshot.world_id,
            actor_ref=snapshot.actor_ref,
            cursor=snapshot.cursor,
            logical_time=snapshot.logical_time,
            situation=snapshot.situation,
            continuity=snapshot.continuity,
            facet_views=tuple(facets),
            materials=materials,
            source_refs=source_refs,
            source_inventory=tuple(inventory),
            viewer_scope=snapshot.viewer_scope,
            privacy_scope=snapshot.privacy_scope,
            capability_scope=snapshot.capability_scope,
            context_compiler=snapshot.context_compiler,
            snapshot_compiler=snapshot.snapshot_compiler,
            truncation=snapshot.truncation,
            recall_trace_json=snapshot.recall_trace_json,
            prefetch_trace_json=prefetched.prefetch_trace_json,
        )

    @staticmethod
    def _bind_capability_scope(
        snapshot: InnerLifeSnapshot,
        subject: InteriorStimulus | InteriorOpportunity,
    ) -> InnerLifeSnapshot:
        manifest = subject.capability_manifest
        if manifest is None:
            return snapshot
        capability_value: dict[str, object] = manifest.binding_value()
        if snapshot.capability_scope.availability == "available":
            capability_value["projected"] = snapshot.capability_scope.value
        return InnerLifeSnapshot.create(
            availability=snapshot.availability,
            world_id=snapshot.world_id,
            actor_ref=snapshot.actor_ref,
            cursor=snapshot.cursor,
            logical_time=snapshot.logical_time,
            situation=snapshot.situation,
            continuity=snapshot.continuity,
            facet_views=snapshot.facet_views,
            materials=snapshot.materials,
            source_refs=snapshot.source_refs,
            source_inventory=snapshot.source_inventory,
            viewer_scope=snapshot.viewer_scope,
            privacy_scope=snapshot.privacy_scope,
            capability_scope=_InteriorBinding.available(capability_value),
            context_compiler=snapshot.context_compiler,
            snapshot_compiler=snapshot.snapshot_compiler,
            truncation=snapshot.truncation,
            recall_trace_json=snapshot.recall_trace_json,
            prefetch_trace_json=snapshot.prefetch_trace_json,
        )

    @staticmethod
    def _assemble_snapshot(
        *,
        world_id: str,
        actor_ref: str,
        cursor: ProjectionCursor,
        logical_time: datetime,
        situation: _InteriorContextView,
        continuity: _InteriorContextView,
        facets: tuple[_InteriorFacet, ...],
    ) -> InnerLifeSnapshot:
        scoped = (
            ("situation", situation),
            ("continuity", continuity),
            *((f"facet:{facet.name}", facet) for facet in facets),
        )
        inventory = tuple(
            _InteriorSourceInventoryItem(
                source_ref=source_ref,
                scope=scope,
                content_hash=view.content_hash,
            )
            for scope, view in scoped
            for source_ref in view.source_refs
        )
        source_refs = tuple(dict.fromkeys(item.source_ref for item in inventory))
        materials = {
            "situation": dict(situation.content),
            "continuity": dict(continuity.content),
            **{facet.name: dict(facet.content) for facet in facets},
        }
        return InnerLifeSnapshot.create(
            availability="available",
            world_id=world_id,
            actor_ref=actor_ref,
            cursor=cursor,
            logical_time=logical_time,
            situation=situation,
            continuity=continuity,
            facet_views=facets,
            materials=materials,
            source_refs=source_refs,
            source_inventory=inventory,
            viewer_scope=_InteriorBinding.unavailable("viewer_scope_not_requested"),
            privacy_scope=_InteriorBinding.unavailable("privacy_scope_not_requested"),
            capability_scope=_InteriorBinding.unavailable("capability_scope_not_requested"),
            context_compiler=_InteriorBinding.unavailable("context_compiler_not_requested"),
            snapshot_compiler=_InteriorBinding.available("character-interior-projection.1"),
            truncation=_InteriorBinding.unavailable("truncation_not_requested"),
        )

    @staticmethod
    def _validate_subject_sources(
        subject: InteriorStimulus | InteriorOpportunity,
        snapshot: InnerLifeSnapshot,
    ) -> None:
        if set(subject.source_refs) - set(snapshot.source_refs):
            raise _InteriorTechnicalError("subject_source_unpinned", snapshot=snapshot)

    async def _run_role_phase(
        self,
        *,
        method_name: str,
        request: _InteriorRoleRequest,
        snapshot: InnerLifeSnapshot,
        entry: _TurnCacheEntry,
        final_statuses: set[str],
    ) -> tuple[_InteriorRoleResult, InnerLifeSnapshot, _PrivateSelfLineage]:
        async def invoke(
            current_request: _InteriorRoleRequest,
            *,
            allowed_statuses: set[str],
        ) -> _InteriorRoleResult:
            faculty = self._registry.for_purpose(current_request.purpose)
            method = getattr(faculty, method_name)
            structural_failure_code: str | None = None
            try:
                raw = await _resolve(method(current_request))
            except _RoleResultContractError as exc:
                structural_failure_code = exc.code
            except Exception as exc:
                import logging

                logging.getLogger(__name__).warning(
                    "role faculty inner failure method=%s type=%s detail=%s",
                    method_name,
                    type(exc).__name__,
                    str(exc)[:300],
                    exc_info=True,
                )
                raise _InteriorTechnicalError(
                    "role_faculty_unavailable", snapshot=current_request.snapshot
                ) from exc
            else:
                try:
                    validated = self._validate_role_result(
                        raw,
                        snapshot=current_request.snapshot,
                        allowed_statuses=allowed_statuses,
                        require_author_lineage=bool(
                            getattr(faculty, "requires_author_lineage", False)
                        ),
                    )
                    await self._record_prefetch_presentation(
                        faculty=faculty,
                        request=current_request,
                        result=validated,
                        entry=entry,
                    )
                    return validated
                except _InteriorTechnicalError as exc:
                    if exc.code != "invalid_role_result":
                        raise
                    structural_failure_code = exc.code

            assert structural_failure_code is not None
            if entry.correction_attempted:
                raise _InteriorTechnicalError(
                    "invalid_role_result_after_correction",
                    snapshot=current_request.snapshot,
                )
            entry.correction_attempted = True
            self._metrics["correction_attempt"] += 1
            corrected_request = current_request.model_copy(
                update={
                    "correction_ordinal": 1,
                    "correction_failure_code": structural_failure_code,
                }
            )
            try:
                corrected_raw = await _resolve(method(corrected_request))
            except _RoleResultContractError as correction_exc:
                raise _InteriorTechnicalError(
                    "invalid_role_result_after_correction",
                    snapshot=current_request.snapshot,
                ) from correction_exc
            except Exception as correction_exc:
                raise _InteriorTechnicalError(
                    "role_correction_unavailable",
                    snapshot=current_request.snapshot,
                ) from correction_exc
            try:
                validated = self._validate_role_result(
                    corrected_raw,
                    snapshot=current_request.snapshot,
                    allowed_statuses=allowed_statuses,
                    require_author_lineage=bool(getattr(faculty, "requires_author_lineage", False)),
                )
                await self._record_prefetch_presentation(
                    faculty=faculty,
                    request=corrected_request,
                    result=validated,
                    entry=entry,
                )
                return validated
            except _InteriorTechnicalError as correction_error:
                if correction_error.code == "invalid_role_result":
                    raise _InteriorTechnicalError(
                        "invalid_role_result_after_correction",
                        snapshot=current_request.snapshot,
                    ) from correction_error
                raise

        result = await invoke(
            request,
            allowed_statuses={*final_statuses, "recall_request"},
        )
        if result.status != "recall_request":
            private_self = _InstantPrivateSelf(
                summary=result.summary,
                attended_source_refs=result.attended_source_refs,
            )
            return (
                result,
                snapshot,
                _PrivateSelfLineage(
                    relation="single_pass",
                    initial_private_self=private_self,
                    initial_snapshot_id=snapshot.snapshot_id,
                    initial_snapshot_hash=snapshot.snapshot_hash,
                    initial_author_lineage=result.author_lineage,
                    final_private_self=private_self,
                    final_snapshot_id=snapshot.snapshot_id,
                    final_snapshot_hash=snapshot.snapshot_hash,
                    final_author_lineage=result.author_lineage,
                ),
            )
        if entry.recall_attempted:
            raise _InteriorTechnicalError("repeated_recall_request", snapshot=snapshot)
        initial_snapshot = snapshot
        initial_private_self = _InstantPrivateSelf(
            summary=result.summary,
            attended_source_refs=result.attended_source_refs,
        )
        entry.recall_attempted = True
        self._metrics["recall_attempt"] += 1
        snapshot = await self._recall_once(
            request=request,
            query=result.recall_query or "",
            snapshot=snapshot,
        )
        entry.snapshot = snapshot
        final_request = request.model_copy(update={"snapshot": snapshot, "recall_completed": True})
        final = await invoke(
            final_request,
            allowed_statuses={*final_statuses, "recall_request"},
        )
        if final.status == "recall_request":
            raise _InteriorTechnicalError("repeated_recall_request", snapshot=snapshot)
        recalled_refs = set(snapshot.source_refs) - set(initial_snapshot.source_refs)
        if recalled_refs.intersection(final.attended_source_refs):
            self._metrics["recall_adopted"] += 1
            self._metrics["recall_reintegrated"] += 1
        final_private_self = _InstantPrivateSelf(
            summary=final.summary,
            attended_source_refs=final.attended_source_refs,
        )
        return (
            final,
            snapshot,
            _PrivateSelfLineage(
                relation="selective_recall",
                initial_private_self=initial_private_self,
                initial_snapshot_id=initial_snapshot.snapshot_id,
                initial_snapshot_hash=initial_snapshot.snapshot_hash,
                initial_author_lineage=result.author_lineage,
                recall_query=result.recall_query,
                final_private_self=final_private_self,
                final_snapshot_id=snapshot.snapshot_id,
                final_snapshot_hash=snapshot.snapshot_hash,
                final_author_lineage=final.author_lineage,
                final_parent_model_call_id=(
                    result.author_lineage.model_call_id
                    if result.author_lineage is not None
                    else None
                ),
            ),
        )

    async def _record_prefetch_presentation(
        self,
        *,
        faculty: object,
        request: _InteriorRoleRequest,
        result: _InteriorRoleResult,
        entry: _TurnCacheEntry,
    ) -> None:
        trace_json = request.snapshot.prefetch_trace_json
        author = result.author_lineage
        if (
            trace_json is None
            or author is None
            or "automatic_prefetch" not in request.snapshot.materials
        ):
            return
        try:
            trace_value = json.loads(trace_json)
            raw_audit = trace_value.get("audit") if isinstance(trace_value, dict) else None
            audit = RecallAuditTrace.model_validate_json(
                json.dumps(
                    raw_audit,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            if audit.mode != "prefetch":
                raise ValueError("automatic candidate trace is not prefetch")
            phase_resolver = getattr(faculty, "prefetch_presentation_phase", None)
            if callable(phase_resolver):
                phase = phase_resolver(request)
            else:
                phase = "recall_followup" if request.recall_completed else "delegated_initial"
            presentation = PrefetchPresentationAudit(
                phase=phase,
                model_call_id=author.model_call_id,
                trace=audit,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise _InteriorTechnicalError(
                "invalid_prefetch_presentation",
                snapshot=request.snapshot,
            ) from exc
        presentations = entry.presented_prefetch_traces
        if presentations is None:
            presentations = []
            entry.presented_prefetch_traces = presentations
        if presentation not in presentations:
            if len(presentations) >= 4:
                raise _InteriorTechnicalError(
                    "prefetch_presentation_budget_exceeded",
                    snapshot=request.snapshot,
                )
            presentations.append(presentation)
        recorder = getattr(self._recall, "record_prefetch_presentation", None)
        if callable(recorder):
            try:
                await _resolve(
                    recorder(
                        phase=presentation.phase,
                        model_call_id=presentation.model_call_id,
                        trace_json=trace_json,
                    )
                )
            except Exception as exc:
                raise _InteriorTechnicalError(
                    "prefetch_presentation_audit_failed",
                    snapshot=request.snapshot,
                ) from exc

    @staticmethod
    def _validate_role_result(
        raw: object,
        *,
        snapshot: InnerLifeSnapshot,
        allowed_statuses: set[str],
        require_author_lineage: bool = False,
    ) -> _InteriorRoleResult:
        try:
            result = _InteriorRoleResult.model_validate(raw)
        except (ValidationError, TypeError, ValueError) as exc:
            import logging

            logging.getLogger(__name__).warning(
                "role result wire invalid purpose=%s detail=%s",
                getattr(snapshot, "actor_ref", "?"),
                str(exc)[:400],
            )
            raise _InteriorTechnicalError("invalid_role_result", snapshot=snapshot) from exc
        if result.status not in allowed_statuses:
            raise _InteriorTechnicalError("invalid_role_result", snapshot=snapshot)
        if require_author_lineage and result.author_lineage is None:
            raise _InteriorTechnicalError("invalid_role_result", snapshot=snapshot)
        if set(result.attended_source_refs) - set(snapshot.source_refs):
            import logging

            logging.getLogger(__name__).warning(
                "role result attended refs unpinned extra=%s",
                sorted(set(result.attended_source_refs) - set(snapshot.source_refs))[:5],
            )
            raise _InteriorTechnicalError("invalid_role_result", snapshot=snapshot)
        return result

    async def _submit_proposals(
        self,
        *,
        turn_id: str,
        subject: InteriorStimulus | InteriorOpportunity,
        snapshot: InnerLifeSnapshot,
        proposals: tuple[dict[str, Any], ...],
        author_lineage: object | None,
        private_self_lineage: _PrivateSelfLineage,
        decision_material: _InteriorRoleResult,
    ) -> tuple[str, ...]:
        if not proposals:
            return ()
        if self._authority is None:
            raise _InteriorTechnicalError("authority_unavailable", snapshot=snapshot)
        request = _AuthorityRequest(
            inner_turn_id=turn_id,
            world_id=snapshot.world_id,
            actor_ref=snapshot.actor_ref,
            purpose=subject.purpose,
            subject_ref=(
                subject.stimulus_ref
                if isinstance(subject, InteriorStimulus)
                else subject.opportunity_ref
            ),
            trigger_ref=subject.trigger_ref,
            subject_source_refs=subject.source_refs,
            cursor=snapshot.cursor,
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.snapshot_hash,
            capability_manifest=subject.capability_manifest,
            author_lineage=author_lineage,
            private_self_lineage_hash="sha256:"
            + _digest(private_self_lineage.model_dump(mode="json")),
            decision_hash="sha256:" + _digest(decision_material.model_dump(mode="json")),
            proposals=proposals,
        )
        try:
            raw_refs = await _resolve(self._authority.submit(request))
            if not isinstance(raw_refs, (tuple, list)):
                raise TypeError("authority result must be a sequence of refs")
            refs = tuple(raw_refs)
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "authority submission failed purpose=%s type=%s detail=%s",
                subject.purpose,
                type(exc).__name__,
                str(exc)[:300],
                exc_info=True,
            )
            raise _InteriorTechnicalError("authority_submission_failed", snapshot=snapshot) from exc
        if len(refs) != len(set(refs)) or any(
            not isinstance(item, str) or not item for item in refs
        ):
            raise _InteriorTechnicalError("invalid_authority_result", snapshot=snapshot)
        self._metrics["typed_proposal_submitted"] += len(refs)
        return refs

    @staticmethod
    def _failed_transition(
        stimulus: InteriorStimulus,
        *,
        turn_id: str,
        error: _InteriorTechnicalError,
    ) -> InnerTransition:
        snapshot = error.snapshot
        return InnerTransition(
            inner_turn_id=turn_id,
            stimulus_ref=stimulus.stimulus_ref,
            actor_ref=stimulus.actor_ref,
            cursor=stimulus.cursor,
            snapshot_id=snapshot.snapshot_id if snapshot else None,
            snapshot_hash=snapshot.snapshot_hash if snapshot else None,
            status="technical_failure",
            failure_code=error.code,
        )

    @staticmethod
    def _failed_decision(
        opportunity: InteriorOpportunity,
        *,
        turn_id: str,
        error: _InteriorTechnicalError,
    ) -> InnerDecision:
        snapshot = error.snapshot
        return InnerDecision(
            inner_turn_id=turn_id,
            opportunity_ref=opportunity.opportunity_ref,
            actor_ref=opportunity.actor_ref,
            cursor=opportunity.cursor,
            snapshot_id=snapshot.snapshot_id if snapshot else None,
            snapshot_hash=snapshot.snapshot_hash if snapshot else None,
            status="technical_failure",
            failure_code=error.code,
        )

    def _trim_cache(self) -> None:
        while len(self._cache) > _CACHE_LIMIT:
            stale_turn_id, _ = self._cache.popitem(last=False)
            self._locks.pop(stale_turn_id, None)
        while len(self._snapshot_cache) > _CACHE_LIMIT:
            stale_snapshot_key, _ = self._snapshot_cache.popitem(last=False)
            self._snapshot_locks.pop(stale_snapshot_key, None)

    def _record_snapshot_health(self, snapshot: InnerLifeSnapshot) -> None:
        """Retain only aggregate source-closure evidence for the latest snapshot."""

        inventory_refs = {item.source_ref for item in snapshot.source_inventory}
        truncation_reason = (
            snapshot.truncation.reason
            if snapshot.truncation.availability == "unavailable"
            else None
        )
        state: dict[str, dict[str, object]] = {}
        for facet in snapshot.facet_views:
            content = facet.content
            material_keys = content.get("material_keys")
            item_count = len(material_keys) if isinstance(material_keys, list) else len(content)
            state[facet.name] = {
                "availability": facet.availability,
                "item_count": item_count,
                "source_count": len(facet.source_refs),
                "source_closed_count": sum(
                    source_ref in inventory_refs for source_ref in facet.source_refs
                ),
                "truncation_reason": truncation_reason,
            }
        self._last_snapshot_faculty_state = state

    def _record_terminal(
        self,
        phase: str,
        status: str,
        failure_code: str | None,
        *,
        purpose: str,
        inner_turn_id: str,
        cursor: ProjectionCursor,
        snapshot_hash: str | None,
    ) -> None:
        self._metrics[f"{phase}:{status}"] += 1
        self._metrics[f"purpose:{purpose}"] += 1
        self._last_turn_metadata = {
            "inner_turn_id": inner_turn_id,
            "phase": phase,
            "purpose": purpose,
            "cursor": cursor.model_dump(mode="json"),
            "snapshot_hash": snapshot_hash,
            "terminal_status": status,
        }
        self._last_terminal_status = status
        self._last_failure_code = failure_code


__all__ = ["CharacterInterior"]
