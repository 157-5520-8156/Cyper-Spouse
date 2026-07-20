"""Offline acceptance run for the v2 fact extraction policy.

Reads every user message ObservationRecorded event from the production ledger
(read-only), replays the new FactObservationProposalAdapter against the real
DeepSeek background model, and reports per-message extraction results.  It
never writes to any ledger; proposals are materialized and discarded.

Usage:
    .venv/bin/python scripts/offline_fact_extraction_audit.py [--limit N]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from companion_daemon.llm import (  # noqa: E402
    DeepSeekChatModel,
    FailoverChatModel,
    OpenAICompatibleChatModel,
)
from companion_daemon.world_v2.fact_draft_adapter import (  # noqa: E402
    FactObservationProposalAdapter,
)
from companion_daemon.world_v2.schemas import Observation, WorldEvent  # noqa: E402

WORLD_ID = "world:companion-v2:qq-c2c:geoff"
DB = "file:data/companion.sqlite?mode=ro"


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"')
    return values


def _observations() -> list[tuple[Observation, WorldEvent]]:
    conn = sqlite3.connect(DB, uri=True)
    rows = conn.execute(
        "SELECT event_json FROM world_v2_events WHERE world_id=? "
        "AND json_extract(event_json,'$.event_type')='ObservationRecorded' "
        "ORDER BY ledger_sequence",
        (WORLD_ID,),
    ).fetchall()
    conn.close()
    pairs = []
    for (event_json,) in rows:
        event = WorldEvent.model_validate_json(event_json)
        observation = Observation.model_validate_json(event.payload_json)
        pairs.append((observation, event))
    return pairs


def _value_from_hash(text: str, value_hash: str) -> str:
    """Recover the exact substring the draft bound (hash-only in the intent)."""

    expected = value_hash.removeprefix("sha256:")
    for start in range(len(text)):
        for end in range(start + 1, min(len(text), start + 256) + 1):
            candidate = text[start:end]
            if hashlib.sha256(candidate.encode()).hexdigest() == expected:
                return candidate
    return "<unrecovered>"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()

    env = _load_env(Path(".env"))
    # Mirror the production background route: DeepSeek flash primary with the
    # OpenAI-compatible fallback used by semantic_chat_composition.
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
    adapter = FactObservationProposalAdapter(model=model)
    pairs = _observations()
    if args.limit:
        pairs = pairs[: args.limit]
    semaphore = asyncio.Semaphore(args.concurrency)

    async def run_one(index: int, observation: Observation, event: WorldEvent):
        if not observation.text:
            return index, observation, "empty", None
        async with semaphore:
            try:
                proposal = await adapter.propose(
                    observation=observation,
                    observation_event=event,
                    source_world_revision=1,
                )
            except ValueError as exc:
                return index, observation, f"invalid-draft: {exc}", None
        if proposal is None:
            return index, observation, "no-change", None
        intent = json.loads(proposal.proposed_changes[0].payload.canonical_json)
        return index, observation, "retained", intent

    results = await asyncio.gather(
        *(run_one(i, obs, ev) for i, (obs, ev) in enumerate(pairs, 1))
    )
    retained = invalid = nochange = empty = 0
    report_lines = []
    for index, observation, status, intent in sorted(results, key=lambda item: item[0]):
        text = (observation.text or "").replace("\n", " ")[:60]
        if status == "retained" and intent is not None:
            retained += 1
            value = _value_from_hash(observation.text or "", intent["value_hash"])
            line = (
                f"{index:3d} | RETAIN  | {text} | {intent['predicate_code']}"
                f" | value={value!r} | conf={intent['confidence_bp']}"
                f" | privacy={intent['privacy_class']}"
            )
        elif status.startswith("invalid-draft"):
            invalid += 1
            line = f"{index:3d} | INVALID | {text} | {status[:120]}"
        elif status == "empty":
            empty += 1
            line = f"{index:3d} | EMPTY   | {text}"
        else:
            nochange += 1
            line = f"{index:3d} | skip    | {text}"
        report_lines.append(line)
    print("\n".join(report_lines))
    total = len(results)
    print(
        f"\ntotal={total} retained={retained} no-change={nochange} "
        f"invalid={invalid} empty={empty} "
        f"extraction_rate={retained / max(1, total - empty):.1%}"
    )


if __name__ == "__main__":
    asyncio.run(main())
