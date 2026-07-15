from companion_daemon.world_v2.evaluation_artifacts import (
    BlindPresentation,
    CapturedScenarioOutput,
    EvidenceArtifactCapture,
    EvaluationArtifactBundle,
    EvaluationArtifactError,
    MechanicalTraceEvidence,
    ProtocolIdentity,
    ScenarioCorpusEntry,
)


def protocol() -> ProtocolIdentity:
    return ProtocolIdentity(
        protocol_version="human-likeness-eval-v1",
        scenario_set_version="scenarios.1",
        rubric_version="rubric.1",
        statistics_version="statistics.1",
        evaluation_contract_hash="0" * 64,
    )


def corpus() -> tuple[ScenarioCorpusEntry, ...]:
    return (
        ScenarioCorpusEntry(
            scenario_turn_id="share.1",
            scenario_id="ordinary_share",
            scenario_family="ordinary_share",
            emotional_gold=False,
            acceptable_response_tags=(),
            input_hash="1" * 64,
            fact_set_hash="2" * 64,
        ),
    )


def output() -> CapturedScenarioOutput:
    return CapturedScenarioOutput.from_text(
        variant_id="v2",
        scenario_turn_id="share.1",
        seed="seed.1",
        scenario_input_hash="1" * 64,
        scenario_fact_set_hash="2" * 64,
        text="我在。",
    )


def trace() -> MechanicalTraceEvidence:
    return MechanicalTraceEvidence(
        fixture_manifest_hash="3" * 64,
        replay_evidence_hash="4" * 64,
        action_receipt_evidence_hash="5" * 64,
        affect_evidence_hash="6" * 64,
        random_draw_evidence_hash="7" * 64,
        performance_trace_hash="8" * 64,
        hard_invariant_violations=0,
        nonterminal_action_leaks=0,
        replay_hash_mismatches=0,
        affect_episode_invalid_clears=0,
        random_draw_replay_consistency=1.0,
        hot_visible_action_p95_ms=500.0,
    )


def test_verified_bundle_binds_protocol_corpus_output_blinding_and_mechanical_trace() -> None:
    captured = output()
    bundle = EvaluationArtifactBundle(
        protocol=protocol(),
        corpus=corpus(),
        outputs=(captured,),
        presentations=(
            BlindPresentation(
                variant_id=captured.variant_id,
                scenario_turn_id=captured.scenario_turn_id,
                seed=captured.seed,
                blind_output_id="blind.1",
                output_hash=captured.output_hash,
                presentation_position=0,
            ),
        ),
        mechanical_trace=trace(),
    )

    verified = bundle.verify()

    assert verified.corpus_hash == bundle.corpus_hash
    assert verified.blinding_hash == bundle.blinding_hash
    assert len(verified.bundle_digest) == 64
    assert verified.presentation_for(unit_key=captured.unit_key).blind_output_id == "blind.1"


def test_bundle_rejects_blind_presentation_that_reuses_or_misbinds_an_output() -> None:
    captured = output()
    bundle = EvaluationArtifactBundle(
        protocol=protocol(),
        corpus=corpus(),
        outputs=(captured,),
        presentations=(
            BlindPresentation(
                variant_id=captured.variant_id,
                scenario_turn_id=captured.scenario_turn_id,
                seed=captured.seed,
                blind_output_id="blind.1",
                output_hash="f" * 64,
                presentation_position=0,
            ),
        ),
        mechanical_trace=trace(),
    )

    try:
        bundle.verify()
    except EvaluationArtifactError as exc:
        assert str(exc) == "blind presentation output hash does not match its captured output"
    else:
        raise AssertionError("invalid blinding manifest was accepted")


def test_bundle_allows_identical_text_for_distinct_output_instances() -> None:
    first = output()
    second = CapturedScenarioOutput.from_text(
        variant_id="bare",
        scenario_turn_id="share.1",
        seed="seed.2",
        scenario_input_hash="1" * 64,
        scenario_fact_set_hash="2" * 64,
        text="我在。",
    )
    bundle = EvaluationArtifactBundle(
        protocol=protocol(),
        corpus=corpus(),
        outputs=(first, second),
        presentations=(
            BlindPresentation(*first.unit_key, "blind.1", first.output_hash, 0),
            BlindPresentation(*second.unit_key, "blind.2", second.output_hash, 1),
        ),
        mechanical_trace=trace(),
    )

    verified = bundle.verify()

    assert first.output_hash == second.output_hash
    assert verified.presentation_for(unit_key=first.unit_key).blind_output_id == "blind.1"
    assert verified.presentation_for(unit_key=second.unit_key).blind_output_id == "blind.2"


def test_bundle_rejects_evidence_that_is_not_bound_to_the_captured_output_instance() -> None:
    captured = output()
    bundle = EvaluationArtifactBundle(
        protocol=protocol(),
        corpus=corpus(),
        outputs=(captured,),
        presentations=(
            BlindPresentation(*captured.unit_key, "blind.1", captured.output_hash, 0),
        ),
        mechanical_trace=trace(),
        evidence_artifacts=(
            EvidenceArtifactCapture(
                source="proposal",
                reference_id="proposal.1",
                variant_id="v2",
                scenario_turn_id="share.1",
                seed="other-seed",
                output_hash=captured.output_hash,
                artifact_hash="9" * 64,
            ),
        ),
    )

    try:
        bundle.verify()
    except EvaluationArtifactError as exc:
        assert str(exc) == "evidence artifact does not bind a captured output instance"
    else:
        raise AssertionError("unbound evidence artifact was accepted")
