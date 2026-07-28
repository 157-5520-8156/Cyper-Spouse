"""Test-only overlay for the pre-ADR-0012 authored LifeAuthor stories."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml


def _outcome(outcome_id: str, text: str, **extra: object) -> dict[str, object]:
    return {"id": outcome_id, "text": text, "privacy": "private", **extra}


def _opening(
    opening_id: str,
    activity_kind: str,
    *,
    location_id: str | None = None,
    windows: list[str],
    outcomes: list[dict[str, object]],
    **extra: object,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": opening_id,
        "activity_kind": activity_kind,
        "source": "routine",
        "domain": "rest_recovery",
        "social_shape": "alone",
        "deviation": "persist",
        "visual_potential": "none",
        "privacy": "private",
        "local_windows": windows,
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
        "duration_minutes": 45,
        "importance_bp": 5000,
        "outcomes": outcomes,
    }
    if location_id is not None:
        result["location_id"] = location_id
    result.update(extra)
    return result


def write_legacy_story_seed(path: Path) -> Path:
    """Merge removed fixed plots into a temporary fixture, never production."""

    raw = yaml.safe_load(Path("configs/world_seed.yaml").read_text(encoding="utf-8"))
    catalog = deepcopy(raw["life_author_catalog"])
    catalog["version"] = "test-only-legacy-story.1"
    catalog["completed_life_arc_continuities"] = [
        {
            "source_context_pack_ref": "life-context:city-publisher-junior-editor",
            "completion_context_tags": ["role:editor", "workplace:city_publisher"],
        }
    ]
    catalog["locations"].extend(
        [
            {
                "id": "publishing-studio",
                "location_ref": "location:shanghai-publishing-studio",
                "privacy": "personal",
                "local_windows": ["08:30-20:00"],
                "weekdays": [0, 1, 2, 3, 4],
                "requires_all_context_tags": ["role:intern", "workplace:publishing"],
            },
            {
                "id": "city-art-museum",
                "location_ref": "location:shanghai-city-art-museum",
                "privacy": "personal",
                "local_windows": ["09:00-17:00"],
                "weekdays": [5, 6],
            },
            {
                "id": "graduate-career-center",
                "location_ref": "location:shanghai-graduate-career-center",
                "privacy": "shareable",
                "local_windows": ["09:00-17:30"],
                "weekdays": [0, 1, 2, 3, 4],
                "requires_all_context_tags": ["academic:graduated"],
            },
            {
                "id": "city-publisher-office",
                "location_ref": "location:shanghai-city-publisher-office",
                "privacy": "personal",
                "local_windows": ["08:30-20:00"],
                "weekdays": [0, 1, 2, 3, 4],
                "requires_all_context_tags": ["workplace:city_publisher"],
            },
        ]
    )
    catalog["npcs"].extend(
        [
            {
                "id": "editor-qin",
                "npc_id": "editor-qin",
                "stable_identity_ref": "reviewed-person:editor-qin",
                "known_trait_refs": ["trait:publishing-editor"],
                "privacy": "personal",
                "location_id": "publishing-studio",
                "local_windows": ["09:00-18:30"],
                "weekdays": [0, 1, 2, 3, 4],
                "requires_all_context_tags": ["role:intern", "workplace:publishing"],
            },
            {
                "id": "recruiter-he",
                "npc_id": "recruiter-he",
                "stable_identity_ref": "reviewed-person:recruiter-he",
                "known_trait_refs": ["trait:publishing-recruiter"],
                "privacy": "personal",
                "location_id": "graduate-career-center",
                "local_windows": ["09:30-17:00"],
                "weekdays": [0, 1, 2, 3, 4],
                "requires_all_context_tags": ["academic:graduated"],
            },
            {
                "id": "senior-editor-luo",
                "npc_id": "senior-editor-luo",
                "stable_identity_ref": "reviewed-person:senior-editor-luo",
                "known_trait_refs": ["trait:senior-editor"],
                "privacy": "personal",
                "location_id": "city-publisher-office",
                "local_windows": ["09:00-18:30"],
                "weekdays": [0, 1, 2, 3, 4],
                "requires_all_context_tags": ["workplace:city_publisher"],
            },
        ]
    )
    next(item for item in catalog["npcs"] if item["id"] == "fan-yuan")[
        "future_location_ids"
    ] = ["city-art-museum"]
    next(
        item
        for item in catalog["future_openings"]
        if item["id"] == "future-fanyuan-exhibition"
    )["location_id"] = "city-art-museum"
    catalog["openings"].extend(
        [
            _opening(
                "family-home-morning-settle",
                "routine.family_home_morning_settle",
                location_id="jiaxing-family-home",
                windows=["07:00-10:00"],
                outcomes=[_outcome("family-morning-steady", "在家慢慢收拾好早上的状态。")],
                domain="hygiene_private",
                visual_potential="ambient",
                duration_minutes=30,
            ),
            _opening(
                "family-home-prepare-for-bed",
                "sleep.family_home_prepare_for_bed",
                location_id="jiaxing-family-home",
                windows=["22:30-00:30"],
                outcomes=[
                    _outcome("family-bedtime-settled", "回房间准备休息了。"),
                    _outcome("family-bedtime-delayed", "又聊了几句才准备休息。"),
                ],
                domain="sleep_wake",
                visual_potential="ambient",
                duration_minutes=35,
                max_per_local_day=1,
            ),
            _opening(
                "family-home-late-wind-down",
                "sleep.family_home_late_wind_down",
                location_id="jiaxing-family-home",
                windows=["00:30-04:00"],
                outcomes=[_outcome("family-late-finally-settled", "终于准备睡了。")],
                source="aftermath",
                domain="sleep_wake",
                visual_potential="ambient",
                duration_minutes=40,
            ),
            _opening(
                "family-home-early-wake",
                "sleep.family_home_early_wake",
                location_id="jiaxing-family-home",
                windows=["04:00-07:30"],
                outcomes=[_outcome("family-early-dozed-again", "醒了一下又睡了回去。")],
                domain="sleep_wake",
                visual_potential="ambient",
                duration_minutes=25,
            ),
            _opening(
                "shanghai-home-evening-settle",
                "home.shanghai_evening_settle",
                location_id="shanghai-home",
                windows=["18:00-23:00"],
                outcomes=[_outcome("shanghai-home-evening-unwound", "回到住处慢慢安静下来。")],
                requires_all_context_tags=["residence:shanghai_home"],
            ),
            _opening(
                "publishing-intern-shift",
                "work.publishing_shift",
                location_id="publishing-studio",
                windows=["09:00-12:00", "13:30-18:00"],
                outcomes=[_outcome("publishing-shift-flow", "把一批稿件顺了一遍。")],
                source="intentional_goal",
                domain="work_career",
                privacy="personal",
                requires_all_context_tags=["role:intern", "workplace:publishing"],
            ),
            _opening(
                "publishing-editor-check-in",
                "work.publishing_editor_check_in",
                location_id="publishing-studio",
                windows=["09:30-11:30", "14:00-17:00"],
                outcomes=[_outcome("editor-check-in-clear", "和秦编辑过了一遍稿件。")],
                source="social",
                domain="work_career",
                social_shape="npc",
                npc_id="editor-qin",
                privacy="personal",
                requires_all_context_tags=["role:intern", "workplace:publishing"],
            ),
            _opening(
                "publishing-intern-interview",
                "career.publishing_intern_interview",
                location_id="campus-library",
                windows=["10:00-12:00", "14:00-17:30"],
                outcomes=[
                    _outcome(
                        "publishing-interview-offer",
                        "决定接下编辑助理工作。",
                        life_arc_effect={
                            "arc_kind": "employment",
                            "context_pack_ref": "life-context:publishing-internship",
                            "context_tags": ["role:intern", "workplace:publishing"],
                            "duration_days": 30,
                            "privacy": "personal",
                        },
                    ),
                    _outcome("publishing-interview-no-fit", "没有接下这份工作。"),
                ],
                source="environmental_opportunity",
                domain="work_career",
                privacy="personal",
                weekdays=[0, 1, 2, 3, 4],
                requires_all_context_tags=["academic:enrolled", "calendar:summer_break"],
                excludes_context_tags=["role:intern"],
            ),
            _opening(
                "graduate-job-search",
                "career.publishing_job_search",
                location_id="graduate-career-center",
                windows=["09:30-12:00", "13:30-17:00"],
                outcomes=[
                    _outcome(
                        "graduate-job-offer",
                        "接下初级编辑工作。",
                        life_arc_effect={
                            "arc_kind": "employment",
                            "context_pack_ref": "life-context:city-publisher-junior-editor",
                            "context_tags": ["role:junior_editor", "workplace:city_publisher"],
                            "duration_days": 180,
                            "privacy": "personal",
                        },
                    ),
                    _outcome("graduate-job-followup", "准备继续看看别的机会。"),
                ],
                source="intentional_goal",
                domain="work_career",
                social_shape="npc",
                npc_id="recruiter-he",
                privacy="personal",
                weekdays=[0, 1, 2, 3, 4],
                requires_all_context_tags=["academic:graduated"],
                excludes_context_tags=["role:junior_editor", "workplace:city_publisher"],
            ),
            _opening(
                "junior-editor-workday",
                "work.junior_editor_day",
                location_id="city-publisher-office",
                windows=["09:00-12:00", "13:30-18:30"],
                outcomes=[_outcome("junior-editor-steady", "跟着资深编辑过完一轮稿件。")],
                source="intentional_goal",
                domain="work_career",
                social_shape="npc",
                npc_id="senior-editor-luo",
                privacy="personal",
                weekdays=[0, 1, 2, 3, 4],
                requires_all_context_tags=["role:junior_editor", "workplace:city_publisher"],
            ),
            _opening(
                "city-publisher-editor-workday",
                "work.city_publisher_editor_day",
                location_id="city-publisher-office",
                windows=["09:00-12:00", "13:30-18:30"],
                outcomes=[_outcome("editor-workday-steady", "独立推进完一轮稿件。")],
                source="intentional_goal",
                domain="work_career",
                social_shape="npc",
                npc_id="senior-editor-luo",
                privacy="personal",
                weekdays=[0, 1, 2, 3, 4],
                requires_all_context_tags=["role:editor", "workplace:city_publisher"],
            ),
        ]
    )
    catalog["future_openings"].extend(
        [
            _opening(
                "future-contextual-destination-research",
                "travel.destination_research",
                windows=["13:00-17:30", "19:30-22:30"],
                outcomes=[
                    _outcome("destination-research-useful", "把路线和现实条件查清楚了一些。"),
                    _outcome("destination-research-paused", "资料还不够，没有当成已经定下的行程。"),
                ],
                source="intentional_goal",
                domain="digital_leisure",
                visual_potential="object",
                advance_days_min=1,
                advance_days_max=5,
            ),
            _opening(
                "future-jiaxing-homecoming",
                "travel.jiaxing_homecoming",
                location_id="jiaxing-family-home",
                windows=["10:00-18:00"],
                outcomes=[
                    _outcome(
                        "jiaxing-homecoming-stay",
                        "回嘉兴后决定在家住两晚。",
                        life_arc_effect={
                            "arc_kind": "travel",
                            "context_pack_ref": "life-context:jiaxing-family-home-stay",
                            "context_tags": [
                                "residence:temporary_family_home_jiaxing",
                                "travel:visiting_jiaxing",
                            ],
                            "duration_days": 3,
                            "privacy": "private",
                        },
                    ),
                    _outcome("jiaxing-homecoming-daytrip", "晚上按原计划回了上海。"),
                ],
                source="intentional_goal",
                domain="family_roommate_friend",
                weekdays=[5, 6],
                duration_minutes=120,
                advance_days_min=2,
                advance_days_max=7,
            ),
        ]
    )
    raw["life_author_catalog"] = catalog
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path
