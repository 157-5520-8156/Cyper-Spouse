"""Bounded, replay-safe world projection for one companion turn."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

from companion_daemon.models import IncomingMessage
from companion_daemon.user_affect import active_user_affect_for_turn


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
    private_impressions: tuple[dict[str, object], ...]
    private_commitments: tuple[dict[str, object], ...]
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
            "private_impressions": list(self.private_impressions),
            "private_commitments": list(self.private_commitments),
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
            # These are explicitly fallible inner records, not user facts.
            "private_impressions": list(self.private_impressions),
            "private_commitments": list(self.private_commitments),
        }


class TurnFrameCompiler:
    """Compiles a fixed-size frame from one already-read World projection."""

    MAX_RECENT_MESSAGES = 8
    MAX_FACTS = 6
    MAX_EXPERIENCES = 4
    MAX_THREADS = 3
    MAX_ACTIONS = 4
    _GENERIC_PRIVATE_BIGRAMS = frozenset(
        {"我感", "感觉", "因为", "没有", "有点", "可能", "刚才", "等他", "他愿", "愿意", "听完", "的话", "这件"}
    )

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
        user_affect = active_user_affect_for_turn(
            self._mapping(self._mapping(snapshot.get("user_affect")).get(user_id)),
            logical_at=self._logical_at(snapshot),
            message_text=message.text,
        )
        private_impressions = tuple(
            self._private_impressions(
                snapshot, user_id=user_id, query=message.text
            )[: self.MAX_THREADS]
        )
        private_commitments = tuple(
            self._private_commitments(
                snapshot, user_id=user_id, query=message.text
            )[: self.MAX_THREADS]
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
                *(str(item.get("impression_id") or "") for item in private_impressions),
                *(
                    str(source)
                    for item in private_impressions
                    for source in list(item.get("source_event_ids") or [])
                ),
                *(str(item.get("commitment_id") or "") for item in private_commitments),
                *(
                    str(source)
                    for item in private_commitments
                    for source in list(item.get("source_event_ids") or [])
                ),
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
            private_impressions=private_impressions,
            private_commitments=private_commitments,
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
        if frame.private_impressions:
            advisories.append(
                InnerAdvisory(
                    kind="continuity",
                    tendency="有未被证实的内在判断；可以影响分寸，但绝不能当作用户事实。",
                    intensity=max(
                        20,
                        min(
                            80,
                            int(
                                max(
                                    float(item.get("confidence") or 0.0)
                                    for item in frame.private_impressions
                                )
                                * 100
                            ),
                        ),
                    ),
                    confidence=max(
                        float(item.get("confidence") or 0.0)
                        for item in frame.private_impressions
                    ),
                    source_event_ids=tuple(
                        str(item.get("impression_id") or "")
                        for item in frame.private_impressions
                        if str(item.get("impression_id") or "")
                    ),
                )
            )
        if frame.private_commitments:
            advisories.append(
                InnerAdvisory(
                    kind="agency",
                    tendency="有仍想在意的事；只在当前输入相关时自然承接，不把它说成已经安排或完成。",
                    intensity=max(
                        int(item.get("priority") or 0)
                        for item in frame.private_commitments
                    ),
                    confidence=0.9,
                    source_event_ids=tuple(
                        str(item.get("commitment_id") or "")
                        for item in frame.private_commitments
                        if str(item.get("commitment_id") or "")
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
        recent_question_sources = tuple(
            str(item.get("source_id") or "")
            for item in frame.recent_messages[-4:]
            if str(item.get("direction") or "") == "out"
            and str(item.get("text") or "").rstrip().endswith(("？", "?"))
            and str(item.get("source_id") or "")
        )
        if recent_question_sources:
            advisories.append(
                InnerAdvisory(
                    kind="rhythm",
                    tendency=(
                        "刚刚已经问过问题；若用户是在继续分享，先用完整陈述接住，"
                        "不必再用问题收尾。"
                    ),
                    intensity=65,
                    confidence=0.85,
                    source_event_ids=recent_question_sources,
                )
            )
        return tuple(advisories)

    @staticmethod
    def _mapping(value: object) -> dict[str, object]:
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _logical_at(snapshot: dict[str, object]) -> datetime | None:
        value = TurnFrameCompiler._mapping(snapshot.get("clock")).get("logical_at")
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

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

    def _private_impressions(
        self, snapshot: dict[str, object], *, user_id: str, query: str
    ) -> list[dict[str, object]]:
        logical_at = str(self._mapping(snapshot.get("clock")).get("logical_at") or "")
        impressions = [
            {
                "impression_id": str(impression_id),
                "kind": str(item.get("kind") or ""),
                "summary": str(item.get("summary") or "")[:240],
                "confidence": float(item.get("confidence") or 0.0),
                "source_event_ids": list(item.get("source_event_ids") or [])[:6],
                "expires_at": str(item.get("expires_at") or ""),
                "contradictory_evidence": list(item.get("contradictory_evidence") or [])[:6],
            }
            for impression_id, raw in self._mapping(snapshot.get("private_impressions")).items()
            if (item := self._mapping(raw)).get("status") == "active"
            and item.get("user_id") == user_id
            and (not logical_at or str(item.get("expires_at") or "") > logical_at)
        ]
        return [
            item
            for score, item in sorted(
                (
                    (
                        self._inner_relevance(
                            query,
                            self._inner_source_content(
                                snapshot, item["source_event_ids"]
                            )
                            or str(item["summary"]),
                        ),
                        item,
                    )
                    for item in impressions
                ),
                key=lambda entry: (
                    -entry[0],
                    -float(entry[1]["confidence"]),
                    str(entry[1]["expires_at"]),
                ),
            )
            if score > 0
        ]

    def _private_commitments(
        self, snapshot: dict[str, object], *, user_id: str, query: str
    ) -> list[dict[str, object]]:
        logical_at = str(self._mapping(snapshot.get("clock")).get("logical_at") or "")
        commitments = [
            {
                "commitment_id": str(commitment_id),
                "intention": str(item.get("intention") or "")[:240],
                "priority": int(item.get("priority") or 0),
                "source_event_ids": list(item.get("source_event_ids") or [])[:6],
                "expires_at": str(item.get("expires_at") or ""),
                "related_thread_id": str(item.get("related_thread_id") or ""),
            }
            for commitment_id, raw in self._mapping(snapshot.get("private_commitments")).items()
            if (item := self._mapping(raw)).get("status") == "active"
            and item.get("user_id") == user_id
            and (not logical_at or str(item.get("expires_at") or "") > logical_at)
        ]
        return [
            item
            for score, item in sorted(
                (
                    (
                        self._inner_relevance(
                            query,
                            self._inner_source_content(
                                snapshot, item["source_event_ids"]
                            )
                            or str(item["intention"]),
                        ),
                        item,
                    )
                    for item in commitments
                ),
                key=lambda entry: (
                    -entry[0],
                    -int(entry[1]["priority"]),
                    str(entry[1]["expires_at"]),
                ),
            )
            if score > 0
        ]

    def _inner_source_content(
        self, snapshot: dict[str, object], source_refs: object
    ) -> str:
        refs = {str(item) for item in source_refs if str(item)} if isinstance(source_refs, list) else set()
        if not refs:
            return ""
        content: list[str] = []
        for raw in snapshot.get("recent_messages", []):
            item = self._mapping(raw)
            if f"message:{str(item.get('message_id') or '')}" in refs:
                content.append(str(item.get("text") or ""))
        for fact_id, raw in self._mapping(snapshot.get("facts")).items():
            if str(fact_id) in refs:
                content.append(str(self._mapping(raw).get("value") or ""))
        for experience_id, raw in self._mapping(snapshot.get("experiences")).items():
            if str(experience_id) in refs:
                content.append(str(self._mapping(raw).get("content") or ""))
        for thread_id, raw in self._mapping(snapshot.get("conversation_threads")).items():
            if str(thread_id) in refs:
                content.append(str(self._mapping(raw).get("question") or ""))
        return "\n".join(part for part in content if part)

    @staticmethod
    def _inner_relevance(query: str, text: str) -> int:
        """Use compact lexical overlap; no private record is globally sticky."""
        normalized_query = re.sub(r"\s+", "", query)
        normalized_text = re.sub(r"\s+", "", text)
        if len(normalized_query) < 2 or len(normalized_text) < 2:
            return 0
        query_bigrams = {
            normalized_query[index : index + 2]
            for index in range(len(normalized_query) - 1)
        }
        text_bigrams = {
            normalized_text[index : index + 2]
            for index in range(len(normalized_text) - 1)
        }
        return len((query_bigrams & text_bigrams) - TurnFrameCompiler._GENERIC_PRIVATE_BIGRAMS)

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
        # ``emotion_modulation`` also contains operational projection fields
        # (charge, violation count, decay bookkeeping, etc.).  They describe
        # state around an emotion; they are not emotional dimensions and must
        # never be surfaced as a model tendency.
        vector = TurnFrameCompiler._mapping(affect.get("vector"))
        candidates = [
            (str(key), int(value))
            for key, value in vector.items()
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and int(value) > 0
        ]
        return max(candidates, key=lambda item: item[1]) if candidates else None
