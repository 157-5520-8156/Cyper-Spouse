from __future__ import annotations

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.source_closure_verdict import (
    SourceClosureVisibleFinding,
)


def test_visible_finding_requires_a_concrete_non_whitespace_span() -> None:
    with pytest.raises(ValidationError, match="concrete visible span"):
        SourceClosureVisibleFinding(
            category="undeclared_external_assertion",
            visible_span=" \n ",
            claim_index=None,
            source_relation="unclosed",
            source_refs=(),
        )


def test_exact_report_discourse_relation_cannot_target_a_declared_claim() -> None:
    with pytest.raises(ValidationError, match="cannot target a declared claim"):
        SourceClosureVisibleFinding(
            category="undeclared_external_assertion",
            visible_span="这件事听着就累",
            claim_index=0,
            source_relation="exact_current_report_discourse_coverage",
            source_refs=("observation:current",),
        )


def test_visible_finding_wire_requires_explicit_claim_and_source_coordinates() -> None:
    with pytest.raises(ValidationError, match="Field required"):
        SourceClosureVisibleFinding(
            category="undeclared_external_assertion",
            visible_span="我刚从操场回来",
            source_relation="unclosed",
        )


def test_exact_report_discourse_relation_is_only_an_undeclared_assertion_coordinate() -> None:
    with pytest.raises(ValidationError, match="only applies to undeclared"):
        SourceClosureVisibleFinding(
            category="temporal_authority_mismatch",
            visible_span="听着就累",
            claim_index=None,
            source_relation="exact_current_report_discourse_coverage",
            source_refs=("observation:current",),
        )
