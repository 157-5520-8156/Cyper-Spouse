"""Bounded, replay-safe world projection for one companion turn."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from companion_daemon.models import IncomingMessage


@dataclass(frozen=True)
class InnerAdvisory:
    """A fallible, non-authoritative tendency supplied to the dialogue model."""

    kind: str
    tendency: str
    intensity: int
    confidence: float
    source_event_ids: tuple[str, ...]
    expires_at: datetime | None = None
    contradictory_evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnFrame:
    """The only bounded World projection exposed to ordinary reply generation."""

    world_id: str
    revision: int
    state_hash: str
    user_id: str
    input_message_id: str
    recent_messages: tuple[dict[str, object], ...]
    scene: dict[str, object]
    relationship: dict[str, object]
    affect: dict[str, object]
    user_affect: dict[str, object]
    facts: tuple[dict[str, object], ...]
    experiences: tuple[dict[str, object], ...]
    open_threads: tuple[dict[str, object], ...]
    open_actions: tuple[dict[str, object], ...]
    capability: dict[str, object]
    dependency_tokens: tuple[str, ...]

    def prompt_payload(self) -> dict[str, object]:
        """Return only bounded, provenance-carrying data suitable for one prompt."""
        return {
            "world_id": self.world_id,
            "revision": self.revision,
            "state_hash": self.state_hash,
            "dependency_tokens": list(self.dependency_tokens),
            "scene": self.scene,
            "relationship": self.relationship,
            "affect": self.affect,
            "user_affect": self.user_affect,
            "recent_messages": list(self.recent_messages),
            "facts": list(self.facts),
            "experiences": list(self.experiences),
            "open_threads": list(self.open_threads),
            "open_actions": list(self.open_actions),
            "capability": self.capability,
        }

    def prompt_delta(self) -> dict[str, object]:
        """Return the non-duplicative part of the frame for reply prompting.

        Facts, transcript, scene and relationship projection already pass
        through the provenance-aware five-layer context budget.  Repeating
        them here made the frame an expensive second prompt.  The model only
        needs this small dependency and pending-work delta beside that budget.
        """
        return {
            "world_revision": self.revision,
            "state_hash": self.state_hash,
            "dependency_tokens": list(self.dependency_tokens),
            "open_threads": list(self.open_threads),
            "open_actions": list(self.open_actions),
            "capability": self.capability,
        }


class TurnFrameCompiler:
    """Compiles a fixed-size frame from one already-read World projection."""

    MAX_RECENT_MESSAGES = 8
    MAX_FACTS = 6
    MAX_EXPERIENCES = 4
    MAX_THREADS = 3
    MAX_ACTIONS = 4

    def compile(
        self,
        *,
        world_id: str,
        revision: int,
        state_hash: str,
        snapshot: dict[str, object],
        user_id: str,
        message: IncomingMessage,
    ) -> TurnFrame:
        recent = tuple(
            self._recent_messages(
                snapshot,
                user_id=user_id,
                exclude_message_id=str(message.message_id or ""),
            )[-self.MAX_RECENT_MESSAGES :]
        )
        facts = tuple(self._facts(snapshot, user_id=user_id)[-self.MAX_FACTS :])
        experiences = tuple(self._experiences(snapshot)[-self.MAX_EXPERIENCES :])
        threads = tuple(self._threads(snapshot, user_id=user_id)[: self.MAX_THREADS])
        actions = tuple(self._actions(snapshot)[: self.MAX_ACTIONS])
        scene = self._scene(snapshot)
        relationship = dict(self._mapping(snapshot.get("relationships")).get(user_id, {}))
        affect = dict(self._mapping(snapshot.get("emotion_modulation")))
        user_affect = dict(
            self._mapping(self._mapping(snapshot.get("user_affect")).get(user_id))
        )
        capability = self._capability(snapshot)
        dependencies = tuple(
            token
            for token in (
                f"world:{world_id}:revision:{revision}:state:{state_hash}",
                *(str(item.get("source_id") or "") for item in facts),
                *(str(item.get("experience_id") or "") for item in experiences),
                *(str(item.get("thread_id") or "") for item in threads),
                *(str(item.get("action_id") or "") for item in actions),
            )
            if token
        )
        return TurnFrame(
            world_id=world_id,
            revision=revision,
            state_hash=state_hash,
            user_id=user_id,
            input_message_id=str(message.message_id or ""),
            recent_messages=recent,
            scene=scene,
            relationship=relationship,
            affect=affect,
            user_affect=user_affect,
            facts=facts,
            experiences=experiences,
            open_threads=threads,
            open_actions=actions,
            capability=capability,
            dependency_tokens=dependencies,
        )

    def advisories(self, frame: TurnFrame) -> tuple[InnerAdvisory, ...]:
        """Produce bounded suggestions; none may veto or create World truth."""
        advisories: list[InnerAdvisory] = []
        strongest = self._strongest_affect(frame.affect)
        if strongest is not None:
            label, value = strongest
            advisories.append(
                InnerAdvisory(
                    kind="affect",
                    tendency=f"当前{label}残留，表达可受影响但不必直说。",
                    intensity=min(100, value),
                    confidence=0.8,
                    source_event_ids=(f"world_revision:{frame.revision}",),
                )
            )
        stage = str(frame.relationship.get("stage") or "stranger")
        advisories.append(
            InnerAdvisory(
                kind="relationship",
                tendency=f"维持{stage}阶段相称的亲密度与边界。",
                intensity=45,
                confidence=0.9,
                source_event_ids=(f"world_revision:{frame.revision}",),
            )
        )
        if (
            bool(frame.user_affect.get("unresolved"))
            and str(frame.user_affect.get("kind") or "")
            in {"disappointment", "confusion"}
        ):
            advisories.append(
                InnerAdvisory(
                    kind="repair",
                    tendency="用户可能仍有失望或困惑；先接住当下，不要用追问抢走修复。",
                    intensity=min(100, int(frame.user_affect.get("intensity") or 0) * 25),
                    confidence=float(frame.user_affect.get("confidence") or 0.6),
                    source_event_ids=tuple(
                        item
                        for item in (
                            str(frame.user_affect.get("source_message_id") or ""),
                        )
                        if item
                    )
                    or (f"world_revision:{frame.revision}",),
                )
            )
        if frame.open_threads:
            advisories.append(
                InnerAdvisory(
                    kind="continuity",
                    tendency="存在未解决话题；仅在和当前输入相关时自然承接。",
                    intensity=55,
                    confidence=0.75,
                    source_event_ids=tuple(
                        str(item.get("thread_id") or "") for item in frame.open_threads
                    ),
                )
            )
        if frame.open_actions:
            advisories.append(
                InnerAdvisory(
                    kind="agency",
                    tendency="存在未结算外部行动；不得把它们说成已经完成。",
                    intensity=60,
                    confidence=0.95,
                    source_event_ids=tuple(
                        str(item.get("action_id") or "") for item in frame.open_actions
                    ),
                )
            )
        if len(frame.recent_messages) >= 4:
            advisories.append(
                InnerAdvisory(
                    kind="rhythm",
                    tendency="对话正在来回；先具体接住，再决定是否追问。",
                    intensity=50,
                    confidence=0.7,
                    source_event_ids=(f"world_revision:{frame.revision}",),
                )
            )
        return tuple(advisories)

    @staticmethod
    def _mapping(value: object) -> dict[str, object]:
        return dict(value) if isinstance(value, dict) else {}

    def _recent_messages(
        self,
        snapshot: dict[str, object],
        *,
        user_id: str,
        exclude_message_id: str,
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for raw in snapshot.get("recent_messages", []):
            if not isinstance(raw, dict):
                continue
            if raw.get("user_id") not in {"", None, user_id}:
                continue
            if str(raw.get("message_id") or "") == exclude_message_id:
                continue
            text = str(raw.get("text") or "").strip()
            if not text:
                continue
            result.append(
                {
                    "source_id": f"message:{raw.get('message_id') or ''}",
                    "direction": str(raw.get("direction") or ""),
                    "text": text[:600],
                    "logical_at": str(raw.get("logical_at") or raw.get("sent_at") or ""),
                }
            )
        return result

    def _facts(self, snapshot: dict[str, object], *, user_id: str) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for fact_id, raw in self._mapping(snapshot.get("facts")).items():
            fact = self._mapping(raw)
            if fact.get("subject") not in {user_id, "zhizhi"}:
                continue
            if str(fact.get("status") or "current") not in {"current", "confirmed"}:
                continue
            result.append(
                {
                    "source_id": str(fact_id),
                    "subject": str(fact.get("subject") or ""),
                    "value": str(fact.get("value") or "")[:400],
                    "provenance": str(fact.get("source") or fact_id),
                }
            )
        return result

    def _experiences(self, snapshot: dict[str, object]) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for experience_id, raw in self._mapping(snapshot.get("experiences")).items():
            item = self._mapping(raw)
            result.append(
                {
                    "experience_id": str(experience_id),
                    "content": str(item.get("content") or "")[:500],
                    "shared": bool(item.get("shared")),
                    "provenance": f"experience:{experience_id}",
                }
            )
        return result

    def _threads(self, snapshot: dict[str, object], *, user_id: str) -> list[dict[str, object]]:
        return [
            {
                "thread_id": str(thread_id),
                "question": str(item.get("question") or "")[:300],
                "expires_at": str(item.get("expires_at") or ""),
            }
            for thread_id, raw in self._mapping(snapshot.get("conversation_threads")).items()
            if (item := self._mapping(raw)).get("status") == "open"
            and item.get("user_id") == user_id
        ]

    def _actions(self, snapshot: dict[str, object]) -> list[dict[str, object]]:
        return [
            {
                "action_id": str(action_id),
                "kind": str(item.get("kind") or ""),
                "status": str(item.get("status") or ""),
                "capability": str(item.get("message_kind") or item.get("outbound_trigger") or ""),
            }
            for action_id, raw in self._mapping(snapshot.get("actions")).items()
            if (item := self._mapping(raw)).get("status") in {"scheduled", "sending", "unknown"}
        ]

    def _scene(self, snapshot: dict[str, object]) -> dict[str, object]:
        clock = self._mapping(snapshot.get("clock"))
        agenda = self._mapping(snapshot.get("agenda"))
        active = next(
            (
                self._mapping(raw)
                for raw in agenda.values()
                if self._mapping(raw).get("status") == "active"
            ),
            {},
        )
        return {
            "logical_at": str(clock.get("logical_at") or ""),
            "activity": str(active.get("title") or active.get("activity_id") or ""),
            "status": str(active.get("status") or "idle"),
        }

    def _capability(self, snapshot: dict[str, object]) -> dict[str, object]:
        return {
            "can_send_text": True,
            "has_open_actions": bool(self._actions(snapshot)),
            "controlled_transgression_enabled": bool(
                snapshot.get("controlled_transgressions") is not None
            ),
        }

    @staticmethod
    def _strongest_affect(affect: dict[str, object]) -> tuple[str, int] | None:
        candidates = [
            (str(key), int(value))
            for key, value in affect.items()
            if isinstance(value, (int, float)) and int(value) > 0
        ]
        return max(candidates, key=lambda item: item[1]) if candidates else None
