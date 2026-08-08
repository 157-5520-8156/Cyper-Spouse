"""Composition-only role identity for the open-life model provider."""

from __future__ import annotations

from typing import Literal, Protocol

from .model_authority_identity import provider_lane_sets_are_independent


class LifeDevelopmentCompletionModel(Protocol):
    model: str

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str: ...


class RoleBoundLifeDevelopmentModelAdapter:
    """Give one provider an explicit semantic role without hiding its origin."""

    def __init__(
        self,
        *,
        model: LifeDevelopmentCompletionModel,
        role: Literal[
            "world_author",
            "world_author_source_reviewer",
        ],
    ) -> None:
        if role not in {"world_author", "world_author_source_reviewer"}:
            raise ValueError("life development adapter accepts only a world-author role")
        model_id = str(getattr(model, "model", "")).strip() or type(model).__name__
        self.model = f"{model_id}/life-development/{role}"
        self._model = model
        self._role = role

    @property
    def authority_origin(self) -> LifeDevelopmentCompletionModel:
        """Expose the actual provider authority for author-exclusion checks.

        The role suffix is audit metadata.  It cannot turn one provider into
        an independent reviewer of bytes that provider authored.
        """

        return self._model

    @property
    def role(self) -> str:
        return self._role

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        complete_json = getattr(self._model, "complete_json", None)
        if callable(complete_json):
            return await complete_json(messages, temperature=temperature)
        return await self._model.complete(messages, temperature=temperature)

    def wire_reselection_route(self) -> "RoleBoundLifeDevelopmentModelAdapter":
        """Keep the same semantic role while changing only the provider lane."""

        route = getattr(self._model, "wire_reselection_route", None)
        if not callable(route):
            raise AttributeError("wrapped model has no wire reselection route")
        return RoleBoundLifeDevelopmentModelAdapter(
            model=route(),
            role=self._role,
        )


def life_development_reviewer_is_independent(
    *,
    author: object,
    reviewer: object | None,
) -> bool:
    """Return whether every possible review winner excludes the author.

    Availability wrappers and review races can expose more than one provider
    lane.  A hard-boundary reviewer is independent only when none of its
    possible winners overlaps any provider that could have authored the
    candidate.  Audit role labels are deliberately unwrapped first.
    """

    return provider_lane_sets_are_independent(author, reviewer)


__all__ = [
    "LifeDevelopmentCompletionModel",
    "RoleBoundLifeDevelopmentModelAdapter",
    "life_development_reviewer_is_independent",
]
