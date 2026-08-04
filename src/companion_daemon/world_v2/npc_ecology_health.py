"""Read-only NPC ecology observability from immutable World V2 authority.

The NPC ecology intentionally has no mutable metrics side channel.  This
compiler distinguishes model attempts from calls that formed a completed
semantic decision. Token/cost usage remains unknown until the producer records
provider-attested usage; health must not estimate it from response text or
deployment configuration.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
import json
from typing import Any


_ACTOR_PROPOSAL_KIND = "npc_ecology"
_WORLD_PROPOSAL_KIND = "npc_ecology_world_adjudication"
_TECHNICAL_FAILURE_PREFIX = "life-ecology:technical_failure.npc_ecology."
_CURRENT_POLICY = "policy:npc-ecology.2"


def _rate(*, numerator: int, denominator: int) -> dict[str, object]:
    if denominator == 0:
        return {"status": "not_measured", "sample_count": 0, "value_bp": None}
    return {
        "status": "measured",
        "sample_count": denominator,
        "value_bp": round(numerator * 10_000 / denominator),
    }


def _unknown_usage() -> dict[str, object]:
    return {
        "status": "unknown",
        "reason": "npc_ecology_provider_usage_not_durably_recorded",
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost": None,
    }


def _exact_events(*, projection: object, ledger: object) -> tuple[object, ...]:
    """Read exact interesting events and reject mismatched lookup results."""

    interesting = frozenset(
        {
            "ProposalRecorded",
            "ModelResultRecorded",
            "TriggerProcessCompleted",
            "NpcStatusChanged",
            "NpcStateChanged",
        }
    )
    logical_time = getattr(projection, "logical_time", None)
    recent_reader = getattr(ledger, "recent_events_by_type", None)
    if isinstance(logical_time, datetime) and callable(recent_reader):
        return tuple(
            recent_reader(
                event_types=interesting,
                since=logical_time - timedelta(hours=24),
                limit=16_384,
            )
        )

    events: list[object] = []
    for authority in getattr(projection, "committed_world_event_refs", ()):
        if authority.event_type not in interesting:
            continue
        located = ledger.lookup_event_commit(authority.event_id)
        if located is None:
            continue
        event = located[0]
        if (
            event.event_type != authority.event_type
            or event.payload_hash != authority.payload_hash
            or event.logical_time != authority.logical_time
        ):
            continue
        events.append(event)
    return tuple(events)


def _payload(event: object) -> dict[str, Any] | None:
    try:
        value = event.payload()
    except (AttributeError, TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _in_last_day(event: object, *, logical_time: datetime | None) -> bool:
    return logical_time is not None and event.logical_time >= logical_time - timedelta(hours=24)


def _participant_refs(*, npc_ref: str, view: object | None, npc: object) -> frozenset[str]:
    values = {npc_ref}
    provisional = getattr(view, "provisional_entity_ref", None)
    if provisional is None:
        edge = getattr(npc, "promotion_edge", None)
        provisional = getattr(edge, "provisional_entity_ref", None)
    if isinstance(provisional, str):
        values.add(provisional)
    return frozenset(values)


def npc_ecology_health_snapshot(
    *,
    projection: object,
    ledger: object,
    identity_views: tuple[object, ...],
) -> dict[str, object]:
    """Compile truthful NPC ecology identity, recurrence, call and failure health."""

    logical_time = getattr(projection, "logical_time", None)
    if not isinstance(logical_time, datetime):
        logical_time = None
    events = _exact_events(projection=projection, ledger=ledger)

    npc_by_ref = {f"npc:{item.npc_id}": item for item in getattr(projection, "npcs", ())}
    dynamic_refs = {
        npc_ref
        for npc_ref, item in npc_by_ref.items()
        if getattr(item, "source_event_ref", None) is not None
    }
    views_by_ref = {item.npc_ref: item for item in identity_views}
    closed_dynamic_refs = dynamic_refs & views_by_ref.keys()
    closure_failure_refs = tuple(sorted(dynamic_refs - closed_dynamic_refs))

    actor_records: list[tuple[object, dict[str, Any], str]] = []
    actor_by_event: dict[str, str] = {}
    actor_by_wake: dict[str, str] = {}
    world_records: list[tuple[object, dict[str, Any], str]] = []
    technical_failures: list[tuple[object, str, str | None]] = []
    lifecycle_events: list[object] = []
    unreadable_audit_count = 0
    raw_model_attempts: list[tuple[object, dict[str, Any]]] = []

    for event in events:
        payload = _payload(event)
        if payload is None:
            unreadable_audit_count += 1
            continue
        if event.event_type in {"NpcStatusChanged", "NpcStateChanged"}:
            lifecycle_events.append(event)
            continue
        if event.event_type == "ModelResultRecorded":
            audit_json = payload.get("audit_json")
            try:
                audit = json.loads(audit_json) if isinstance(audit_json, str) else None
            except (TypeError, ValueError):
                audit = None
            route = audit.get("route") if isinstance(audit, dict) else None
            reason_code = route.get("reason_code") if isinstance(route, dict) else None
            if reason_code in {"npc_ecology_actor", "npc_ecology_world"}:
                raw_model_attempts.append((event, audit))
            elif isinstance(reason_code, str) and reason_code.startswith("npc_ecology_"):
                unreadable_audit_count += 1
            continue
        if event.event_type == "ProposalRecorded":
            kind = payload.get("proposal_kind")
            decision = payload.get("decision_payload")
            if not isinstance(decision, dict):
                if kind in {_ACTOR_PROPOSAL_KIND, _WORLD_PROPOSAL_KIND}:
                    unreadable_audit_count += 1
                continue
            if kind == _ACTOR_PROPOSAL_KIND:
                npc_ref = decision.get("npc_ref")
                if not isinstance(npc_ref, str) or not npc_ref.startswith("npc:"):
                    unreadable_audit_count += 1
                    continue
                actor_records.append((event, decision, npc_ref))
                actor_by_event[event.event_id] = npc_ref
                wake_ref = payload.get("trigger_id")
                if isinstance(wake_ref, str):
                    actor_by_wake[wake_ref] = npc_ref
            elif kind == _WORLD_PROPOSAL_KIND:
                actor_ref = payload.get("actor_decision_event_ref")
                npc_ref = actor_by_event.get(actor_ref) if isinstance(actor_ref, str) else None
                if npc_ref is None:
                    # Actor records can appear later in a synthetic fixture;
                    # resolve the binding in a second pass below.
                    npc_ref = ""
                world_records.append((event, decision, npc_ref))
            continue
        outcome = payload.get("runtime_outcome_ref")
        if (
            event.event_type == "TriggerProcessCompleted"
            and isinstance(outcome, str)
            and outcome.startswith(_TECHNICAL_FAILURE_PREFIX)
        ):
            wake_ref = getattr(event, "causation_id", None)
            npc_ref = actor_by_wake.get(wake_ref) if isinstance(wake_ref, str) else None
            technical_failures.append((event, outcome.removeprefix("life-ecology:"), npc_ref))

    # Event order normally puts the actor decision first.  Exact event refs
    # still make attribution order-independent for replay fixtures and imports.
    if any(not npc_ref for _, _, npc_ref in world_records):
        repaired: list[tuple[object, dict[str, Any], str]] = []
        for event, decision, npc_ref in world_records:
            payload = _payload(event) or {}
            actor_ref = payload.get("actor_decision_event_ref")
            repaired.append(
                (
                    event,
                    decision,
                    npc_ref
                    or (actor_by_event.get(actor_ref, "") if isinstance(actor_ref, str) else ""),
                )
            )
        world_records = repaired

    # Likewise, a technical completion may precede the actor record in a
    # synthetic imported order.  The wake is an immutable causation edge.
    technical_failures = [
        (
            event,
            code,
            npc_ref
            or (
                actor_by_wake.get(event.causation_id)
                if isinstance(getattr(event, "causation_id", None), str)
                else None
            ),
        )
        for event, code, npc_ref in technical_failures
    ]
    model_attempts: list[tuple[object, str, bool, str | None]] = []
    successful_statuses = {"proposal_validated", "main_invalid_recovered"}
    for event, audit in raw_model_attempts:
        reason_code = audit["route"]["reason_code"]
        role = "actor" if reason_code == "npc_ecology_actor" else "world"
        status = audit.get("status")
        failed = status not in successful_statuses
        npc_ref = (
            event.actor
            if role == "actor" and isinstance(event.actor, str) and event.actor.startswith("npc:")
            else actor_by_wake.get(event.causation_id)
            if isinstance(getattr(event, "causation_id", None), str)
            else None
        )
        model_attempts.append((event, role, failed, npc_ref))

    per_npc: list[dict[str, object]] = []
    total_scene_reappearances = 0
    total_lifecycle_reactivations = 0
    for npc_ref, npc in sorted(npc_by_ref.items()):
        view = views_by_ref.get(npc_ref)
        participant_refs = _participant_refs(npc_ref=npc_ref, view=view, npc=npc)
        settled_scenes = tuple(
            item
            for item in getattr(projection, "world_occurrences", ())
            if item.status == "settled" and participant_refs.intersection(item.participant_refs)
        )
        scene_reappearance_count = max(0, len(settled_scenes) - 1)
        lifecycle_reactivation_events = []
        for event in lifecycle_events:
            payload = _payload(event) or {}
            before = payload.get("npc_before")
            after = payload.get("npc_after")
            if (
                isinstance(before, dict)
                and isinstance(after, dict)
                and f"npc:{after.get('npc_id')}" == npc_ref
                and before.get("status") in {"dormant", "departed"}
                and after.get("status") == "active"
            ):
                lifecycle_reactivation_events.append(event)
        npc_actor = tuple(item for item in actor_records if item[2] == npc_ref)
        npc_world = tuple(item for item in world_records if item[2] == npc_ref)
        npc_failures = tuple(item for item in technical_failures if item[2] == npc_ref)
        npc_attempts = tuple(item for item in model_attempts if item[3] == npc_ref)
        total_scene_reappearances += scene_reappearance_count
        total_lifecycle_reactivations += len(lifecycle_reactivation_events)
        per_npc.append(
            {
                "npc_ref": npc_ref,
                "dynamic": npc_ref in dynamic_refs,
                "promotion_closed": npc_ref not in dynamic_refs or view is not None,
                "actor_completed_call_count": len(npc_actor),
                "actor_completed_call_count_24h": sum(
                    _in_last_day(item[0], logical_time=logical_time) for item in npc_actor
                ),
                "world_completed_call_count": len(npc_world),
                "world_completed_call_count_24h": sum(
                    _in_last_day(item[0], logical_time=logical_time) for item in npc_world
                ),
                "actor_no_op_count": sum(item[1].get("decision") == "no_op" for item in npc_actor),
                "world_no_op_count": sum(item[1].get("decision") == "no_op" for item in npc_world),
                "settled_scene_count": len(settled_scenes),
                "scene_reappearance_count": scene_reappearance_count,
                "lifecycle_reactivation_count": len(lifecycle_reactivation_events),
                "technical_failure_count": len(npc_failures),
                "technical_failure_count_24h": sum(
                    _in_last_day(item[0], logical_time=logical_time) for item in npc_failures
                ),
                "technical_failure_codes": dict(
                    sorted(Counter(item[1] for item in npc_failures).items())
                ),
                "actor_model_attempt_count": sum(item[1] == "actor" for item in npc_attempts),
                "world_model_attempt_count": sum(item[1] == "world" for item in npc_attempts),
                "failed_model_attempt_count": sum(item[2] for item in npc_attempts),
            }
        )

    actor_no_ops = sum(item[1].get("decision") == "no_op" for item in actor_records)
    world_no_ops = sum(item[1].get("decision") == "no_op" for item in world_records)
    unattributed_failures = sum(item[2] is None for item in technical_failures)
    actor_attempts = tuple(item for item in model_attempts if item[1] == "actor")
    world_attempts = tuple(item for item in model_attempts if item[1] == "world")

    def success_rates(role: str) -> tuple[dict[str, object], dict[str, object]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event, audit in raw_model_attempts:
            route = audit.get("route")
            if not isinstance(route, dict) or route.get("reason_code") != f"npc_ecology_{role}":
                continue
            policy_version = audit.get("model_version") or audit.get(
                "attempted_model_version"
            )
            if policy_version != _CURRENT_POLICY:
                continue
            if not _in_last_day(event, logical_time=logical_time):
                continue
            attempt_id = audit.get("attempt_id")
            if not isinstance(attempt_id, str):
                attempt_id = str(audit.get("model_call_id") or event.event_id)
            grouped.setdefault(attempt_id, []).append(audit)
        first_pass = sum(
            bool(audits) and audits[0].get("status") == "proposal_validated"
            for audits in grouped.values()
        )
        eventually_valid = sum(
            any(audit.get("status") in successful_statuses for audit in audits)
            for audits in grouped.values()
        )
        return (
            _rate(numerator=eventually_valid, denominator=len(grouped)),
            _rate(numerator=first_pass, denominator=len(grouped)),
        )

    actor_success_rate, actor_first_pass_rate = success_rates("actor")
    world_success_rate, world_first_pass_rate = success_rates("world")
    warning_reasons = []
    for name, rate in (
        ("actor_consideration_success_below_90_percent", actor_success_rate),
        ("actor_first_attempt_success_below_90_percent", actor_first_pass_rate),
        ("world_consideration_success_below_90_percent", world_success_rate),
        ("world_first_attempt_success_below_90_percent", world_first_pass_rate),
    ):
        value = rate.get("value_bp")
        if isinstance(value, int) and value < 9_000:
            warning_reasons.append(name)
    return {
        "dynamic_count": len(dynamic_refs),
        "promotion_closed_count": len(closed_dynamic_refs),
        "promotion_closure_failure_count": len(closure_failure_refs),
        "promotion_closure_failure_refs": list(closure_failure_refs[:16]),
        "actor_completed_call_count": len(actor_records),
        "actor_completed_call_count_24h": sum(
            _in_last_day(item[0], logical_time=logical_time) for item in actor_records
        ),
        "world_completed_call_count": len(world_records),
        "world_completed_call_count_24h": sum(
            _in_last_day(item[0], logical_time=logical_time) for item in world_records
        ),
        "actor_no_op_count": actor_no_ops,
        "actor_no_op_rate": _rate(numerator=actor_no_ops, denominator=len(actor_records)),
        "world_no_op_count": world_no_ops,
        "world_no_op_rate": _rate(numerator=world_no_ops, denominator=len(world_records)),
        "actor_model_attempt_count": len(actor_attempts),
        "actor_model_attempt_count_24h": sum(
            _in_last_day(item[0], logical_time=logical_time) for item in actor_attempts
        ),
        "actor_failed_model_attempt_count": sum(item[2] for item in actor_attempts),
        "actor_consideration_success_rate_24h": actor_success_rate,
        "actor_first_attempt_success_rate_24h": actor_first_pass_rate,
        "world_model_attempt_count": len(world_attempts),
        "world_model_attempt_count_24h": sum(
            _in_last_day(item[0], logical_time=logical_time) for item in world_attempts
        ),
        "world_failed_model_attempt_count": sum(item[2] for item in world_attempts),
        "world_consideration_success_rate_24h": world_success_rate,
        "world_first_attempt_success_rate_24h": world_first_pass_rate,
        "scene_reappearance_count": total_scene_reappearances,
        "reappeared_npc_count": sum(int(item["scene_reappearance_count"] > 0) for item in per_npc),
        "lifecycle_reactivation_count": total_lifecycle_reactivations,
        "technical_failure_count": len(technical_failures),
        "technical_failure_count_24h": sum(
            _in_last_day(item[0], logical_time=logical_time) for item in technical_failures
        ),
        "unattributed_technical_failure_count": unattributed_failures,
        "technical_failure_codes": dict(
            sorted(Counter(item[1] for item in technical_failures).items())
        ),
        "audit_read_failure_count": unreadable_audit_count,
        "warning": bool(warning_reasons),
        "warning_reasons": warning_reasons,
        "provider_attempt_evidence": "model_result_recorded",
        "success_rate_policy_version": _CURRENT_POLICY,
        "actor_usage": _unknown_usage(),
        "world_usage": _unknown_usage(),
        "per_npc": per_npc,
    }


__all__ = ["npc_ecology_health_snapshot"]
