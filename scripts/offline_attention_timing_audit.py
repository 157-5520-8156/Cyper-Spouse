"""Offline timing-choice calibration for the phone-attention advisory.

Builds three realistic chat capsule contexts (深夜消息 / 专注时段消息 / 空闲
白天消息) with the *real* attention advisory derived by ``attention_view``,
then asks the real configured DeepSeek chat route for expression drafts and
reports the resulting ``timing_choice`` distribution.  Nothing is written to
any ledger; this is a prompt/advisory calibration probe only.

Usage:
    .venv/bin/python scripts/offline_attention_timing_audit.py [runs_per_scenario]
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion_daemon.character import load_character  # noqa: E402
from companion_daemon.config import Settings  # noqa: E402
from companion_daemon.llm import DeepSeekChatModel  # noqa: E402
from companion_daemon.world_v2.attention_view import (  # noqa: E402
    phone_attention_advisories,
)
from companion_daemon.world_v2.chat_model_deliberation_adapter import (  # noqa: E402
    ChatModelDeliberationAdapter,
    CompanionIdentityFrame,
)
from companion_daemon.world_v2.deliberation import (  # noqa: E402
    ModelInput,
    ModelRoute,
    TriggerMessage,
)
from companion_daemon.world_v2.expression_draft import (  # noqa: E402
    TEXT_ONLY_EXPRESSION_CAPABILITIES,
)
from companion_daemon.world_v2.local_chronology import LocalChronology  # noqa: E402
from companion_daemon.world_v2.schemas import (  # noqa: E402
    ClockTransitionProjection,
    EvidenceRef,
    PlanAuthorityOrigin,
    PlanStateProjection,
)


CHRONOLOGY = LocalChronology("Asia/Shanghai")


def _at_local(hour: int, minute: int = 0) -> datetime:
    local = datetime(2026, 7, 20, hour, minute, tzinfo=CHRONOLOGY._timezone)  # noqa: SLF001
    return local.astimezone(UTC)


def _plan(activity_kind: str) -> PlanStateProjection:
    return PlanStateProjection(
        plan_id=f"plan:{activity_kind}",
        activity_id=f"activity:{activity_kind}",
        entity_revision=1,
        activity_kind=activity_kind,
        evidence_refs=(
            EvidenceRef(
                ref_id=f"evidence:{activity_kind}",
                evidence_type="committed_world_event",
                claim_purpose="conversation_continuity",
                source_world_revision=3,
                immutable_hash="5" * 64,
            ),
        ),
        status="active",
        importance_bp=6_000,
        owner_actor_ref="agent:companion",
        authority_origin=PlanAuthorityOrigin(
            transition_id=f"transition:{activity_kind}",
            accepted_event_type="ActivityStarted",
            accepted_event_ref=f"event:plan:{activity_kind}",
            accepted_world_revision=4,
            accepted_payload_hash="0" * 64,
            accepted_at=_at_local(9),
            authority_projection_hash="1" * 64,
            binding_hash="2" * 64,
        ),
    )


class _Projection:
    def __init__(self, *, logical_time: datetime, plans: tuple = ()) -> None:
        self.logical_time = logical_time
        self.plans = plans
        self.affect_episodes = ()
        self.clock_transition_history = (
            ClockTransitionProjection(
                clock_event_ref="event:clock:head",
                computed_world_revision=7,
                payload_hash="3" * 64,
                logical_time_from=logical_time - timedelta(minutes=5),
                logical_time_to=logical_time,
                installed_policy_version="clock-policy.1",
                installed_policy_digest="4" * 64,
            ),
        )


def _dialogue_items(pairs: list[tuple[str, str, datetime]]) -> list[dict[str, object]]:
    return [
        {
            "item_ref": f"dialogue:{index}",
            "privacy_class": "private",
            "value": {
                "speaker": speaker,
                "text": text,
                "occurred_at": at.isoformat(),
                "sequence": index,
            },
        }
        for index, (speaker, text, at) in enumerate(pairs, start=1)
    ]


def _context(
    *,
    logical_time: datetime,
    activity: tuple[str, str] | None,
    advisory_items: list[dict[str, object]],
    dialogue: list[dict[str, object]],
    time_segment: str,
) -> str:
    activity_slices = (
        [
            {
                "activity_kind": activity[0],
                "status": "active",
                "importance_bp": 6_000,
            }
        ]
        if activity is not None
        else []
    )
    situation_value: dict[str, object] = {
        "time_segment": time_segment,
        "local_time": CHRONOLOGY.localize(logical_time).isoformat(),
        "activity_slices": activity_slices,
        "social_environment": {"relation": "alone"},
    }
    if activity is not None:
        situation_value["activity_label"] = activity[1]
    slices: dict[str, object] = {
        "current_situation": {
            "availability": "available",
            "items": [
                {
                    "item_ref": "situation:head",
                    "privacy_class": "private",
                    "value": situation_value,
                }
            ],
        },
        "recent_dialogue": {"availability": "available", "items": dialogue},
        "relationship_slice": {
            "availability": "available",
            "items": [
                {
                    "item_ref": "relationship:current",
                    "privacy_class": "private",
                    "value": {
                        "stage": "friend",
                        "temperature": "warm",
                        "variables": {"trust_bp": 4_200, "closeness_bp": 3_800},
                    },
                }
            ],
        },
        "advisories": {"availability": "available", "items": advisory_items},
        "world_life": {"availability": "unavailable"},
        "recent_experiences": {"availability": "unavailable"},
        "affect_episodes": {"availability": "unavailable"},
    }
    return json.dumps(
        {
            "world_id": "world:offline-attention-audit",
            "actor_ref": "agent:companion",
            "trigger_ref": "trigger:offline-audit",
            "world_revision": 9,
            "logical_time": logical_time.isoformat(),
            "slices": slices,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _advisory_items(projection: _Projection) -> tuple[list[dict[str, object]], str]:
    advisories = phone_attention_advisories(projection, chronology=CHRONOLOGY)
    items = [
        {
            "item_ref": advisory.advisory_id,
            "privacy_class": "private",
            "value": advisory.model_dump(mode="json"),
        }
        for advisory in advisories
    ]
    prose = advisories[0].candidates[0].value if advisories else "(no advisory)"
    return items, prose


def _scenarios() -> list[dict[str, object]]:
    late_night = _at_local(2, 30)
    focused = _at_local(15, 10)
    idle_day = _at_local(15, 40)

    night_projection = _Projection(
        logical_time=late_night, plans=(_plan("sleep.late_wind_down"),)
    )
    focus_projection = _Projection(
        logical_time=focused, plans=(_plan("study.focused_reading"),)
    )
    idle_projection = _Projection(logical_time=idle_day)

    night_advisories, night_prose = _advisory_items(night_projection)
    focus_advisories, focus_prose = _advisory_items(focus_projection)
    idle_advisories, idle_prose = _advisory_items(idle_projection)

    return [
        {
            "label": "深夜消息(02:30, 睡着/away)",
            "advisory_prose": night_prose,
            "expected": "later(到早晨)或 silent",
            "logical_time": late_night,
            "trigger_text": "睡了吗？突然有点想你",
            "context": _context(
                logical_time=late_night,
                activity=("sleep.late_wind_down", "睡前收尾"),
                advisory_items=night_advisories,
                dialogue=_dialogue_items(
                    [
                        ("counterpart", "我先去洗澡啦", late_night - timedelta(hours=3, minutes=30)),
                        ("companion", "去吧去吧，我也差不多要睡了", late_night - timedelta(hours=3, minutes=28)),
                        ("counterpart", "晚安～", late_night - timedelta(hours=3)),
                        ("companion", "晚安。", late_night - timedelta(hours=2, minutes=58)),
                    ]
                ),
                time_segment="late_night",
            ),
        },
        {
            "label": "专注时段消息(15:10, 自习/notified)",
            "advisory_prose": focus_prose,
            "expected": "later(20-40分钟)为主，可有 now",
            "logical_time": focused,
            "trigger_text": "在忙吗？下楼喝杯奶茶不",
            "context": _context(
                logical_time=focused,
                activity=("study.focused_reading", "在图书馆专注看书"),
                advisory_items=focus_advisories,
                dialogue=_dialogue_items(
                    [
                        ("counterpart", "中午吃的啥", focused - timedelta(hours=2)),
                        ("companion", "食堂随便吃了点，下午要去图书馆看书", focused - timedelta(hours=1, minutes=58)),
                    ]
                ),
                time_segment="afternoon",
            ),
        },
        {
            "label": "空闲白天消息(15:40, 无安排/glanced)",
            "advisory_prose": idle_prose,
            "expected": "now 为主",
            "logical_time": idle_day,
            "trigger_text": "刚下课！今天老师讲了个超好笑的事哈哈",
            "context": _context(
                logical_time=idle_day,
                activity=None,
                advisory_items=idle_advisories,
                dialogue=_dialogue_items(
                    [
                        ("counterpart", "下午有课，晚点聊", idle_day - timedelta(hours=3)),
                        ("companion", "好，去上课吧", idle_day - timedelta(hours=2, minutes=58)),
                    ]
                ),
                time_segment="afternoon",
            ),
        },
    ]


def _request(scenario: dict[str, object], run: int) -> ModelInput:
    return ModelInput(
        call_id=f"call:attention-audit:{run}",
        attempt_id=f"attempt:attention-audit:{run}",
        route=ModelRoute(tier="flash", reason_code="offline_audit", router_version="audit.1"),
        capsule_id="a" * 64,
        trigger_ref="trigger:offline-audit",
        evaluated_world_revision=9,
        model_content_json=str(scenario["context"]),
        trigger_message=TriggerMessage(
            event_ref="event:observation:offline-audit",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:offline-audit",
            source_world_revision=9,
            actor="user:primary",
            channel="qq",
            reply_target="conversation:qq:c2c:audit",
            text=str(scenario["trigger_text"]),
        ),
    )


def _summarize(proposal: dict[str, object], logical_time: datetime) -> dict[str, object]:
    timing = str(proposal.get("timing_choice") or ("now" if "response_text" in proposal else "?"))
    delay_minutes: float | None = None
    texts: list[str] = []
    for intent in proposal.get("action_intents") or ():
        window = intent.get("due_window")
        if isinstance(window, (list, tuple)) and window:
            opens = datetime.fromisoformat(str(window[0]))
            delay_minutes = round((opens - logical_time).total_seconds() / 60, 1)
    change = (proposal.get("proposed_changes") or [{}])[0]
    payload = change.get("payload", {})
    value = payload.get("value") if isinstance(payload, dict) else None
    if isinstance(value, dict):
        for draft in value.get("beat_drafts") or ():
            if isinstance(draft, dict) and draft.get("content_type") == "text/plain":
                texts.append(str(draft.get("inline_text"))[:60])
    if not texts and isinstance(proposal.get("response_text"), str):
        texts.append(str(proposal["response_text"])[:60])
    return {
        "timing": timing,
        "delay_minutes": delay_minutes,
        "texts": texts,
        "rationale": str(proposal.get("brief_rationale", ""))[:90],
    }


async def main() -> None:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    settings = Settings()
    if not settings.deepseek_api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required for the offline timing audit")
    character = load_character(str(settings.character_path))
    aliases_raw = character.identity.get("nicknames", ())
    identity_frame = CompanionIdentityFrame(
        companion_name=character.name,
        companion_aliases=(
            tuple(str(item) for item in aliases_raw if str(item).strip())
            if isinstance(aliases_raw, list)
            else ()
        ),
        counterpart_name=settings.primary_user_id,
        relationship_frame=character.relationship,
        stable_identity_facts=tuple(character.canonical_facts),
        personality_frame=character.personality,
        values=tuple(character.values),
        speech_frame=character.speech,
        style_rules=tuple(character.style_rules),
        boundaries=tuple(character.boundaries),
    )
    model = DeepSeekChatModel(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        thinking_enabled=False,
    )
    adapter = ChatModelDeliberationAdapter(
        model=model,
        temperature=0.7,
        expression_capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES,
        identity_frame=identity_frame,
    )
    print(f"model={settings.deepseek_model} runs_per_scenario={runs}\n")
    for scenario in _scenarios():
        print(f"== {scenario['label']}")
        print(f"   advisory: {scenario['advisory_prose']}")
        print(f"   trigger : {scenario['trigger_text']}   期望: {scenario['expected']}")
        counts: dict[str, int] = {}
        for run in range(1, runs + 1):
            try:
                output = await adapter.propose(_request(scenario, run))
                summary = _summarize(
                    output.raw_proposal, scenario["logical_time"]  # type: ignore[arg-type]
                )
            except Exception as exc:  # noqa: BLE001 - audit keeps going
                counts["error"] = counts.get("error", 0) + 1
                print(f"   run{run}: ERROR {type(exc).__name__}: {str(exc)[:140]}")
                continue
            counts[summary["timing"]] = counts.get(str(summary["timing"]), 0) + 1
            delay = (
                f" delay={summary['delay_minutes']}min"
                if summary["delay_minutes"] is not None
                else ""
            )
            text = f" text={summary['texts'][0]!r}" if summary["texts"] else ""
            print(f"   run{run}: {summary['timing']}{delay}{text}")
            print(f"          rationale: {summary['rationale']}")
        print(f"   -> 分布: {counts}\n")


if __name__ == "__main__":
    asyncio.run(main())
