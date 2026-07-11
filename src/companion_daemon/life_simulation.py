"""Deterministic rules for turning completed activities into world facts."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any


class LifeSimulation:
    """The one rule seam for automatic and model-proposed life outcomes."""

    RULE_VERSION = "life-sim-v3"

    def advance(self, state: dict[str, Any], completed: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        working = deepcopy(state)
        emitted: list[tuple[str, dict[str, Any]]] = []
        for activity in completed:
            events = self.events_for_activity(working, activity)
            emitted.extend(events)
            self._apply(working, events)
        return emitted

    def events_for_activity(self, state: dict[str, Any], activity: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        valid, reason, spec = self._validate(state, activity, require_completed=False)
        if not valid or spec is None:
            return []
        outcome_id = f"outcome:{activity['activity_id']}"
        return self._events(outcome_id, activity, spec, validation=reason)

    def events_for_candidate(self, state: dict[str, Any], candidate: dict[str, Any]) -> tuple[bool, str, list[tuple[str, dict[str, Any]]]]:
        """Accept a model candidate only as the result of one completed activity."""
        activity_id = str(candidate.get("activity_id") or "")
        activity = state.get("agenda", {}).get(activity_id)
        planned_events: list[tuple[str, dict[str, Any]]] = []
        if activity:
            if activity.get("status") != "completed":
                return False, "completed_activity_required", []
            for key in ("entity_id", "template_id", "location", "starts_at", "ends_at"):
                if str(candidate.get(key) or "") != str(activity.get(key) or ""):
                    return False, f"candidate_{key}_does_not_match_activity", []
        else:
            activity = {key: candidate.get(key) for key in ("activity_id", "entity_id", "template_id", "location", "starts_at", "ends_at")}
            activity["title"] = str(candidate.get("title") or candidate.get("template_id") or "world activity")
            activity["status"] = "completed"
            for existing in state.get("agenda", {}).values():
                if existing.get("entity_id") == activity["entity_id"] and existing.get("status") in {"planned", "active", "completed"}:
                    if str(activity["starts_at"]) < str(existing.get("ends_at")) and str(existing.get("starts_at")) < str(activity["ends_at"]):
                        return False, "activity_conflicts_with_world_commitment", []
            planned_events = [("ActivityPlanned", {key: activity[key] for key in ("activity_id", "entity_id", "title", "template_id", "location", "starts_at", "ends_at")}), ("ActivityStarted", {"activity_id": activity_id}), ("ActivityCompleted", {"activity_id": activity_id})]
        valid, reason, spec = self._validate(state, activity, require_completed=True, candidate=candidate)
        if not valid or spec is None:
            return False, reason, []
        outcome_id = str(candidate["proposal_id"])
        return True, reason, [
            *planned_events,
            ("LifeOutcomeValidated", {"outcome_id": outcome_id, "activity_id": activity_id, "validation": reason, "rule_version": self.RULE_VERSION}),
            ("ModelProposalAccepted", {"proposal_id": outcome_id}),
            *self._events(outcome_id, activity, spec, validation=reason, proposed=False, validated=True, content=str(candidate["content"])),
        ]

    def validate_candidate(self, state: dict[str, Any], candidate: dict[str, Any]) -> tuple[bool, str]:
        accepted, reason, _ = self.events_for_candidate(state, candidate)
        return accepted, reason

    def _events(self, outcome_id: str, activity: dict[str, Any], spec: dict[str, Any], *, validation: str, proposed: bool = True, validated: bool = False, content: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        content = content or str(spec["content"])
        payload = {"outcome_id": outcome_id, "activity_id": activity["activity_id"], "npc_id": spec.get("npc_id"), "content": content, "rule_version": self.RULE_VERSION, "template_id": activity["template_id"], "location": activity["location"], "starts_at": activity["starts_at"], "ends_at": activity["ends_at"]}
        events: list[tuple[str, dict[str, Any]]] = []
        if proposed:
            events.append(("LifeOutcomeProposed", payload))
        if not validated:
            events.append(("LifeOutcomeValidated", {**payload, "validation": validation}))
        events.extend([("LifeOutcomeCommitted", payload), ("NeedChanged", {"need": "energy", "delta": -int(spec["energy_cost"])}), ("ExperienceCommitted", {"experience_id": outcome_id, "action_id": None, "content": content, "source_outcome_id": outcome_id})])
        if spec.get("npc_id"):
            events.append(("NpcRelationshipChanged", {"entity_id": spec["npc_id"], "dimension": "closeness", "delta": int(spec.get("relationship_delta", 2))}))
        if spec.get("goal_id"):
            events.append(("GoalProgressed", {"goal_id": spec["goal_id"], "activity_id": activity["activity_id"], "delta": 1}))
        return events

    def _validate(self, state: dict[str, Any], activity: dict[str, Any], *, require_completed: bool, candidate: dict[str, Any] | None = None) -> tuple[bool, str, dict[str, Any] | None]:
        if activity.get("entity_id") != "zhizhi" or not activity.get("activity_id"):
            return False, "unsupported_activity_entity", None
        if require_completed and activity.get("status") != "completed":
            return False, "completed_activity_required", None
        if f"outcome:{activity['activity_id']}" in state.get("outcomes", {}):
            return False, "activity_already_has_outcome", None
        template = str(activity.get("template_id") or "")
        specs = state.get("life_outcome_templates", {})
        spec = specs.get(template)
        if not isinstance(spec, dict):
            return False, "unregistered_outcome_template", None
        if str(activity.get("location") or "") != str(spec.get("location") or ""):
            return False, "template_location_mismatch", None
        if state.get("needs", {}).get("energy", 0) < int(spec.get("energy_cost", 0)):
            return False, "insufficient_energy", None
        starts_at, ends_at = datetime.fromisoformat(str(activity["starts_at"])), datetime.fromisoformat(str(activity["ends_at"]))
        if ends_at <= starts_at or ends_at > datetime.fromisoformat(str(state["clock"]["logical_at"])):
            return False, "activity_outside_logical_time", None
        npc_id = spec.get("npc_id")
        if candidate and candidate.get("npc_id") != npc_id:
            return False, "candidate_npc_does_not_match_template", None
        if npc_id and not self._available(state.get("entities", {}), str(npc_id), starts_at, ends_at):
            return False, "npc_unavailable", None
        day = starts_at.date().isoformat()
        if npc_id and any(item.get("npc_id") == npc_id and str(item.get("starts_at", ""))[:10] == day for item in state.get("outcomes", {}).values()):
            return False, "npc_daily_frequency_limit", None
        goal_id = spec.get("goal_id")
        if goal_id:
            goal = state.get("goals", {}).get(goal_id)
            if not goal or goal.get("status") != "active":
                return False, "goal_not_active", None
            if goal.get("deadline") and ends_at > datetime.fromisoformat(str(goal["deadline"])):
                return False, "goal_deadline_passed", None
        if candidate and len(str(candidate.get("content") or "")) > 160:
            return False, "content_too_long", None
        return True, "template/time/location/npc/resources/frequency/goal", spec

    @staticmethod
    def _available(entities: dict[str, Any], npc_id: str, starts_at: datetime, ends_at: datetime) -> bool:
        for window in entities.get(npc_id, {}).get("availability", []):
            start, end = str(window).split("-", 1)
            start_hour, end_hour = int(start[:2]), int(end[:2])
            if start_hour <= starts_at.hour and ends_at.hour <= end_hour and starts_at.date() == ends_at.date():
                return True
        return False

    @staticmethod
    def _apply(state: dict[str, Any], events: list[tuple[str, dict[str, Any]]]) -> None:
        for kind, payload in events:
            if kind == "NeedChanged":
                state["needs"][payload["need"]] += payload["delta"]
            elif kind == "LifeOutcomeCommitted":
                state.setdefault("outcomes", {})[payload["outcome_id"]] = payload
            elif kind == "GoalProgressed":
                goal = state["goals"][payload["goal_id"]]
                goal["progress"] += payload["delta"]
                if goal["progress"] >= goal["target"]:
                    goal["status"] = "completed"
