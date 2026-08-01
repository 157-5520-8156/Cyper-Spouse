"""Auditable semantic findings and deterministic source-closure adjudication.

The semantic reviewer identifies propositions and their evidence relation.  The
host does not interpret prose: it only verifies structural coordinates and may
remove one narrowly-scoped accusation when the reviewer explicitly binds it to
the exact current counterpart report as report-only discourse uptake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel


SourceClosureFailureCategory = Literal[
    "undeclared_external_assertion",
    "subject_authority_mismatch",
    "temporal_authority_mismatch",
    "occurrence_or_status_authority_mismatch",
]

SourceClosureSourceRelation = Literal[
    "unclosed",
    "not_external_proposition",
    "exact_current_report_discourse_coverage",
    "exact_dialogue_record_coverage",
    "first_person_immediate_private_continuity",
    "declared_world_claim_source_mismatch",
]

# These are epistemic mismatch coordinates, not reply modes or behavioral
# categories.  They may be carried from a bounded report-relative verdict to
# the same role's one complete rechoice, then explicitly rechecked by the
# existing final source-review call.
SourceClosureFailureDimension = Literal[
    "participant_role",
    "logical_modality",
    "polarity",
    "temporal_relation",
    "agent_patient_relation",
    "added_external_premise",
    "habitual_or_generic_scope",
]


class SourceClosureVisibleFinding(FrozenModel):
    """One provider-identified visible proposition and its evidence relation."""

    category: SourceClosureFailureCategory
    visible_span: str = Field(min_length=1, max_length=1_024)
    claim_index: int | None = Field(ge=0)
    source_relation: SourceClosureSourceRelation
    source_refs: tuple[str, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def relation_has_auditable_coordinates(self) -> "SourceClosureVisibleFinding":
        if not self.visible_span.strip():
            raise ValueError(
                "source-closure visible finding requires a concrete visible span"
            )
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("source-closure visible finding source refs must be unique")
        if any(not source_ref for source_ref in self.source_refs):
            raise ValueError(
                "source-closure visible finding source refs must be non-empty"
            )
        if (
            self.source_relation == "exact_current_report_discourse_coverage"
            and not self.source_refs
        ):
            raise ValueError(
                "exact-current-report discourse finding requires a source ref"
            )
        if (
            self.source_relation == "exact_current_report_discourse_coverage"
            and self.claim_index is not None
        ):
            raise ValueError(
                "exact-current-report discourse finding cannot target a declared claim"
            )
        if (
            self.source_relation == "exact_current_report_discourse_coverage"
            and self.category != "undeclared_external_assertion"
        ):
            raise ValueError(
                "exact-current-report discourse coverage only applies to undeclared "
                "external assertions"
            )
        if self.source_relation == "exact_dialogue_record_coverage":
            if not self.source_refs:
                raise ValueError(
                    "exact-dialogue-record finding requires a source ref"
                )
            if self.claim_index is not None:
                raise ValueError(
                    "exact-dialogue-record finding cannot target a declared claim"
                )
            if self.category != "undeclared_external_assertion":
                raise ValueError(
                    "exact-dialogue-record coverage only applies to undeclared "
                    "external assertions"
                )
        if self.source_relation == "first_person_immediate_private_continuity":
            if self.claim_index is not None:
                raise ValueError(
                    "first-person immediate private continuity cannot target a declared claim"
                )
            if self.source_refs:
                raise ValueError(
                    "first-person immediate private continuity cannot cite external authority"
                )
            if self.category != "undeclared_external_assertion":
                raise ValueError(
                    "first-person immediate private continuity only applies to undeclared "
                    "external assertions"
                )
        if self.source_relation == "not_external_proposition":
            if self.claim_index is not None:
                raise ValueError(
                    "not-external proposition finding cannot target a declared claim"
                )
            if self.source_refs:
                raise ValueError(
                    "not-external proposition finding cannot cite external authority"
                )
            if self.category != "undeclared_external_assertion":
                raise ValueError(
                    "not-external proposition relation only applies to undeclared "
                    "external assertions"
                )
        if (
            self.source_relation == "declared_world_claim_source_mismatch"
            and self.claim_index is None
        ):
            raise ValueError(
                "declared-world-claim source mismatch requires a claim index"
            )
        return self


@dataclass(frozen=True, slots=True)
class SourceClosureVisibleAdjudication:
    """Normalized visible failures plus findings resolved by discourse authority."""

    retained_failure_categories: tuple[SourceClosureFailureCategory, ...]
    discourse_resolved_finding_indexes: tuple[int, ...]


def adjudicate_visible_source_findings(
    *,
    accused_failure_categories: tuple[SourceClosureFailureCategory, ...],
    findings: tuple[SourceClosureVisibleFinding, ...],
    visible_text: str,
    world_claim_count: int,
    exact_current_report_source_refs: frozenset[str],
) -> SourceClosureVisibleAdjudication:
    """Validate reviewer coordinates and resolve only exact-report uptake.

    This function intentionally performs no lexical or semantic classification.
    It trusts the reviewer's explicit semantic relation only after validating
    the referenced authority, visible span, and absence of a declared-claim
    target. Missing or contradictory structure is an invalid reviewer result,
    not evidence that the authored expression is factually unsupported.
    """

    accused = frozenset(accused_failure_categories)
    seen_findings: set[
        tuple[
            SourceClosureFailureCategory,
            str,
            int | None,
            SourceClosureSourceRelation,
            tuple[str, ...],
        ]
    ] = set()
    resolved_indexes: list[int] = []
    retained_categories: set[SourceClosureFailureCategory] = set()
    findings_by_category: dict[
        SourceClosureFailureCategory,
        list[tuple[int, SourceClosureVisibleFinding]],
    ] = {}

    for index, finding in enumerate(findings):
        if finding.category not in accused:
            raise ValueError(
                "source-closure visible finding names an unaccused category"
            )
        if finding.visible_span not in visible_text:
            raise ValueError(
                "source-closure visible finding span is absent from visible_text"
            )
        if (
            finding.claim_index is not None
            and finding.claim_index >= world_claim_count
        ):
            raise ValueError(
                "source-closure visible finding returned invalid claim index"
            )
        identity = (
            finding.category,
            finding.visible_span,
            finding.claim_index,
            finding.source_relation,
            finding.source_refs,
        )
        if identity in seen_findings:
            raise ValueError("source-closure review returned duplicate visible findings")
        seen_findings.add(identity)
        findings_by_category.setdefault(finding.category, []).append(
            (index, finding)
        )

    for category in accused_failure_categories:
        category_findings = findings_by_category.get(category, ())
        if not category_findings:
            raise ValueError(
                "source-closure visible failure lacks a concrete visible finding"
            )
        for index, finding in category_findings:
            report_refs = frozenset(finding.source_refs)
            is_exact_report_uptake = (
                category == "undeclared_external_assertion"
                and finding.claim_index is None
                and finding.source_relation
                == "exact_current_report_discourse_coverage"
                and bool(report_refs)
                and report_refs.issubset(exact_current_report_source_refs)
            )
            if is_exact_report_uptake:
                resolved_indexes.append(index)
            else:
                retained_categories.add(category)

    return SourceClosureVisibleAdjudication(
        retained_failure_categories=tuple(
            category
            for category in accused_failure_categories
            if category in retained_categories
        ),
        discourse_resolved_finding_indexes=tuple(resolved_indexes),
    )


__all__ = [
    "SourceClosureFailureDimension",
    "SourceClosureFailureCategory",
    "SourceClosureSourceRelation",
    "SourceClosureVisibleAdjudication",
    "SourceClosureVisibleFinding",
    "adjudicate_visible_source_findings",
]
