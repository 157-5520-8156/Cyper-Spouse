"""Deterministic, replay-safe rules for the companion's longer life rhythm.

This module only proposes plans and projections.  It never commits an
experience: a planned activity must still pass through the normal world
activity completion and outcome settlement path before it becomes lived fact.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class ActivityCandidateScore:
    """Auditable score for one seed-authorized activity template."""

    template_id: str
    eligible: bool
    score: int
    reasons: tuple[str, ...]
    rejected_reasons: tuple[str, ...]


class LifeEvolution:
    """Long-horizon planning and preference rules with no external I/O."""

    RULE_VERSION = "life-evolution-v1"
    MAX_WEEKLY_THEMES = 2
    MAX_WEEKLY_ACTIVITIES = 3
    ALLOWED_OBSERVATIONS = frozenset(
        {"weather", "ambient_noise", "light", "crowding", "temperature"}
    )
    ALLOWED_INFLUENCES = frozenset(
        {"user_vulnerability", "user_conflict", "user_support", "user_returned"}
    )

    def plan_week(
        self, state: dict[str, Any], *, week_start: datetime
    ) -> list[tuple[str, dict[str, Any]]]:
        """Materialize a sparse, deterministic week from seed-owned themes."""
        logical_now = self._at(state["clock"]["logical_at"])
        start = week_start - timedelta(days=week_start.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        week_id = start.date().isoformat()
        if week_id in state.get("weekly_plans", {}):
            return []

        themes = [
            item
            for item in state.get("weekly_themes", [])
            if isinstance(item, dict) and item.get("id") and item.get("template_id")
        ]
        selected_themes = sorted(
            themes,
            key=lambda item: (-int(item.get("priority", 0)), str(item["id"])),
        )[: self.MAX_WEEKLY_THEMES]

        candidates: list[tuple[datetime, datetime, dict[str, Any]]] = []
        for theme in selected_themes:
            weekdays = sorted({int(day) for day in theme.get("weekdays", []) if 0 <= int(day) <= 6})
            for weekday in weekdays:
                starts_at = (start + timedelta(days=weekday)).replace(
                    hour=int(theme.get("starts_hour", 18))
                )
                ends_at = starts_at + timedelta(
                    hours=max(1, min(12, int(theme.get("duration_hours", 1))))
                )
                if starts_at <= logical_now:
                    continue
                candidates.append((starts_at, ends_at, theme))

        candidates.sort(key=lambda item: (item[0], -int(item[2].get("priority", 0)), str(item[2]["id"])))
        occupied = [
            (self._at(item["starts_at"]), self._at(item["ends_at"]))
            for item in state.get("agenda", {}).values()
            if isinstance(item, dict)
            and item.get("status") in {"planned", "active"}
            and item.get("starts_at")
            and item.get("ends_at")
        ]
        planned: list[tuple[datetime, datetime, dict[str, Any]]] = []
        for starts_at, ends_at, theme in candidates:
            if len(planned) >= self.MAX_WEEKLY_ACTIVITIES:
                break
            if any(starts_at < occupied_end and occupied_start < ends_at for occupied_start, occupied_end in occupied):
                continue
            planned.append((starts_at, ends_at, theme))
            occupied.append((starts_at, ends_at))

        used_theme_ids = list(
            dict.fromkeys(str(theme["id"]) for _, _, theme in planned)
        )
        if not planned:
            return []
        events: list[tuple[str, dict[str, Any]]] = [
            (
                "WeeklyPlanCreated",
                {
                    "week_id": week_id,
                    "starts_at": start.isoformat(),
                    "ends_at": (start + timedelta(days=7)).isoformat(),
                    "theme_ids": used_theme_ids,
                    "status": "planned",
                    "rule_version": self.RULE_VERSION,
                },
            )
        ]
        theme_by_id = {str(item["id"]): item for item in selected_themes}
        activity_payloads = [
            {
                "activity_id": self._weekly_activity_id(
                    week_id, starts_at, str(theme["id"])
                ),
                "entity_id": "zhizhi",
                "title": str(theme["title"]),
                "template_id": str(theme["template_id"]),
                "location": str(theme.get("location") or ""),
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
                "attention_demand": int(theme.get("attention_demand", 35)),
                "interruptible": bool(theme.get("interruptible", True)),
                "fallback_templates": list(theme.get("fallback_templates", [])),
                "plan_source": f"weekly:{week_id}:{theme['id']}",
            }
            for starts_at, ends_at, theme in planned
        ]
        for theme_id in used_theme_ids:
            theme = theme_by_id[theme_id]
            theme_activities = [
                dict(item)
                for item in activity_payloads
                if item["plan_source"] == f"weekly:{week_id}:{theme_id}"
            ]
            events.append(
                (
                    "WeeklyThemePlanned",
                    {
                        "week_id": week_id,
                        "theme_id": theme_id,
                        "title": str(theme["title"]),
                        "activity_ids": [
                            str(item["activity_id"]) for item in theme_activities
                        ],
                        "activities": theme_activities,
                        "rule_version": self.RULE_VERSION,
                    },
                )
            )
        events.extend(("ActivityPlanned", item) for item in activity_payloads)
        return events

    def score_candidate(
        self,
        state: dict[str, Any],
        activity: dict[str, Any],
        template_id: str,
    ) -> ActivityCandidateScore:
        """Score one candidate without relaxing seed/template constraints."""
        spec = state.get("life_outcome_templates", {}).get(template_id)
        if not isinstance(spec, dict):
            return ActivityCandidateScore(
                template_id, False, -10_000, (), ("unregistered_outcome_template",)
            )
        rejected: list[str] = []
        energy = int(state.get("needs", {}).get("energy", 0))
        energy_cost = int(spec.get("energy_cost", 0))
        if energy < energy_cost:
            rejected.append("insufficient_energy")

        starts_at = self._at(activity["starts_at"])
        ends_at = self._at(activity["ends_at"])
        goal = state.get("goals", {}).get(spec.get("goal_id")) if spec.get("goal_id") else None
        if spec.get("goal_id") and (
            not isinstance(goal, dict) or goal.get("status") != "active"
        ):
            rejected.append("goal_not_active")
        if isinstance(goal, dict) and goal.get("deadline") and self._at(goal["deadline"]) < self._at(activity["ends_at"]):
            rejected.append("goal_deadline_passed")
        day = starts_at.date().isoformat()
        npc_id = spec.get("npc_id")
        if npc_id and any(
            item.get("npc_id") == npc_id
            and str(item.get("starts_at", ""))[:10] == day
            for item in state.get("outcomes", {}).values()
        ):
            rejected.append("npc_daily_frequency_limit")
        if sum(
            1
            for item in state.get("outcomes", {}).values()
            if item.get("template_id") == template_id
            and str(item.get("starts_at", ""))[:10] == day
        ) >= int(spec.get("max_per_day", 1)):
            rejected.append("template_daily_frequency_limit")
        if npc_id and not self._npc_available(
            state.get("entities", {}), str(npc_id), starts_at, ends_at
        ):
            rejected.append("npc_unavailable")
        if rejected:
            return ActivityCandidateScore(
                template_id, False, -10_000, (), tuple(rejected)
            )

        score = 0
        reasons: list[str] = []
        if isinstance(goal, dict):
            priority = int(goal.get("priority", 0))
            remaining = max(
                0, int(goal.get("target", 0)) - int(goal.get("progress", 0))
            )
            score += priority * 8 + remaining * 2
            if priority:
                reasons.append("goal_priority")
            if goal.get("deadline"):
                hours = max(
                    0,
                    int(
                        (self._at(goal["deadline"]) - starts_at).total_seconds()
                        // 3600
                    ),
                )
                urgency_window = max(1, int(goal.get("urgency_window_hours", 72)))
                urgency = max(0, urgency_window - hours)
                if urgency:
                    score += min(40, urgency)
                    reasons.append("deadline_urgency")

        score -= energy_cost
        if energy_cost:
            reasons.append("resource_cost")

        recent_repetitions = sum(
            1
            for item in state.get("outcomes", {}).values()
            if isinstance(item, dict)
            and item.get("template_id") == template_id
            and item.get("ends_at")
            and timedelta(0)
            <= starts_at - self._at(item["ends_at"])
            <= timedelta(days=7)
        )
        if recent_repetitions:
            score -= recent_repetitions * 12
            reasons.append("recent_repetition_cost")

        chronic = state.get("life_evolution", {}).get("chronic", {})
        fatigue = int(chronic.get("fatigue", 0))
        relationship_pressure = int(chronic.get("relationship_pressure", 0))
        load = str(spec.get("load") or "medium")
        if load == "high" and fatigue:
            score -= fatigue * 2
            reasons.append("chronic_fatigue_high_load_cost")
        elif load == "low" and fatigue:
            score += fatigue // 2
            reasons.append("chronic_fatigue_low_load_preference")
        if bool(spec.get("social")) and relationship_pressure:
            score -= relationship_pressure
            reasons.append("relationship_pressure_social_cost")
        elif spec.get("social") is False and relationship_pressure:
            score += relationship_pressure // 3
            reasons.append("relationship_pressure_solitude_preference")

        for influence in state.get("life_evolution", {}).get("influences", {}).values():
            if not isinstance(influence, dict) or influence.get("status") != "active":
                continue
            if self._at(influence["expires_at"]) <= starts_at:
                continue
            kind = str(influence.get("kind") or "")
            if kind == "user_conflict" and load == "low":
                score += 30
                reasons.append("user_conflict_low_load_preference")
            elif kind == "user_vulnerability" and bool(spec.get("phone_accessible")):
                score += 25
                reasons.append("user_vulnerability_attention_preference")
            elif kind == "user_support" and load == "high":
                score += 10
                reasons.append("user_support_goal_momentum")

        return ActivityCandidateScore(
            template_id, True, score, tuple(reasons), ()
        )

    def events_for_user_influence(
        self,
        state: dict[str, Any],
        *,
        influence_id: str,
        kind: str,
        observed_at: datetime,
        expires_at: datetime,
        source_message_id: str | None = None,
    ) -> list[tuple[str, dict[str, Any]]]:
        if kind not in self.ALLOWED_INFLUENCES:
            raise ValueError("unsupported life influence")
        logical_now = self._at(state["clock"]["logical_at"])
        if observed_at > logical_now or expires_at <= logical_now:
            raise ValueError("life influence requires an observed time and future expiry")
        if expires_at > logical_now + timedelta(days=2):
            raise ValueError("life influence cannot exceed two days")
        payload = {
            "influence_id": influence_id,
            "kind": kind,
            "observed_at": observed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "source_message_id": source_message_id,
            "scope": "future_schedule_only",
            "rule_version": self.RULE_VERSION,
        }
        events: list[tuple[str, dict[str, Any]]] = [("LifeInfluenceRecorded", payload)]
        future_candidates: dict[str, dict[str, Any]] = {
            str(item["activity_id"]): item
            for item in state.get("agenda", {}).values()
            if isinstance(item, dict) and item.get("activity_id")
        }
        for raw_plan in state.get("weekly_plans", {}).values():
            if not isinstance(raw_plan, dict):
                continue
            for raw_theme in raw_plan.get("themes", {}).values():
                if not isinstance(raw_theme, dict):
                    continue
                for item in raw_theme.get("activities", []):
                    if isinstance(item, dict) and item.get("activity_id"):
                        future_candidates.setdefault(str(item["activity_id"]), item)
        for raw in sorted(
            future_candidates.values(), key=lambda item: str(item.get("starts_at", ""))
        ):
            if not isinstance(raw, dict) or raw.get("status") != "planned":
                # Weekly-plan activity specs have no execution status until
                # they are materialized into the agenda.
                if not str(raw.get("plan_source") or "").startswith("weekly:"):
                    continue
            starts_at = self._at(raw["starts_at"])
            if starts_at <= logical_now or starts_at >= expires_at:
                continue
            attention = int(raw.get("attention_demand", 35))
            preference_bias = str(raw.get("preference_bias") or "")
            if kind == "user_vulnerability":
                attention = min(100, attention + 20)
                preference_bias = "phone_accessible"
            elif kind == "user_conflict":
                attention = max(0, attention - 10)
                preference_bias = "low_load"
            elif kind == "user_support":
                preference_bias = "goal_momentum"
            else:
                preference_bias = "socially_available"
            events.append(
                (
                    "FutureActivityAdjusted",
                    {
                        "activity_id": str(raw["activity_id"]),
                        "influence_id": influence_id,
                        "attention_demand": attention,
                        "preference_bias": preference_bias,
                        "reason": kind,
                        "rule_version": self.RULE_VERSION,
                    },
                )
            )
        return events

    def environment_observation_events(
        self,
        *,
        observation_id: str,
        category: str,
        value: str,
        source_id: str,
        observed_at: datetime,
        expires_at: datetime,
        confidence: float,
        confirmed_current: bool,
    ) -> list[tuple[str, dict[str, Any]]]:
        if not confirmed_current:
            raise ValueError("environment observation requires confirmed current input")
        if category not in self.ALLOWED_OBSERVATIONS:
            raise ValueError("unsupported or high-risk environment observation")
        if not observation_id or not value.strip() or not source_id:
            raise ValueError("environment observation requires id, value, and source")
        if expires_at <= observed_at or expires_at > observed_at + timedelta(hours=48):
            raise ValueError("environment observation must expire within 48 hours")
        if not 0.0 <= confidence <= 0.7:
            raise ValueError("environment observation confidence must remain low")
        return [
            (
                "EnvironmentObservationRecorded",
                {
                    "observation_id": observation_id,
                    "category": category,
                    "value": value.strip(),
                    "source_id": source_id,
                    "observed_at": observed_at.isoformat(),
                    "expires_at": expires_at.isoformat(),
                    "confidence": confidence,
                    "weight": "low",
                    "rule_version": self.RULE_VERSION,
                },
            )
        ]

    def pressure_events(
        self,
        state: dict[str, Any],
        *,
        sample_id: str,
        week_start: datetime,
        fatigue: int,
        relationship_pressure: int,
    ) -> list[tuple[str, dict[str, Any]]]:
        if not sample_id or not 0 <= fatigue <= 100 or not 0 <= relationship_pressure <= 100:
            raise ValueError("life pressure requires an id and values from 0 to 100")
        if any(
            isinstance(item, dict) and item.get("sample_id") == sample_id
            for item in state.get("life_evolution", {}).get("pressure_samples", [])
        ):
            return []
        previous = state.get("life_evolution", {}).get("chronic", {})
        previous_fatigue = int(previous.get("fatigue", 0))
        previous_relationship = int(previous.get("relationship_pressure", 0))
        next_fatigue = round(previous_fatigue * 0.65 + fatigue * 0.35)
        next_relationship = round(
            previous_relationship * 0.65 + relationship_pressure * 0.35
        )
        chronic = {
            "fatigue": next_fatigue,
            "relationship_pressure": next_relationship,
            "high_load_preference": round(max(0.2, 1 - next_fatigue / 110), 3),
            "social_frequency": round(
                max(0.25, 1 - next_relationship / 150), 3
            ),
            "share_willingness": round(
                max(0.3, 1 - (next_fatigue + next_relationship) / 260), 3
            ),
        }
        return [
            (
                "LifePressureRecorded",
                {
                    "sample_id": sample_id,
                    "week_start": week_start.date().isoformat(),
                    "fatigue": fatigue,
                    "relationship_pressure": relationship_pressure,
                    "chronic": chronic,
                    "rule_version": self.RULE_VERSION,
                },
            )
        ]

    @staticmethod
    def _weekly_activity_id(week_id: str, starts_at: datetime, theme_id: str) -> str:
        # Keep the lived-date prefix used by all agenda/outcome projections;
        # the plan's week remains explicit in the rest of the stable id.
        return f"{starts_at.date().isoformat()}:weekly:{week_id}:{theme_id}"

    @staticmethod
    def _npc_available(
        entities: dict[str, Any], npc_id: str, starts_at: datetime, ends_at: datetime
    ) -> bool:
        for window in entities.get(npc_id, {}).get("availability", []):
            start, end = str(window).split("-", 1)
            start_minutes = int(start[:2]) * 60 + int(start[3:])
            end_minutes = int(end[:2]) * 60 + int(end[3:])
            activity_start = starts_at.hour * 60 + starts_at.minute
            activity_end = ends_at.hour * 60 + ends_at.minute
            if (
                start_minutes <= activity_start
                and activity_end <= end_minutes
                and starts_at.date() == ends_at.date()
            ):
                return True
        return False

    @staticmethod
    def _at(value: object) -> datetime:
        return datetime.fromisoformat(str(value))
