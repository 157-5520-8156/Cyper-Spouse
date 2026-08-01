from __future__ import annotations

import asyncio
from hashlib import sha256
import json
from types import SimpleNamespace

from companion_daemon.world_v2.isolated_source_closure_trace import (
    BoundedSourceClosureTraceCollector,
    capture_isolated_source_closure_trace,
    emit_source_closure_candidate_materialization_failure_trace,
    emit_source_closure_trace,
    emit_source_closure_verdict_trace,
    emit_source_closure_wire_failure_trace,
)
from companion_daemon.world_v2.source_closure_verdict import (
    SourceClosureVisibleFinding,
)


def _candidate(*, visible: str, private: str) -> str:
    return json.dumps(
        {
            "expression_draft": {
                "private_turn_state": {
                    "inner_state_summary": private,
                    "attended_source_refs": ["secret-context-ref"],
                },
                "beats": [
                    {"modality": "text", "text": visible},
                    {"modality": "sticker", "sticker_id": "sticker:wave"},
                ],
                "world_claims": [],
            }
        },
        ensure_ascii=False,
    )


def test_trace_is_absent_by_default_and_sink_failures_are_observational_only() -> None:
    candidate = _candidate(visible="这句可以看见。", private="绝不能进入 trace 的私有状态")
    disabled = BoundedSourceClosureTraceCollector()

    emit_source_closure_trace(
        stage="post_appeal_initial_rejection",
        raw_candidate=candidate,
        ci=(0,),
        v=("undeclared_external_assertion",),
        p=(),
    )

    class _BrokenSink:
        def record(self, _event: object) -> None:
            raise RuntimeError("diagnostic sink is broken")

    with capture_isolated_source_closure_trace(_BrokenSink()):
        emit_source_closure_trace(
            stage="post_appeal_initial_rejection",
            raw_candidate=candidate,
            ci=(0,),
            v=("undeclared_external_assertion",),
            p=(),
        )

    assert disabled.snapshot() == ()


def test_trace_keeps_only_bounded_visible_surface_hashes_and_rejection_coordinates() -> None:
    private = "PRIVATE_TURN_STATE_DO_NOT_RETAIN"
    reviewer_reason = "REVIEWER_REASON_DO_NOT_RETAIN"
    prompt = "PINNED_CONTEXT_AND_PROMPT_DO_NOT_RETAIN"
    collector = BoundedSourceClosureTraceCollector()

    with capture_isolated_source_closure_trace(collector):
        emit_source_closure_trace(
            stage="reselection_output_invalid_before_review",
            raw_candidate=_candidate(
                visible="我刚才把你的话接歪了。",
                private=f"{private} {reviewer_reason} {prompt}",
            ),
            ci=(2, 2, 1),
            v=("undeclared_external_assertion", "undeclared_external_assertion"),
            p=("temporal_authority_mismatch",),
        )

    event = collector.snapshot()[0]
    payload = event.as_dict()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload["stage"] == "reselection_output_invalid_before_review"
    assert payload["ci"] == [2, 1]
    assert payload["v"] == ["undeclared_external_assertion"]
    assert payload["p"] == ["temporal_authority_mismatch"]
    assert payload["visible_beat_texts"] == ["我刚才把你的话接歪了。"]
    assert len(payload["candidate_sha256"]) == 64
    assert len(payload["visible_beat_sha256"]) == 1
    assert len(payload["visible_beat_sha256"][0]) == 64
    assert private not in encoded
    assert reviewer_reason not in encoded
    assert prompt not in encoded
    assert "private_turn_state" not in encoded
    assert "source_ref" not in encoded


def test_trace_keeps_only_sanitized_prior_correction_coordinate() -> None:
    private = "PRIVATE_STATE_MUST_NOT_ENTER_CORRECTION_TRACE"
    collector = BoundedSourceClosureTraceCollector()

    with capture_isolated_source_closure_trace(collector):
        emit_source_closure_trace(
            stage="initial_rejection",
            raw_candidate=_candidate(
                visible="我下午在宿舍翻了一本旧诗集。",
                private=private,
            ),
            ci=(),
            v=("undeclared_external_assertion",),
            p=(),
            prior_correction_kind="private_turn_state",
            sanitized_failure_code="private_turn_state.field_order",
            sanitized_failure_field_path="private_turn_state",
        )

    payload = collector.snapshot()[0].as_dict()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload["prior_correction_kind"] == "private_turn_state"
    assert payload["sanitized_failure_code"] == "private_turn_state.field_order"
    assert payload["sanitized_failure_field_path"] == "private_turn_state"
    assert private not in encoded
    assert "inner_state_summary" not in encoded


def test_trace_keeps_auditable_finding_coordinates_without_raw_authority_refs() -> None:
    collector = BoundedSourceClosureTraceCollector()
    authority_ref = "observation:private-current-report"
    visible_span = "他肯定一直都故意坑学生"

    with capture_isolated_source_closure_trace(collector):
        emit_source_closure_trace(
            stage="initial_rejection",
            raw_candidate=_candidate(
                visible=f"跟摊贩争了半天，听着就累。{visible_span}。",
                private="private",
            ),
            ci=(),
            v=("undeclared_external_assertion",),
            p=(),
            visible_findings=(
                SourceClosureVisibleFinding(
                    category="undeclared_external_assertion",
                    visible_span=visible_span,
                    claim_index=None,
                    source_relation="unclosed",
                    source_refs=(authority_ref,),
                ),
            ),
            discourse_resolved_visible_finding_indexes=(0,),
        )

    payload = collector.snapshot()[0].as_dict()
    finding = payload["visible_findings"][0]
    encoded = json.dumps(payload, ensure_ascii=False)
    assert finding == {
        "category": "undeclared_external_assertion",
        "visible_span": visible_span,
        "visible_span_sha256": sha256(visible_span.encode("utf-8")).hexdigest(),
        "visible_span_truncated": False,
        "claim_index": None,
        "source_relation": "unclosed",
        "authority_sha256": [sha256(authority_ref.encode("utf-8")).hexdigest()],
    }
    assert payload["discourse_resolved_visible_finding_indexes"] == [0]
    assert authority_ref not in encoded
    assert "source_ref" not in encoded


def test_success_trace_distinguishes_empty_external_inventory_from_not_external_coverage() -> None:
    """Accepted semantic paths remain diagnosable without retaining visible prose."""

    private_text = "PRIVATE_VISIBLE_TEXT_MUST_BE_HASHED"
    private_locator = SimpleNamespace(
        beat_index=0,
        char_start=0,
        char_end=len(private_text),
        text=private_text,
    )
    private_proposition = SimpleNamespace(
        locator=private_locator,
        semantic_role="outer_private_state",
        parent_index=None,
    )
    questioned_text = "DID_THE_UNCERTAIN_EVENT_HAPPEN"
    questioned_locator = SimpleNamespace(
        beat_index=0,
        char_start=0,
        char_end=len(questioned_text),
        text=questioned_text,
    )
    questioned_proposition = SimpleNamespace(
        locator=questioned_locator,
        semantic_role="embedded_external_proposition",
        parent_index=1,
    )
    questioned_outer = SimpleNamespace(
        locator=questioned_locator,
        semantic_role="outer_private_state",
        parent_index=None,
    )
    not_external = SimpleNamespace(
        locator=questioned_locator,
        decision="not_external_proposition",
        source_relation="not_external_proposition",
        source_refs=(),
    )
    collector = BoundedSourceClosureTraceCollector()

    with capture_isolated_source_closure_trace(collector):
        emit_source_closure_verdict_trace(
            raw_candidate=_candidate(visible=private_text, private="private"),
            propositions=(private_proposition,),
            coverage_findings=(),
        )
        emit_source_closure_verdict_trace(
            raw_candidate=_candidate(visible=questioned_text, private="private"),
            propositions=(questioned_proposition, questioned_outer),
            coverage_findings=(not_external,),
        )

    inventory_empty, coverage_completed = [event.as_dict() for event in collector.snapshot()]
    assert inventory_empty["record_kind"] == "candidate_verdict"
    assert inventory_empty["inventory_outcome"] == "no_external_propositions"
    assert inventory_empty["coverage_outcome"] == "not_run"
    assert inventory_empty["proposition_role_counts"] == {
        "outer_private_state": 1,
    }
    assert (
        inventory_empty["locators"][0]["text_sha256"]
        == sha256(private_text.encode("utf-8")).hexdigest()
    )

    assert coverage_completed["inventory_outcome"] == "external_propositions"
    assert coverage_completed["coverage_outcome"] == "completed"
    assert coverage_completed["coverage"][0]["decision"] == "not_external_proposition"
    assert coverage_completed["coverage"][0]["locator_index"] == 0
    assert coverage_completed["coverage"][0]["source_relation"] == "not_external_proposition"
    encoded = json.dumps(
        [inventory_empty, coverage_completed],
        ensure_ascii=False,
    )
    assert private_text not in encoded
    assert questioned_text not in encoded
    assert "private_turn_state" not in encoded


def test_success_trace_preserves_coverage_ordinal_for_same_span_semantic_units() -> None:
    """A private wrapper and embedded fact sharing text retain distinct coordinates."""

    text = "I_REMEMBER_THE_EXTERNAL_EVENT"
    locator = SimpleNamespace(
        beat_index=0,
        char_start=0,
        char_end=len(text),
        text=text,
    )
    private = SimpleNamespace(
        locator=locator,
        semantic_role="immediate_private_state",
        parent_index=None,
    )
    embedded = SimpleNamespace(
        locator=locator,
        semantic_role="embedded_external_proposition",
        parent_index=None,
    )
    private_finding = SimpleNamespace(
        locator=locator,
        decision="closed",
        source_relation="first_person_immediate_private_continuity",
        source_refs=(),
    )
    embedded_finding = SimpleNamespace(
        locator=locator,
        decision="unclosed",
        source_relation="unclosed",
        source_refs=(),
    )
    collector = BoundedSourceClosureTraceCollector()

    with capture_isolated_source_closure_trace(collector):
        emit_source_closure_verdict_trace(
            raw_candidate=_candidate(visible=text, private="private"),
            propositions=(private, embedded),
            coverage_findings=(private_finding, embedded_finding),
        )

    event = collector.snapshot()[0].as_dict()
    assert [finding["locator_index"] for finding in event["coverage"]] == [0, 1]


def test_verdict_trace_excludes_world_unbound_role_from_coverage_ordinals() -> None:
    """A source-irrelevant role stays auditable without shifting Coverage indexes."""

    general_text = "SUMMER_RAIN_IS_SUDDEN"
    factual_text = "COUNTERPART_IS_ALREADY_HOME"
    general_locator = SimpleNamespace(
        beat_index=0,
        char_start=0,
        char_end=len(general_text),
        text=general_text,
    )
    factual_locator = SimpleNamespace(
        beat_index=1,
        char_start=0,
        char_end=len(factual_text),
        text=factual_text,
    )
    generalization = SimpleNamespace(
        locator=general_locator,
        semantic_role="world_unbound_generalization",
        parent_index=None,
    )
    factual = SimpleNamespace(
        locator=factual_locator,
        semantic_role="standalone_external_proposition",
        parent_index=None,
    )
    finding = SimpleNamespace(
        locator=factual_locator,
        decision="unclosed",
        source_relation="unclosed",
        source_refs=(),
    )
    collector = BoundedSourceClosureTraceCollector()

    with capture_isolated_source_closure_trace(collector):
        emit_source_closure_verdict_trace(
            raw_candidate=_candidate(
                visible=f"{general_text}\n{factual_text}",
                private="private",
            ),
            propositions=(generalization, factual),
            coverage_findings=(finding,),
        )

    event = collector.snapshot()[0].as_dict()
    assert event["proposition_role_counts"] == {
        "standalone_external_proposition": 1,
        "world_unbound_generalization": 1,
    }
    assert event["coverage"][0]["locator_index"] == 1


def test_trace_bounds_event_count_and_visible_surface_bytes() -> None:
    collector = BoundedSourceClosureTraceCollector(max_events=1)
    oversized = "字" * 5_000

    with capture_isolated_source_closure_trace(collector):
        for stage in (
            "post_appeal_initial_rejection",
            "post_appeal_corrected_rejection",
        ):
            emit_source_closure_trace(
                stage=stage,
                raw_candidate=_candidate(visible=oversized, private="private"),
                ci=(),
                v=("undeclared_external_assertion",),
                p=(),
            )

    events = collector.snapshot()
    assert len(events) == 1
    assert collector.dropped_count == 1
    assert events[0].visible_text_truncated is True
    assert len(events[0].visible_beat_texts[0].encode("utf-8")) <= 8_192


def test_wire_failure_trace_keeps_only_stable_coordinate_and_candidate_hash() -> None:
    collector = BoundedSourceClosureTraceCollector()
    candidate = _candidate(
        visible="RAW_CANDIDATE_MUST_NOT_BE_RETAINED",
        private="PRIVATE_STATE_MUST_NOT_BE_RETAINED",
    )

    with capture_isolated_source_closure_trace(collector):
        emit_source_closure_wire_failure_trace(
            raw_candidate=candidate,
            stage="coverage",
            code="locator_index_set_mismatch",
            field="findings.locator_index",
        )

    payload = collector.snapshot()[0].as_dict()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload == {
        "record_kind": "wire_failure",
        "candidate_sha256": sha256(candidate.encode("utf-8")).hexdigest(),
        "stage": "coverage",
        "code": "locator_index_set_mismatch",
        "field": "findings.locator_index",
    }
    assert "RAW_CANDIDATE" not in encoded
    assert "PRIVATE_STATE" not in encoded
    assert "source_ref" not in encoded


def test_candidate_materialization_failure_trace_is_text_free_and_field_only() -> None:
    collector = BoundedSourceClosureTraceCollector()
    candidate = _candidate(
        visible="VISIBLE_DRAFT_MUST_NOT_BE_RETAINED",
        private="PRIVATE_STATE_MUST_NOT_BE_RETAINED",
    )

    with capture_isolated_source_closure_trace(collector):
        emit_source_closure_candidate_materialization_failure_trace(
            raw_candidate=candidate,
            category="private_turn_state",
            code="private_turn_state.field_order",
            field_paths=(
                "private_turn_state",
                "private_turn_state.attended_source_refs",
            ),
        )

    payload = collector.snapshot()[0].as_dict()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload == {
        "record_kind": "candidate_materialization_failure",
        "candidate_sha256": sha256(candidate.encode("utf-8")).hexdigest(),
        "stage": "post_source_acceptance",
        "category": "private_turn_state",
        "code": "private_turn_state.field_order",
        "field_paths": [
            "private_turn_state",
            "private_turn_state.attended_source_refs",
        ],
    }
    assert "VISIBLE_DRAFT" not in encoded
    assert "PRIVATE_STATE" not in encoded
    assert "inner_state_summary" not in encoded
    assert "secret-context-ref" not in encoded
    assert "reviewer_reason" not in encoded


def test_candidate_materialization_failure_trace_drops_explanatory_or_unsafe_coordinates() -> None:
    collector = BoundedSourceClosureTraceCollector()
    candidate = _candidate(visible="visible", private="private")

    with capture_isolated_source_closure_trace(collector):
        emit_source_closure_candidate_materialization_failure_trace(
            raw_candidate=candidate,
            category="expression_draft_schema",
            code="ValidationError: REVIEWER_REASON_MUST_NOT_BE_RETAINED",
            field_paths=("expression_draft.beats",),
        )
        emit_source_closure_candidate_materialization_failure_trace(
            raw_candidate=candidate,
            category="expression_draft_schema",
            code="expression_draft.invalid_field",
            field_paths=("expression_draft.beats[用户私有内容]",),
        )

    assert collector.snapshot() == ()


def test_wire_failure_trace_can_retain_only_allowlisted_isolated_provider_fields() -> None:
    """An explicit audit can diagnose exhausted wires without retaining prompts or private state."""

    collector = BoundedSourceClosureTraceCollector()
    candidate = _candidate(
        visible="VISIBLE_CANDIDATE",
        private="PRIVATE_CANDIDATE_MUST_NOT_BE_RETAINED",
    )
    inventory_raw = json.dumps(
        {
            "contract": "candidate-external-proposition-inventory.5",
            "propositions": [
                {
                    "locator": {
                        "beat_index": 0,
                        "char_start": 0,
                        "char_end": len("VISIBLE_CANDIDATE"),
                        "text": "VISIBLE_CANDIDATE",
                    },
                    "semantic_role": "standalone_external_proposition",
                    "forbidden_echo": "PINNED_CONTEXT_MUST_NOT_BE_RETAINED",
                }
            ],
            "private_turn_state": "PRIVATE_PROVIDER_ECHO_MUST_NOT_BE_RETAINED",
        },
        ensure_ascii=False,
    )
    coverage_raw = json.dumps(
        {
            "contract": "candidate-external-proposition-coverage.4",
            "inventory_complete": False,
            "findings": [],
            "missing_findings": [
                {
                    "locator": {
                        "beat_index": 0,
                        "char_start": 0,
                        "char_end": len("VISIBLE_CANDIDATE"),
                        "text": "VISIBLE_CANDIDATE",
                    },
                    "semantic_role": "standalone_external_proposition",
                    "forbidden_echo": "PINNED_CONTEXT_MUST_NOT_BE_RETAINED",
                }
            ],
        },
        ensure_ascii=False,
    )

    with capture_isolated_source_closure_trace(collector):
        emit_source_closure_wire_failure_trace(
            raw_candidate=candidate,
            stage="coverage",
            code="not_external_relation_mismatch",
            field="findings",
            provider_attempts=(
                ("inventory", inventory_raw),
                ("coverage", coverage_raw),
            ),
        )

    payload = collector.snapshot()[0].as_dict()
    encoded = json.dumps(payload, ensure_ascii=False)
    assert payload["provider_attempts"] == [
        {
            "stage": "inventory",
            "attempt_ordinal": 1,
            "wire_sha256": sha256(inventory_raw.encode("utf-8")).hexdigest(),
            "extraction": "available",
            "wire": {
                "contract": "candidate-external-proposition-inventory.5",
                "propositions": [
                    {
                        "locator": {
                            "beat_index": 0,
                            "char_start": 0,
                            "char_end": len("VISIBLE_CANDIDATE"),
                            "text": "VISIBLE_CANDIDATE",
                        },
                        "semantic_role": "standalone_external_proposition",
                    }
                ],
            },
        },
        {
            "stage": "coverage",
            "attempt_ordinal": 2,
            "wire_sha256": sha256(coverage_raw.encode("utf-8")).hexdigest(),
            "extraction": "available",
            "wire": {
                "contract": "candidate-external-proposition-coverage.4",
                "inventory_complete": False,
                "findings": [],
                "missing_findings": [
                    {
                        "locator": {
                            "beat_index": 0,
                            "char_start": 0,
                            "char_end": len("VISIBLE_CANDIDATE"),
                            "text": "VISIBLE_CANDIDATE",
                        },
                        "semantic_role": "standalone_external_proposition",
                    }
                ],
            },
        },
    ]
    assert "PRIVATE_CANDIDATE" not in encoded
    assert "PINNED_CONTEXT" not in encoded
    assert "PRIVATE_PROVIDER_ECHO" not in encoded
    assert "RAW_AUTHORITY_REF" not in encoded
    assert "source_refs" not in encoded


async def _capture_one(
    *,
    visible: str,
    ready: asyncio.Event,
    release: asyncio.Event,
) -> tuple[str, ...]:
    collector = BoundedSourceClosureTraceCollector()
    with capture_isolated_source_closure_trace(collector):
        ready.set()
        await release.wait()
        emit_source_closure_trace(
            stage="appeal_cleared_initial_rejection",
            raw_candidate=_candidate(visible=visible, private="private"),
            ci=(),
            v=("undeclared_external_assertion",),
            p=(),
        )
    return collector.snapshot()[0].visible_beat_texts


async def _wait_for_both(first: asyncio.Event, second: asyncio.Event) -> None:
    await first.wait()
    await second.wait()


async def _run_isolated_contexts() -> tuple[tuple[str, ...], tuple[str, ...]]:
    first_ready = asyncio.Event()
    second_ready = asyncio.Event()
    release = asyncio.Event()
    first = asyncio.create_task(_capture_one(visible="first", ready=first_ready, release=release))
    second = asyncio.create_task(
        _capture_one(visible="second", ready=second_ready, release=release)
    )
    await _wait_for_both(first_ready, second_ready)
    release.set()
    return await asyncio.gather(first, second)


def test_concurrent_capture_scopes_do_not_cross_contaminate() -> None:
    first, second = asyncio.run(_run_isolated_contexts())

    assert first == ("first",)
    assert second == ("second",)
