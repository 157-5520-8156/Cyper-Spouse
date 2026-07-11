"""Deterministic rule module for controlled virtual-life evolution."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any


class LifeSimulation:
    """One deep seam: completed activities plus state become verified event specs."""

    def advance(self, state: dict[str, Any], completed: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
        working = deepcopy(state)
        emitted: list[tuple[str, dict[str, Any]]] = []
        for activity in completed:
            events = self._outcome(working, activity)
            emitted.extend(events)
            self._apply(working, events)
        return emitted

    def _outcome(self, state: dict[str, Any], activity: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        spec = self._spec(activity, state)
        if spec is None:
            return []
        npc_id, content, goal_id, cost = spec
        outcome_id = f"outcome:{activity['activity_id']}"
        payload = {"outcome_id": outcome_id, "activity_id": activity["activity_id"], "npc_id": npc_id, "content": content, "rule_version": "life-sim-v2"}
        events = [("LifeOutcomeProposed", payload), ("LifeOutcomeValidated", {**payload, "validation": "template/location/availability/resources/frequency"}), ("LifeOutcomeCommitted", payload), ("NeedChanged", {"need": "energy", "delta": -cost}), ("ExperienceCommitted", {"experience_id": outcome_id, "action_id": None, "content": content, "source_outcome_id": outcome_id})]
        if npc_id:
            events.append(("NpcRelationshipChanged", {"entity_id": npc_id, "dimension": "closeness", "delta": 2}))
        if goal_id:
            events.append(("GoalProgressed", {"goal_id": goal_id, "activity_id": activity["activity_id"], "delta": 1}))
        return events

    def _spec(self, activity: dict[str, Any], state: dict[str, Any]) -> tuple[str | None, str, str | None, int] | None:
        if activity.get("entity_id") != "zhizhi" or f"outcome:{activity['activity_id']}" in state.get("outcomes", {}) or state["needs"].get("energy", 0) < 15:
            return None
        template = str(activity.get("template_id") or "")
        expected_locations = {"literature_reading": "华东师范大学", "course_notes": "华东师范大学", "photo_portfolio": "上海", "campus_walk": "华东师范大学"}
        if activity.get("location") != expected_locations.get(template):
            return None
        specs = {"literature_reading": ("literature-fan", "在图书馆和范予安核对了读书会的书单。", "literature-reading", 6), "course_notes": (None, "整理完了今天的课程笔记。", "course-notes", 7), "photo_portfolio": ("photography-zhou", "整理了摄影社活动要用的照片。", "photo-portfolio", 8), "campus_walk": ("roommate-lin", "和林晚在校园里走了一小段，聊了晚饭。", None, 5)}
        spec = specs.get(template)
        if not spec:
            return None
        npc_id, _, goal_id, cost = spec
        goal = state.get("goals", {}).get(goal_id) if goal_id else None
        if goal_id and (not goal or goal.get("status") != "active"):
            return None
        hour = datetime.fromisoformat(str(activity["starts_at"])).hour
        if npc_id and not self._available(state.get("entities", {}), npc_id, hour):
            return None
        day = str(activity["starts_at"])[:10]
        if npc_id and sum(1 for item in state.get("outcomes", {}).values() if item.get("npc_id") == npc_id and str(item.get("activity_id", ""))[:10] == day) >= 1:
            return None
        return spec

    @staticmethod
    def _available(entities: dict[str, Any], npc_id: str, hour: int) -> bool:
        for window in entities.get(npc_id, {}).get("availability", []):
            start, end = str(window).split("-", 1)
            if int(start[:2]) <= hour < int(end[:2]):
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
