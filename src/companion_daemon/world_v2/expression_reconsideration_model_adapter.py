"""Bounded semantic reviewer for a user-interrupted expression beat.

This adapter intentionally answers only *what to do with the old beat*.  It
does not author response text, Actions, reservations, trigger IDs, or a new
plan.  Replacement work must travel through the already audited normal
ExpressionPlan lane, which keeps an LLM's semantic judgement separate from
effect authority.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from .expression_reconsideration_runtime import ExpressionReconsiderationDecision
from .schemas import ProjectionCursor, TriggerProcess, WorldEvent


class ExpressionReconsiderationChatModel(Protocol):
    model: str

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str: ...


class ExpressionReconsiderationChatModelAdapter:
    """Parse an intentionally small no-prose decision grammar.

    The model may choose ``continue``, ``cancel`` or ``defer``.  The other
    typed dispositions remain available to a composition-owned deliberation
    worker only after it has produced an auditable replacement plan reference.
    This avoids allowing a bare model string to fabricate that authority.
    """

    VERSION = "expression-reconsideration-draft.1"

    def __init__(self, *, model: ExpressionReconsiderationChatModel, temperature: float = 0.25) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("expression reconsideration temperature must be between 0 and 2")
        self._model = model
        self._temperature = temperature

    async def review(
        self, *, process: TriggerProcess, observation_event: WorldEvent, cursor: ProjectionCursor
    ) -> ExpressionReconsiderationDecision:
        raw = await self._model.complete(
            self._messages(process=process, observation_event=observation_event, cursor=cursor),
            temperature=self._temperature,
        )
        return self._decision(raw)

    @classmethod
    def _decision(cls, raw: str) -> ExpressionReconsiderationDecision:
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("expression reconsideration model did not return one JSON object") from exc
        if not isinstance(value, dict) or set(value) != {"disposition"}:
            raise ValueError("expression reconsideration model returned unsupported fields")
        disposition = value.get("disposition")
        if disposition not in {"continue", "cancel", "defer"}:
            raise ValueError("expression reconsideration model returned unsupported disposition")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return ExpressionReconsiderationDecision(
            disposition=disposition,
            rationale_ref=f"model-decision:{cls.VERSION}:{digest}",
        )

    @staticmethod
    def _messages(
        *, process: TriggerProcess, observation_event: WorldEvent, cursor: ProjectionCursor
    ) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You review one not-yet-dispatched companion expression after a new user message. "
                    "Return exactly JSON {\"disposition\":...}; no Markdown or other keys. "
                    "Choose continue only when the old response still fits the new message. Choose cancel "
                    "when sending it would be jarring, irrelevant, repetitive, or insensitive. Choose defer "
                    "when the old response should wait for more context. You cannot compose a replacement "
                    "here. Allowed dispositions: continue, cancel, defer."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "trigger_id": process.trigger_id,
                        "trigger_ref": process.trigger_ref,
                        "new_observation": observation_event.payload(),
                        "cursor": cursor.model_dump(mode="json"),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]


__all__ = ["ExpressionReconsiderationChatModel", "ExpressionReconsiderationChatModelAdapter"]
