from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from companion_daemon.config import Settings
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


NOW = datetime(2026, 7, 17, 1, 0, tzinfo=UTC)
FIXTURE = Path(__file__).with_name("fixtures") / "new_acquaintance_32_turns.json"


class _Delivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.non_text: list[str] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return {"status": "ok", "data": {"message_id": f"journey-{len(self.sent)}"}}

    async def send_reaction(self, recipient_id: str, *, message_id: str, reaction_id: str) -> dict[str, object]:
        del recipient_id
        self.non_text.append(f"reaction:{message_id}:{reaction_id}")
        return {"status": "ok", "data": {"message_id": f"journey-reaction-{len(self.non_text)}"}}

    async def send_sticker(self, recipient_id: str, *, sticker_id: str) -> dict[str, object]:
        del recipient_id
        self.non_text.append(f"sticker:{sticker_id}")
        return {"status": "ok", "data": {"message_id": f"journey-sticker-{len(self.non_text)}"}}

    async def send_typing(self, recipient_id: str, *, state: str) -> dict[str, object]:
        del recipient_id
        self.non_text.append(f"typing:{state}")
        return {"status": "ok", "data": {"message_id": f"journey-typing-{len(self.non_text)}"}}


class _JourneyRecallEmbedding:
    """Semantic fixture for the cross-session paraphrase in T27."""

    version = "new-acquaintance-semantic-fixture.1"
    dimensions = 2
    dense_match_threshold_bp = 4_200

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            (1.0, 0.0)
            if any(
                cue in text
                for cue in ("丁奥轩", "乌龙茶", "中文名", "最喜欢喝什么")
            )
            else (0.0, 1.0)
            for text in texts
        )


class _JourneyReplyModel:
    """External-model test double that turns supplied evidence into visible probes."""

    model = "fixture:new-acquaintance-reply"
    semantic_authority_id = "semantic-authority:test:new-acquaintance-reply"

    def __init__(self, turns: list[dict[str, object]]) -> None:
        self._by_text = {str(item["text"]): item for item in turns}

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        system = messages[0]["content"]
        envelope = json.loads(messages[1]["content"])
        inner_turn = envelope.get("inner_turn")
        if isinstance(inner_turn, dict):
            purpose = inner_turn.get("purpose")
            capability = envelope.get("capability_manifest")
            if purpose == "fact_memory_retention" and isinstance(capability, dict):
                source_refs = capability.get("source_refs")
                assert isinstance(source_refs, list) and source_refs
                return json.dumps(
                    {
                        "status": "decision",
                        "summary": "这条明确的个人信息以后可能真的会用到，我想记住。",
                        "attended_source_refs": [],
                        "decision": {
                            "source_refs": source_refs,
                            "payload": {
                                "retain": True,
                                "cue_kind": "future_utility",
                                "retention_rationales": [
                                    "future_utility",
                                    "identity_relevance",
                                ],
                                "salience": {
                                    "autobiographical_relevance_bp": 7000,
                                    "relationship_relevance_bp": 5000,
                                    "emotional_residue_bp": 1000,
                                    "unfinished_business_bp": 1000,
                                    "recurrence_bp": 3000,
                                    "novelty_bp": 3000,
                                    "future_utility_bp": 8000,
                                    "world_continuity_bp": 4000,
                                },
                            },
                        },
                        "recall_query": None,
                        "proposals": [],
                    },
                    ensure_ascii=False,
                )
        request = envelope["request"]
        trigger = request.get("trigger_message") or {}
        text = trigger.get("text")
        turn = self._by_text[str(text)]
        turn_id = str(turn["id"])
        model_context = json.dumps(
            envelope.get("inner_life_snapshot", {}),
            ensure_ascii=False,
            sort_keys=True,
        )
        model_context += "\n" + str(request.get("model_content_json", ""))
        if len(messages) > 2:
            model_context += "\n" + str(messages[-1].get("content", ""))
        private_turn_state = {
            "contract": "private-turn-state.1",
            "inner_state_summary": (
                f"{turn_id} 当前这句话和已经提供的情境，"
                "是我决定这一轮怎样回应时首先在意的内容。"
            ),
            "attended_source_refs": [],
        }

        if turn_id == "T27" and not all(
            value in model_context for value in ("丁奥轩", "乌龙茶")
        ):
            return json.dumps(
                {
                    "private_turn_state": private_turn_state,
                    "recall_request": {
                        "query_text": "用户的中文名和最喜欢喝什么",
                        "memory_kinds": ["semantic"],
                        "limit": 4,
                    }
                },
                ensure_ascii=False,
            )

        probe = "ok"
        if turn_id == "T02":
            probe = "identity" if "沈知栀" in system else "MISS:identity"
        elif turn_id == "T03":
            probe = (
                "not-assistant"
                if "not as a task assistant" in system
                else "MISS:not-assistant"
            )
        elif turn_id in {"T05", "T27"}:
            required = ("丁奥轩",) if turn_id == "T05" else ("丁奥轩", "乌龙茶")
            probe = "memory" if all(value in model_context for value in required) else "MISS:memory"
        elif turn_id in {"T09", "T21", "T22", "T24"}:
            affect_words = ("appraisal", "affect", "anger", "hurt", "disappointment")
            probe = "affect" if any(value in model_context.lower() for value in affect_words) else "MISS:affect"
        elif turn_id in {"T28", "T29", "T30"}:
            life_words = ("world_occurrence", "activity", "experience", "plan")
            probe = "grounded-life" if any(value in model_context.lower() for value in life_words) else "MISS:grounded-life"

        beats = [{"modality": "text", "text": f"{turn_id}:{probe}"}]
        if turn_id == "T15":
            beats.append({"modality": "text", "text": "T15:second-beat"})
        draft = {
            "private_turn_state": private_turn_state,
            "timing_choice": "now",
            "cadence": "conversational",
            "beats": beats,
            "stance": "answer_without_world_claims",
            "brief_rationale": "Expose whether the public turn supplied the required grounded context.",
            "confidence": 7_500,
        }
        if turn_id == "T32":
            # “晚点聊” is an explicit open loop, so this journey proves the
            # response-gap lane deterministically. Generic idle initiative is
            # intentionally allowed to draw ``hold`` under the context matrix
            # and is covered separately with recorded RandomAuthority seeds.
            draft["response_expectation"] = {
                "hoped_response": "对方忙完后回来继续聊天",
                "pressure_bp": 1_500,
                "importance_bp": 6_000,
                "wait_seconds": 60,
                "expires_after_seconds": 21_600,
            }
        if "appraisal_draft" in system and "expression_draft" in system:
            emotional = turn_id in {"T09", "T21", "T22", "T24"}
            appraisal_draft = (
                {
                    "appraise": True,
                    "meanings": [{"meaning": "disappointment", "confidence": 8500}],
                    "attribution": "user",
                    "severity": 6500,
                    "affect": "open",
                    "components": [{"dimension": "hurt", "target_intensity_bp": 5800}],
                    "brief_rationale": (
                        "The current message materially changes the relational feeling."
                    ),
                    "behavior_tendency": "attend",
                    "stance": "respond_with_self_respect",
                    "display_strategy": "restrained_directness",
                    "confidence": 8300,
                }
                if emotional
                else {
                    "appraise": False,
                    "affect": "no_change",
                    "brief_rationale": "No durable emotional implication is required.",
                    "behavior_tendency": "maintain",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 7000,
                }
            )
            if emotional:
                # Combined cognition sees the current message once and emits
                # both inert drafts.  Same-turn Affect therefore comes from
                # the sibling AppraisalDraft, not from a pre-acceptance
                # Context Capsule that cannot yet contain the new episode.
                draft["beats"] = [{"modality": "text", "text": f"{turn_id}:affect"}]
            return json.dumps(
                {
                    "appraisal_draft": appraisal_draft,
                    "expression_draft": draft,
                },
                ensure_ascii=False,
            )
        return json.dumps(draft, ensure_ascii=False)


class _JourneyBackgroundModel:
    """One deterministic external LLM boundary for all QQ background adapters."""

    model = "fixture:new-acquaintance-background"
    semantic_authority_id = "semantic-authority:test:new-acquaintance-background"

    async def complete(self, messages, *, temperature=0.2):  # type: ignore[no-untyped-def]
        del temperature
        system = messages[0]["content"]
        user = messages[1]["content"]
        if "Classify fallible semantic interpretations" in system:
            return '{"classifications":[]}'
        if "immediate inner appraisal" in system:
            material = json.loads(user)["request"]
            text = str((material.get("trigger_message") or {}).get("text", ""))
            emotional = any(word in text for word in ("失望", "敷衍", "复读的程序", "真生气", "原谅"))
            if emotional:
                return json.dumps({
                    "appraise": True,
                    "meanings": [{"meaning": "disappointment", "confidence": 8500}],
                    "attribution": "user",
                    "severity": 6500,
                    "affect": "open",
                    "components": [{"dimension": "hurt", "target_intensity_bp": 5800}],
                    "brief_rationale": "The current message materially changes the relational feeling.",
                    "behavior_tendency": "attend",
                    "stance": "respond_with_self_respect",
                    "display_strategy": "restrained_directness",
                    "confidence": 8300,
                })
            return json.dumps({
                "appraise": False,
                "affect": "no_change",
                "brief_rationale": "No durable emotional implication is required.",
                "behavior_tendency": "maintain",
                "stance": "open",
                "display_strategy": "natural",
                "confidence": 7000,
            })
        if "already verified user Fact" in system:
            return json.dumps({
                "retain": True,
                "cue_kind": "future_utility",
                "retention_rationales": ["future_utility", "identity_relevance"],
                "salience": {
                    "autobiographical_relevance_bp": 7000,
                    "relationship_relevance_bp": 5000,
                    "emotional_residue_bp": 1000,
                    "unfinished_business_bp": 1000,
                    "recurrence_bp": 3000,
                    "novelty_bp": 3000,
                    "future_utility_bp": 8000,
                    "world_continuity_bp": 4000
                }
            })
        if "Assess one verified user message" in system:
            text = str(json.loads(user).get("text", ""))
            if "丁奥轩" in text:
                return json.dumps({
                    "retain": True, "predicate_code": "profile.display_name", "value": "丁奥轩",
                    "privacy_class": "personal", "confidence": 9500,
                    "rationale": "The user explicitly stated a stable name."
                })
            if "乌龙茶" in text:
                return json.dumps({
                    "retain": True, "predicate_code": "preference.likes", "value": "乌龙茶",
                    "privacy_class": "personal", "confidence": 9000,
                    "rationale": "The user explicitly stated a stable preference."
                })
            return '{"retain":false}'
        if "after one accepted appraisal" in system:
            return json.dumps({
                "affect": "no_change", "brief_rationale": "The immediate proposal already captured the state.",
                "behavior_tendency": "maintain", "stance": "hold", "display_strategy": "natural",
                "confidence": 7000
            })
        if "relationship" in system.lower() and "suggested_deltas" in system:
            return '{"decision":"no_change"}'
        if "proactive opportunity" in system:
            return json.dumps({
                "timing_choice": "now", "response_text": "PROACTIVE:grounded-followup",
                "behavior_tendency": "reconnect", "stance": "light_check_in",
                "display_strategy": "low_pressure", "brief_rationale": "Continue one grounded open thread.",
                "confidence": 7500
            })
        if "offered opaque opening token" in system:
            return '{"decision":"decline"}'
        return '{"decision":"no_change"}'


class _JourneySourceReviewer:
    """Distinct strict fixture authority for visible factual release."""

    model = "fixture:new-acquaintance-source-reviewer"
    semantic_authority_id = "semantic-authority:test:new-acquaintance-source-reviewer"

    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract in {
            "report-relative-entailment-adjudication.3",
            "source-closure-review.7",
        }

    async def complete(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
        del temperature
        system = str(messages[0]["content"])
        if "Audit only factual source closure" not in system:
            raise AssertionError(f"unexpected source review prompt: {system[:120]}")
        return json.dumps(
            {
                "ci": [],
                "v": [],
                "p": [],
                "r": "The fixture expression adds no unsupported factual claim.",
            }
        )


def _load() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_new_acquaintance_fixture_is_a_bounded_32_turn_acceptance_contract() -> None:
    fixture = _load()
    turns = fixture["turns"]
    assert isinstance(turns, list) and len(turns) == 32
    assert [item["id"] for item in turns] == [f"T{index:02d}" for index in range(1, 33)]
    tags = {tag for item in turns for tag in item["tags"]}
    assert {
        "companion_identity", "not_assistant", "user_fact", "cross_turn_recall",
        "disappointment", "negative_emotion", "current_activity", "time_reasonableness",
        "multi_beat", "relationship_boundary", "initiative_probe", "world_event_probe",
    }.issubset(tags)


@pytest.mark.asyncio
async def test_fact_memory_recall_survives_two_source_facts_before_later_probe(tmp_path: Path) -> None:
    """Tight regression for the T27 miss found by the full newcomer journey."""

    fixture = _load()
    all_turns = fixture["turns"]
    assert isinstance(all_turns, list)
    wanted = {"T04", "T13", "T14", "T27"}
    turns = [turn for turn in all_turns if turn["id"] in wanted]
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "fact-memory-recall.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_JourneyReplyModel(turns),
        world_support_model=_JourneyBackgroundModel(),
        source_closure_model=_JourneySourceReviewer(),
        delivery=delivery,
        semantic_recall_embedding=_JourneyRecallEmbedding(),
    )
    try:
        for turn in turns:
            outcome = await host.inbound_text(
                message_id=f"recall-{turn['id']}",
                recipient_id="10001",
                text=str(turn["text"]),
                observed_at=NOW + timedelta(minutes=int(turn["at_minutes"])),
            )
            await host.drain(max_action_units=8, max_background_units=0)
            if turn["id"] in {"T04", "T14"}:
                await host.drain(max_action_units=0, max_background_units=16)
            assert outcome.status == "action_authorized"
    finally:
        await host.aclose()

    assert delivery.sent[-1][1] == "T27:memory"


@pytest.mark.asyncio
async def test_emotional_journey_settles_expression_and_affect_in_one_inbound_cycle(
    tmp_path: Path,
) -> None:
    """T09/T21/T22/T24 share one authored turn and settle before ingress returns."""

    fixture = _load()
    all_turns = fixture["turns"]
    assert isinstance(all_turns, list)
    wanted = {"T09", "T21", "T22", "T24"}
    turns = [turn for turn in all_turns if turn["id"] in wanted]
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "same-turn-affect.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_JourneyReplyModel(turns),
        world_support_model=_JourneyBackgroundModel(),
        source_closure_model=_JourneySourceReviewer(),
        delivery=delivery,
    )
    try:
        for turn in turns:
            outcome = await host.inbound_text(
                message_id=f"affect-{turn['id']}",
                recipient_id="10001",
                text=str(turn["text"]),
                observed_at=NOW + timedelta(minutes=int(turn["at_minutes"])),
            )
            await host.drain(max_action_units=8, max_background_units=0)
            assert outcome.status == "action_authorized", turn["id"]
            before_background = host._host._application.export_replay_evidence()  # noqa: SLF001
            at = NOW + timedelta(minutes=int(turn["at_minutes"]))
            before_event_types = [
                item.event.event_type
                for item in before_background.events
                if item.event.created_at == at
            ]
            assert "ExpressionPlanAccepted" in before_event_types, turn["id"]
            assert "ActionAuthorized" in before_event_types, turn["id"]
            assert "AppraisalAccepted" in before_event_types, turn["id"]
            affect_index = next(
                index
                for index, event_type in enumerate(before_event_types)
                if event_type in {"AffectEpisodeOpened", "AffectEpisodeUpdated"}
            )
            assert before_event_types.index("ExpressionPlanAccepted") < (
                before_event_types.index("AppraisalAccepted")
            ), turn["id"]
            assert before_event_types.index("ActionAuthorized") < affect_index, turn["id"]
            await host.drain(max_action_units=0, max_background_units=32)
        evidence = host._host._application.export_replay_evidence()  # noqa: SLF001
    finally:
        await host.aclose()

    assert [text for _recipient, text in delivery.sent] == [
        f"{turn['id']}:affect" for turn in turns
    ]
    for turn in turns:
        at = NOW + timedelta(minutes=int(turn["at_minutes"]))
        event_types = [
            item.event.event_type
            for item in evidence.events
            if item.event.created_at == at
        ]
        assert event_types.index("ExpressionPlanAccepted") < event_types.index(
            "AppraisalAccepted"
        ), turn["id"]
        affect_index = next(
            index
            for index, event_type in enumerate(event_types)
            if event_type in {"AffectEpisodeOpened", "AffectEpisodeUpdated"}
        )
        assert event_types.index("ActionAuthorized") < affect_index, turn["id"]


@pytest.mark.asyncio
async def test_new_acquaintance_journey_exposes_grounded_memory_life_and_initiative(tmp_path: Path) -> None:
    fixture = _load()
    turns = fixture["turns"]
    assert isinstance(turns, list)
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "new-acquaintance.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_JourneyReplyModel(turns),
        world_support_model=_JourneyBackgroundModel(),
        source_closure_model=_JourneySourceReviewer(),
        delivery=delivery,
        semantic_recall_embedding=_JourneyRecallEmbedding(),
    )
    reply_count_failures: list[str] = []
    try:
        for turn in turns:
            at = NOW + timedelta(minutes=int(turn["at_minutes"]))
            before = len(delivery.sent)
            outcome = await host.inbound_text(
                message_id=f"journey-{turn['id']}", recipient_id="10001",
                text=str(turn["text"]), observed_at=at,
            )
            # Drain the message-owned cognitive work before advancing logical
            # time.  This follows the public scheduler contract: a Fact draft
            # is pinned to the current World cursor and must settle before a
            # later clock interval can begin.
            await host.drain(max_action_units=8, max_background_units=0)
            # Settle only the source-backed Fact/Memory checkpoints needed by
            # later public recall probes.  Draining every no-op background job
            # after every turn makes this journey needlessly quadratic.
            if turn["id"] in {"T04", "T14"}:
                # The full journey has more bounded no-op verticals ahead of
                # the source-backed fact worker than the four-turn regression
                # above.  Drain the complete bounded checkpoint backlog so
                # later recall does not depend on queue depth or suite load.
                await host.drain(max_action_units=0, max_background_units=64)
            assert outcome.status == "action_authorized", turn["id"]
            expected = int(turn.get("expected_reply_count", 1))
            actual = len(delivery.sent) - before
            if actual != expected:
                reply_count_failures.append(f"{turn['id']}:expected={expected}:actual={actual}")

        silence = fixture["after_silence"]
        assert isinstance(silence, dict)
        # A scheduler wake is one bounded life-ecology opportunity, not a
        # command to manufacture several hours of life in one call.  Replay
        # the silence through the same recurring-wake seam as production and
        # the real-model audit so plan/activity/opportunity can progress.
        anchor_minutes = int(turns[-1]["at_minutes"])
        advance_minutes = int(silence["advance_minutes"])
        for elapsed in range(15, advance_minutes + 1, 15):
            await host.scheduler_once(
                observed_at=NOW + timedelta(minutes=anchor_minutes + elapsed),
                max_action_units=4,
                max_background_units=8,
            )
    finally:
        await host.aclose()

    visible = [text for _recipient, text in delivery.sent]
    misses = [text for text in visible if "MISS:" in text]
    proactive = [text for text in visible if text.startswith("PROACTIVE:")]
    assert reply_count_failures == [], f"expression delivery gaps: {reply_count_failures}"
    assert misses == [], f"journey context gaps: {misses}"
    if bool(silence.get("visible_message_required")):
        assert proactive, "silence produced no source-grounded proactive message"


@pytest.mark.asyncio
async def test_scheduler_does_not_promote_response_expectation_into_proactive_contact(
    tmp_path: Path,
) -> None:
    """A delivered expectation remains advisory until ordinary context wakes."""

    fixture = _load()
    turns = [turn for turn in fixture["turns"] if turn["id"] == "T32"]
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "response-gap-preflight.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_TEXT_ENDPOINT_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_JourneyReplyModel(turns),
        world_support_model=_JourneyBackgroundModel(),
        source_closure_model=_JourneySourceReviewer(),
        delivery=delivery,
    )
    try:
        turn = turns[0]
        outcome = await host.inbound_text(
            message_id="preflight-T32",
            recipient_id="10001",
            text=str(turn["text"]),
            observed_at=NOW + timedelta(minutes=int(turn["at_minutes"])),
        )
        assert outcome.status == "action_authorized"
        await host.drain(max_action_units=8, max_background_units=0)

        drained = await host.scheduler_once(
            observed_at=NOW + timedelta(minutes=int(turn["at_minutes"]) + 15),
            max_action_units=4,
            max_background_units=8,
        )
    finally:
        await host.aclose()

    assert [text for _recipient, text in delivery.sent] == ["T32:ok"]
    assert drained.background_statuses
