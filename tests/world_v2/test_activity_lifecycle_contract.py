from companion_daemon.world_v2.schema_core import EvidenceRef


def test_life_transition_evidence_purpose_is_available_without_reusing_future_plan() -> None:
    evidence = EvidenceRef(
        ref_id="event:clock:activity-lifecycle",
        evidence_type="committed_world_event",
        claim_purpose="life_transition",
        source_world_revision=7,
        immutable_hash="a" * 64,
    )

    assert evidence.claim_purpose == "life_transition"
