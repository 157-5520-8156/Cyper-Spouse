"""Offline calibration run for the v2 relationship evaluation draft.

Reads real accepted appraisals plus surrounding verified user messages from
the production ledger (read-only), rebuilds the draft capsule the way the v2
adapter does, and asks the real background-model route for a suggestion.
Nothing is written anywhere.

Usage:
    .venv/bin/python scripts/offline_relationship_draft_audit.py
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion_daemon.llm import (  # noqa: E402
    DeepSeekChatModel,
    FailoverChatModel,
    OpenAICompatibleChatModel,
)
from companion_daemon.world_v2.relationship_evaluation_draft import (  # noqa: E402
    RelationshipEvaluationDraftAdapter,
    RelationshipEvaluationDraftCapsule,
)

WORLD_ID = "world:companion-v2:qq-c2c:geoff"
DB = "file:data/companion.sqlite?mode=ro"
STRANGER = {
    "stage": "stranger",
    "variables": {
        "trust_bp": 0,
        "closeness_bp": 0,
        "respect_bp": 0,
        "reliability_bp": 0,
        "mutuality_bp": 0,
        "repair_confidence_bp": 0,
    },
    "temperature": "ordinary",
}


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"')
    return values


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _rows(query: str, *args: object) -> list:
    conn = sqlite3.connect(DB, uri=True)
    rows = conn.execute(query, args).fetchall()
    conn.close()
    return rows


def _appraisals() -> list[dict[str, object]]:
    results = []
    for (event_json,) in _rows(
        "SELECT event_json FROM world_v2_events WHERE world_id=? AND "
        "json_extract(event_json,'$.event_type')='AppraisalAccepted' ORDER BY ledger_sequence",
        WORLD_ID,
    ):
        event = json.loads(event_json)
        payload = json.loads(event["payload_json"])
        appraisal = payload.get("appraisal") or payload
        results.append(
            {
                "logical_time": event["logical_time"],
                "confidence_bp": appraisal.get("confidence_bp"),
                "expires_at": appraisal.get("expires_at"),
                "status": appraisal.get("status", "active"),
                "hypotheses": [
                    {
                        "meaning": item.get("meaning"),
                        "attribution": item.get("attribution"),
                        "controllability": item.get("controllability"),
                        "severity": item.get("severity"),
                        "weight_bp": item.get("weight_bp"),
                    }
                    for item in appraisal.get("hypotheses", [])
                ],
            }
        )
    return results


def _messages() -> list[tuple[str, str]]:
    results = []
    for time_value, text in _rows(
        "SELECT json_extract(event_json,'$.logical_time'), "
        "json_extract(json_extract(event_json,'$.payload_json'),'$.text') "
        "FROM world_v2_events WHERE world_id=? AND "
        "json_extract(event_json,'$.event_type')='ObservationRecorded' ORDER BY ledger_sequence",
        WORLD_ID,
    ):
        if text:
            results.append((time_value, text))
    return results


def _dialogue_before(messages: list[tuple[str, str]], at: str, count: int = 8) -> tuple[str, ...]:
    stamp = datetime.fromisoformat(at.replace("Z", "+00:00"))
    prior = [
        f"counterpart: {text[:180]}"
        for time_value, text in messages
        if datetime.fromisoformat(time_value.replace("Z", "+00:00")) <= stamp
    ]
    return tuple(prior[-count:])


async def main() -> None:
    env = _load_env(Path(".env"))
    primary = DeepSeekChatModel(
        api_key=env["DEEPSEEK_API_KEY"],
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        thinking_enabled=False,
    )
    model = (
        FailoverChatModel(
            primary=primary,
            fallback=OpenAICompatibleChatModel(
                api_key=env["OPENAI_API_KEY"],
                base_url="https://api.openai.com/v1",
                model="gpt-5.6-luna",
                reasoning_effort="none",
                proxy_url=env.get("OPENAI_PROXY_URL") or None,
            ),
        )
        if env.get("OPENAI_API_KEY")
        else primary
    )
    adapter = RelationshipEvaluationDraftAdapter(model=model)
    appraisals = _appraisals()
    messages = _messages()

    # Real production appraisals: neutral pings/tests versus warm, intimate,
    # or self-disclosing moments, in ledger order.
    picks = {
        "neutral-first-chat-question": "2026-07-18T13:48",
        "neutral-online-ping": "2026-07-18T14:03",
        "warm-tired-disclosure": "2026-07-18T14:46",
        "warm-tired-wants-to-talk": "2026-07-18T15:32",
        "intimate-do-you-dislike-me": "2026-07-19T16:35",
    }
    for label, prefix in picks.items():
        appraisal = next(
            (item for item in appraisals if str(item["logical_time"]).startswith(prefix)),
            None,
        )
        if appraisal is None:
            print(f"{label}: appraisal not found for {prefix}")
            continue
        summary = {
            "status": appraisal["status"],
            "confidence_bp": appraisal["confidence_bp"],
            "expires_at": appraisal["expires_at"],
            "hypotheses": appraisal["hypotheses"],
        }
        capsule = RelationshipEvaluationDraftCapsule(
            accepted_appraisal_summary=_canonical(summary),
            relationship_summary=_canonical(STRANGER),
            recent_dialogue_summaries=_dialogue_before(
                messages, str(appraisal["logical_time"])
            ),
        )
        draft = await adapter.deliberate(capsule=capsule)
        if draft.decision == "no_change":
            print(f"{label}: no_change")
        else:
            assert draft.suggested_deltas is not None
            deltas = {
                key: value
                for key, value in draft.suggested_deltas.model_dump().items()
                if value != 0
            }
            print(
                f"{label}: signal code={draft.signal_code} conf={draft.confidence_bp} "
                f"persistence={draft.persistence} rationale={draft.rationale_code} "
                f"deltas={deltas}"
            )


if __name__ == "__main__":
    asyncio.run(main())
