"""Composition contract for one bounded source-closure reselection lane."""

from __future__ import annotations

from dataclasses import dataclass

from .model_authority_identity import provider_lane_sets_are_independent
from .model_completion import ChatCompletionModel


@dataclass(frozen=True, slots=True)
class SourceClosureReselectionLane:
    """A same-role correction author plus independent factual reviewers."""

    author: ChatCompletionModel
    reviewer: ChatCompletionModel
    report_relative_reviewer: ChatCompletionModel | None = None
    inventory_model: ChatCompletionModel | None = None
    author_model_id: str | None = None

    def __post_init__(self) -> None:
        if not provider_lane_sets_are_independent(self.author, self.reviewer):
            raise ValueError("source-closure reselection author cannot review its own draft")
        for label, model in (
            ("report-relative reviewer", self.report_relative_reviewer),
            ("inventory model", self.inventory_model),
        ):
            if model is None:
                continue
            if not provider_lane_sets_are_independent(self.author, model):
                raise ValueError(f"source-closure reselection {label} must be independent")
        if self.inventory_model is not None and not provider_lane_sets_are_independent(
            self.inventory_model,
            self.reviewer,
        ):
            raise ValueError(
                "source-closure reselection inventory model must be independent from reviewer"
            )
        if (
            self.inventory_model is not None
            and self.report_relative_reviewer is not None
            and not provider_lane_sets_are_independent(
                self.inventory_model,
                self.report_relative_reviewer,
            )
            and self.report_relative_reviewer is not self.reviewer
        ):
            raise ValueError(
                "source-closure reselection inventory model must be independent "
                "from report-relative reviewer"
            )

    @property
    def model_id(self) -> str:
        inferred = str(getattr(self.author, "model", "")).strip()
        return (self.author_model_id or inferred or type(self.author).__name__)[:256]


__all__ = ["SourceClosureReselectionLane"]
