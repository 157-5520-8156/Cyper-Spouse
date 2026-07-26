"""Read-only recorded-cadence shadow over recent accepted expression proposals."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from statistics import median

from companion_daemon.world_v2.expression_cadence import CadenceDraw, cadence_windows


PROFILES = ("rapid", "conversational", "hesitant", "escalating")


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def _copy_read_only(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as reader, sqlite3.connect(target) as writer:
        reader.backup(writer)


def _plans(connection: sqlite3.Connection, limit: int) -> list[dict[str, object]]:
    rows = connection.execute(
        """
        SELECT event_json
        FROM world_v2_events
        WHERE json_extract(event_json, '$.event_type') = 'ProposalRecorded'
        ORDER BY ledger_sequence DESC
        """
    )
    plans: list[dict[str, object]] = []
    for (event_json,) in rows:
        event = json.loads(event_json)
        audit = json.loads(event["payload_json"])
        proposal_json = audit.get("proposal_json")
        if not isinstance(proposal_json, str):
            continue
        proposal = json.loads(proposal_json)
        changes = proposal.get("proposed_changes", ())
        if len(changes) != 1 or changes[0].get("kind") != "expression_plan_transition":
            continue
        payload = json.loads(changes[0]["payload"]["canonical_json"])
        beats = payload.get("beat_drafts")
        if not isinstance(beats, list) or not beats:
            continue
        plans.append(
            {
                "proposal_id": proposal["proposal_id"],
                "logical_time": event["logical_time"],
                "beat_count": len(beats),
            }
        )
        if len(plans) >= max(limit, 100):
            break
    multi = [item for item in plans if int(item["beat_count"]) > 1]
    singles = [item for item in plans if int(item["beat_count"]) == 1]
    return [*multi, *singles][:limit]


def _simulate(plan: dict[str, object]) -> dict[str, object]:
    proposal_id = str(plan["proposal_id"])
    beat_count = min(8, int(plan["beat_count"]))
    selector = int(hashlib.sha256(proposal_id.encode()).hexdigest(), 16)
    profile = PROFILES[selector % len(PROFILES)] if beat_count > 1 else "conversational"
    draw_ref = "shadow-draw:" + hashlib.sha256(
        f"{proposal_id}:expression-cadence.1".encode()
    ).hexdigest()
    draws = tuple(
        CadenceDraw(
            draw_ref=draw_ref,
            beat_position=position,
            fraction_ppm=int(
                hashlib.sha256(f"{draw_ref}:{position}".encode()).hexdigest(), 16
            )
            % 1_000_001,
        )
        for position in range(2, beat_count + 1)
    )
    origin = datetime.fromisoformat(str(plan["logical_time"]).replace("Z", "+00:00"))
    windows = cadence_windows(
        origin=origin, profile=profile, beat_count=beat_count, draws=draws
    )
    due = [item[0] for item in windows if item is not None]
    gaps: list[float] = []
    previous = origin
    for instant in due:
        gaps.append((instant - previous).total_seconds())
        previous = instant
    return {
        "proposal_id_hash": hashlib.sha256(proposal_id.encode()).hexdigest(),
        "suggested_beat_count": beat_count,
        "cadence_profile": profile,
        "gaps_seconds": gaps,
        "episode_seconds": (due[-1] - origin).total_seconds() if due else 0.0,
        "draw_ref": draw_ref,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("data/companion.sqlite"))
    parser.add_argument(
        "--copy",
        type=Path,
        default=Path("output/recorded-cadence-shadow/companion-copy.sqlite"),
    )
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if args.limit < 20:
        raise ValueError("shadow sample must include at least twenty plans")
    _copy_read_only(args.source, args.copy)
    with sqlite3.connect(f"file:{args.copy.resolve()}?mode=ro", uri=True) as connection:
        plans = _plans(connection, args.limit)
    if len(plans) < 20:
        raise RuntimeError(f"only {len(plans)} expression plans were available")
    first = [_simulate(plan) for plan in plans]
    replay = [_simulate(plan) for plan in plans]
    gaps = [gap for item in first for gap in item["gaps_seconds"]]
    durations = [float(item["episode_seconds"]) for item in first]
    report = {
        "policy_version": "expression-cadence.1",
        "plan_count": len(first),
        "actual_multi_plan_count": sum(
            int(plan["beat_count"]) > 1 for plan in plans
        ),
        "suggested_beat_counts": {
            str(count): sum(item["suggested_beat_count"] == count for item in first)
            for count in range(1, 9)
        },
        "cadence_profiles": {
            profile: sum(item["cadence_profile"] == profile for item in first)
            for profile in PROFILES
        },
        "gap_seconds_p50": _percentile(gaps, 0.5),
        "gap_seconds_p95": _percentile(gaps, 0.95),
        "episode_seconds_p50": median(durations),
        "episode_seconds_p95": _percentile(durations, 0.95),
        "additional_actions": 0,
        "additional_sends": 0,
        "restart_consistent": first == replay,
        "plans": first,
    }
    report_path = args.copy.parent / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in report.items() if key != "plans"}))


if __name__ == "__main__":
    main()
