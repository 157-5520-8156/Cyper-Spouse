"""Phone attention: pure projection derivation of her presence at the phone."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.attention_view import (
    phone_attention_advisories,
    phone_attention_prose,
    phone_attention_reading,
)
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.schemas import (
    AffectComponentProjection,
    AffectDecayProfileProjection,
    AffectEpisodeProjection,
    AffectOrigin,
    AppraisalMeaningRef,
    ClockTransitionProjection,
    EvidenceRef,
    PlanAuthorityOrigin,
    PlanStateProjection,
    affect_decay_config_digest,
)


CHRONOLOGY = LocalChronology("Asia/Shanghai")


def _at_local(hour: int, minute: int = 0) -> datetime:
    """A UTC instant whose Asia/Shanghai civil time has the given hour."""

    local = datetime(2026, 7, 20, hour, minute, tzinfo=CHRONOLOGY._timezone)  # noqa: SLF001
    return local.astimezone(UTC)


def _plan(activity_kind: str, *, status: str = "active", suffix: str = "1") -> PlanStateProjection:
    return PlanStateProjection(
        plan_id=f"plan:{activity_kind}:{suffix}",
        activity_id=f"activity:{activity_kind}:{suffix}",
        entity_revision=1,
        activity_kind=activity_kind,
        evidence_refs=(
            EvidenceRef(
                ref_id=f"observation:{activity_kind}",
                evidence_type="committed_world_event",
                claim_purpose="conversation_continuity",
                source_world_revision=3,
                immutable_hash="5" * 64,
            ),
        ),
        status=status,  # type: ignore[arg-type]
        importance_bp=5_000,
        owner_actor_ref="agent:companion",
        authority_origin=PlanAuthorityOrigin(
            transition_id=f"transition:{activity_kind}:{suffix}",
            accepted_event_type="ActivityStarted",
            accepted_event_ref=f"event:plan:{activity_kind}:{suffix}",
            accepted_world_revision=4,
            accepted_payload_hash="0" * 64,
            accepted_at=_at_local(9),
            authority_projection_hash="1" * 64,
            binding_hash="2" * 64,
        ),
    )


def _clock(logical_time: datetime) -> ClockTransitionProjection:
    return ClockTransitionProjection(
        clock_event_ref="event:clock:head",
        computed_world_revision=7,
        payload_hash="3" * 64,
        logical_time_from=logical_time - timedelta(minutes=5),
        logical_time_to=logical_time,
        installed_policy_version="clock-policy.1",
        installed_policy_digest="4" * 64,
    )


def _withdrawal_episode(
    *, dimension: str = "anger", intensity_bp: int = 7_000, at: datetime
) -> AffectEpisodeProjection:
    meaning = AppraisalMeaningRef(
        appraisal_id="appraisal:1",
        hypothesis_id="meaning:offence",
        source_cluster_ref="cluster:1",
        accepted_change_id="change:appraisal:1",
        accepted_transition_id="transition:appraisal:1",
    )
    component = AffectComponentProjection(
        component_id=f"component:{dimension}",
        dimension=dimension,
        source_cluster_ref="cluster:1",
        appraisal_refs=(meaning,),
        intensity_bp=intensity_bp,
        decay_anchor_intensity_bp=intensity_bp,
        opened_at=at,
        decay_anchor_at=at,
        decay_not_before=at + timedelta(seconds=120),
        last_stimulus_at=at,
        last_updated_at=at,
        decay_profile=AffectDecayProfileProjection(
            half_life_seconds=3_600,
            floor_bp=300,
            delay_seconds=120,
            config_version="affect-decay.1",
            config_digest=affect_decay_config_digest(
                kind="exponential_half_life",
                half_life_seconds=3_600,
                floor_bp=300,
                delay_seconds=120,
                config_version="affect-decay.1",
            ),
        ),
        residue_bp=300,
    )
    return AffectEpisodeProjection(
        episode_id="affect:withdrawal",
        entity_revision=1,
        origin=AffectOrigin(
            change_id="change:affect:withdrawal",
            transition_id="transition:affect:withdrawal",
            policy_refs=("policy:affect.1",),
            matrix_catalog_version="affect-matrix.1",
            accepted_event_ref="event:affect:withdrawal",
        ),
        components=(component,),
        evidence_refs=(
            EvidenceRef(
                ref_id="observation:offence",
                evidence_type="observed_message",
                claim_purpose="private_hypothesis",
            ),
        ),
        opened_at=at,
        updated_at=at,
        status="active",
    )


class _Projection:
    def __init__(
        self,
        *,
        logical_time: datetime,
        plans: tuple = (),
        affect_episodes: tuple = (),
        with_clock: bool = True,
    ) -> None:
        self.logical_time = logical_time
        self.plans = plans
        self.affect_episodes = affect_episodes
        self.clock_transition_history = (_clock(logical_time),) if with_clock else ()


def test_sleep_plan_reads_as_away_and_cites_the_plan_authority() -> None:
    at = _at_local(2, 30)
    view = phone_attention_reading(
        _Projection(logical_time=at, plans=(_plan("sleep.late_wind_down"),)),
        chronology=CHRONOLOGY,
    )
    assert view is not None
    assert view.state == "away"
    assert view.derivation == "sleep_plan"
    assert "睡" in view.prose and "醒来" in view.prose
    assert "event:plan:sleep.late_wind_down:1" in view.source_event_refs
    assert "event:clock:head" in view.source_event_refs


def test_deep_night_idle_strongly_reads_as_away() -> None:
    view = phone_attention_reading(
        _Projection(logical_time=_at_local(3)), chronology=CHRONOLOGY
    )
    assert view is not None
    assert view.state == "away"
    assert view.derivation == "deep_night_idle"
    assert "深夜" in view.prose


def test_deep_night_with_an_active_awake_plan_yields_to_the_ledger() -> None:
    view = phone_attention_reading(
        _Projection(logical_time=_at_local(2), plans=(_plan("study.essay_writing"),)),
        chronology=CHRONOLOGY,
    )
    assert view is not None
    assert view.state == "notified"
    assert view.derivation == "focus_plan"


def test_focused_study_reads_as_notified() -> None:
    view = phone_attention_reading(
        _Projection(logical_time=_at_local(15), plans=(_plan("study.focused_reading"),)),
        chronology=CHRONOLOGY,
    )
    assert view is not None
    assert view.state == "notified"
    assert "专注" in view.prose and "忙完" in view.prose


def test_literature_club_meetup_reads_as_notified() -> None:
    view = phone_attention_reading(
        _Projection(
            logical_time=_at_local(15), plans=(_plan("social.literature_club_meetup"),)
        ),
        chronology=CHRONOLOGY,
    )
    assert view is not None
    assert view.state == "notified"


def test_digital_browse_reads_as_reading() -> None:
    view = phone_attention_reading(
        _Projection(logical_time=_at_local(16), plans=(_plan("leisure.digital_browse"),)),
        chronology=CHRONOLOGY,
    )
    assert view is not None
    assert view.state == "reading"
    assert view.derivation == "phone_plan"


def test_other_engaged_activity_reads_as_glanced() -> None:
    view = phone_attention_reading(
        _Projection(logical_time=_at_local(10), plans=(_plan("leisure.podcast_listen"),)),
        chronology=CHRONOLOGY,
    )
    assert view is not None
    assert view.state == "glanced"
    assert view.derivation == "engaged_plan"


def test_idle_gap_is_disposed_by_local_hour() -> None:
    evening = phone_attention_reading(
        _Projection(logical_time=_at_local(21)), chronology=CHRONOLOGY
    )
    assert evening is not None
    assert (evening.state, evening.derivation) == ("reading", "idle_phone_hours")
    daytime = phone_attention_reading(
        _Projection(logical_time=_at_local(10)), chronology=CHRONOLOGY
    )
    assert daytime is not None
    assert (daytime.state, daytime.derivation) == ("glanced", "idle_daytime")


def test_strong_withdrawal_feeling_reads_as_do_not_disturb() -> None:
    at = _at_local(16)
    view = phone_attention_reading(
        _Projection(
            logical_time=at,
            affect_episodes=(_withdrawal_episode(at=at - timedelta(minutes=30)),),
        ),
        chronology=CHRONOLOGY,
    )
    assert view is not None
    assert view.state == "do_not_disturb"
    assert "不想理" in view.prose or "不太想理" in view.prose
    assert "event:affect:withdrawal" in view.source_event_refs


def test_sleep_outranks_withdrawal_and_mild_feelings_do_not_trigger_dnd() -> None:
    at = _at_local(3)
    asleep = phone_attention_reading(
        _Projection(
            logical_time=at,
            plans=(_plan("sleep.late_wind_down"),),
            affect_episodes=(_withdrawal_episode(at=at),),
        ),
        chronology=CHRONOLOGY,
    )
    assert asleep is not None and asleep.state == "away"
    mild = phone_attention_reading(
        _Projection(
            logical_time=_at_local(16),
            affect_episodes=(
                _withdrawal_episode(at=_at_local(15), intensity_bp=4_000),
            ),
        ),
        chronology=CHRONOLOGY,
    )
    assert mild is not None and mild.state == "glanced"


def test_naive_logical_time_is_rejected_and_missing_time_is_none() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        phone_attention_reading(
            _Projection(logical_time=_at_local(3)).__class__(
                logical_time=datetime(2026, 7, 20, 3, 0)
            ),
            chronology=CHRONOLOGY,
        )

    class _Unstarted:
        logical_time = None
        plans = ()
        affect_episodes = ()
        clock_transition_history = ()

    assert phone_attention_reading(_Unstarted(), chronology=CHRONOLOGY) is None
    assert phone_attention_advisories(_Unstarted(), chronology=CHRONOLOGY) == ()


def test_advisory_is_source_bound_bounded_and_labelled() -> None:
    projection = _Projection(
        logical_time=_at_local(2, 30), plans=(_plan("sleep.late_wind_down"),)
    )
    advisories = phone_attention_advisories(projection, chronology=CHRONOLOGY)
    assert len(advisories) == 1
    advisory = advisories[0]
    assert advisory.kind == "phone_attention"
    assert advisory.producer_version == "attention-view.1"
    assert set(advisory.source_refs) == {
        "event:plan:sleep.late_wind_down:1",
        "event:clock:head",
    }
    assert len(advisory.candidates) == 1
    value = advisory.candidates[0].value
    assert value.startswith("【手机注意力：不在手机旁】")
    assert len(value) <= 256
    assert advisory.expiry == projection.logical_time + timedelta(hours=2)


def test_advisory_without_any_committed_source_is_dropped() -> None:
    projection = _Projection(logical_time=_at_local(3), with_clock=False)
    assert phone_attention_advisories(projection, chronology=CHRONOLOGY) == ()


def test_prose_carries_a_state_label_for_every_state() -> None:
    at = _at_local(15)
    view = phone_attention_reading(
        _Projection(logical_time=at, plans=(_plan("study.focused_reading"),)),
        chronology=CHRONOLOGY,
    )
    assert view is not None
    assert phone_attention_prose(view).startswith("【手机注意力：专注中】")


def test_expression_prompt_teaches_the_later_mechanics_and_restraint() -> None:
    """The timing calibration must reach the provider prompt verbatim enough
    to matter: the phone_attention advisory hook, the later mechanics (host
    delivers after delay_seconds, kept as her private commitment), the
    one-beat deployment limit, and the do-not-perform-busyness restraint."""

    from companion_daemon.world_v2.chat_model_deliberation_adapter import (
        ChatModelDeliberationAdapter,
    )
    from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute

    adapter = ChatModelDeliberationAdapter(model=object())
    request = ModelInput(
        call_id="call:prompt-probe",
        attempt_id="attempt:prompt-probe",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="trigger:prompt-probe",
        evaluated_world_revision=3,
        model_content_json='{"capsule":"authoritative"}',
    )
    messages = adapter._messages(  # noqa: SLF001 - prompt regression seam
        request=request, quick_recovery=False, failure_code=None
    )
    system = messages[0]["content"]
    assert "phone_attention" in system
    assert "【手机注意力：" in system
    assert "later and silent are fully legitimate" in system
    assert "delivers your beats only after delay_seconds" in system
    assert "private commitment" in system
    assert "exactly one text beat" in system
    assert "never perform busyness" in system
    # The recovery prompt stays narrow: no timing latitude in the failsafe.
    recovery = adapter._messages(  # noqa: SLF001
        request=request, quick_recovery=True, failure_code="timeout"
    )[0]["content"]
    assert "later and silent are fully legitimate" not in recovery


def test_overflowing_later_text_beats_merge_into_the_one_beat_contract() -> None:
    """A model that drafts several bubbles for a deferred return must not lose
    the turn to the deployment's one-followup limit: purely-text later beats
    join into one text, while any other shape still fails honestly."""

    import json as _json

    from companion_daemon.world_v2.chat_model_deliberation_adapter import (
        _proposal_from_model_text,
    )
    from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
    from companion_daemon.world_v2.expression_draft import (
        TEXT_ONLY_EXPRESSION_CAPABILITIES,
    )

    request = ModelInput(
        call_id="call:merge-probe",
        attempt_id="attempt:merge-probe",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="trigger:merge-probe",
        evaluated_world_revision=3,
        model_content_json=_json.dumps(
            {"logical_time": _at_local(2, 30).isoformat(), "slices": {}}
        ),
        trigger_message=TriggerMessage(
            event_ref="event:observation:merge",
            event_payload_hash="sha256:" + "c" * 64,
            observation_ref="observation:merge",
            source_world_revision=3,
            actor="user:primary",
            channel="test",
            reply_target="user:primary",
            text="睡了吗？",
        ),
    )
    raw = _json.dumps(
        {
            "timing_choice": "later",
            "delay_seconds": 21_600,
            "expires_after_seconds": 43_200,
            "beats": [
                {"modality": "text", "text": "现在才看到手机"},
                {"modality": "text", "text": "昨晚睡得太死了，怎么啦？"},
            ],
            "stance": "warm_return",
            "brief_rationale": "深夜睡着，早上补看再回。",
            "world_claims": [],
        },
        ensure_ascii=False,
    )
    proposal = _proposal_from_model_text(
        raw=raw,
        request=request,
        capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES,
        quick_recovery=False,
    )
    assert proposal["timing_choice"] == "later"
    payload = _json.loads(proposal["proposed_changes"][0]["payload"]["canonical_json"])
    drafts = payload["beat_drafts"]
    assert len(drafts) == 1
    assert drafts[0]["inline_text"] == "现在才看到手机\n昨晚睡得太死了，怎么啦？"
    assert proposal["action_intents"][0]["kind"] == "followup"

    with pytest.raises(ValueError, match="deferred-effect limit|text modality"):
        _proposal_from_model_text(
            raw=_json.dumps(
                {
                    "timing_choice": "later",
                    "delay_seconds": 1_200,
                    "expires_after_seconds": 7_200,
                    "beats": [
                        {"modality": "typing"},
                        {"modality": "text", "text": "忙完啦"},
                    ],
                    "stance": "warm_return",
                    "brief_rationale": "非纯文本的 later 不做静默合并。",
                    "world_claims": [],
                },
                ensure_ascii=False,
            ),
            request=request,
            capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES,
            quick_recovery=False,
        )
