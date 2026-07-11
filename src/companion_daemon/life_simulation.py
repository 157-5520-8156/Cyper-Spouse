"""Deterministic, rule-only life outcomes for the world ledger."""
from __future__ import annotations

from datetime import datetime
from typing import Any


class LifeSimulation:
    """A deep rule module: activity + world state -> append-only event specs."""

    def outcomes_for(self, state: dict[str, Any], activity: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        outcome_id = f"outcome:{activity['activity_id']}"
        if outcome_id in state.get("outcomes", {}) or state["needs"].get("energy", 0) < 15:
            return []
        starts = datetime.fromisoformat(str(activity["starts_at"]))
        template = self._template_for(activity, starts.hour, state)
        if template is None:
            return []
        npc_id, content, goal_id, energy_cost = template
        payload = {"outcome_id": outcome_id, "activity_id": activity["activity_id"], "npc_id": npc_id, "content": content, "rule_version": "life-sim-v1"}
        events: list[tuple[str, dict[str, Any]]] = [
            ("LifeOutcomeProposed", payload),
            ("LifeOutcomeCommitted", payload),
            ("NeedChanged", {"need": "energy", "delta": -energy_cost}),
            ("ExperienceCommitted", {"experience_id": outcome_id, "action_id": None, "content": content, "source_outcome_id": outcome_id}),
        ]
        if npc_id:
            events.append(("NpcRelationshipChanged", {"entity_id": npc_id, "dimension": "closeness", "delta": 2}))
        if goal_id:
            events.append(("GoalProgressed", {"goal_id": goal_id, "activity_id": activity["activity_id"], "delta": 1}))
        return events

    def _template_for(self, activity: dict[str, Any], hour: int, state: dict[str, Any]) -> tuple[str | None, str, str | None, int] | None:
        title = str(activity["title"])
        entities = state.get("entities", {})
        if "图书馆" in title and self._available(entities, "literature-fan", hour):
            return ("literature-fan", "在图书馆和范予安核对了读书会的书单。", "literature-reading", 6)
        if "社团" in title and self._available(entities, "photography-zhou", hour):
            return ("photography-zhou", "整理了摄影社活动要用的照片。", "photo-portfolio", 8)
        if "散步" in title and self._available(entities, "roommate-lin", hour):
            return ("roommate-lin", "和林晚在校园里走了一小段，聊了晚饭。", None, 5)
        if "课程" in title:
            return (None, "整理完了今天的课程笔记。", "course-notes", 7)
        return None

    @staticmethod
    def _available(entities: dict[str, Any], npc_id: str, hour: int) -> bool:
        npc = entities.get(npc_id)
        if not npc:
            return False
        for window in npc.get("availability", []):
            start, end = str(window).split("-", 1)
            if int(start[:2]) <= hour < int(end[:2]):
                return True
        return False
