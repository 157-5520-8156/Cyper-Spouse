"""Replayable character agency decisions over structured world state.

This module chooses a stance, not reply prose.  It is deliberately pure: it
does not read clocks, databases, models, or a random-number generator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from companion_daemon.world_interaction_rules import HARMFUL_INTERACTION_APPRAISALS

Stance = Literal[
    "comply",
    "comply_then_revisit",
    "disagree_gently",
    "refuse_to_affirm",
    "set_boundary",
    "seek_repair",
    "care_despite_hurt",
    "care_override",
    "defer",
    "remain_silent",
    "initiate",
]
SelectionMode = Literal["highest_score", "recorded_weighted"]


@dataclass(frozen=True)
class UserRequest:
    """A user's preference supplied to deliberation, never a hard command."""

    kind: str
    scope: str = "current_turn"
    strength: str = "explicit"
    subject: str | None = None
    expires_after_turns: int = 1

    @classmethod
    def no_advice_now(cls) -> UserRequest:
        return cls(kind="no_advice")

    @classmethod
    def from_text(cls, text: str) -> UserRequest:
        for address in ("宝宝", "宝贝", "老婆", "亲爱的"):
            if any(marker in text for marker in (f"别叫我{address}", f"不要叫我{address}")):
                return cls(kind="avoid_address", subject=address)
        if any(
            marker in text
            for marker in ("别劝", "别只劝", "不要劝", "不要只劝", "不用讲道理")
        ):
            return cls.no_advice_now()
        if any(marker in text for marker in ("别问", "不要问")):
            return cls(kind="no_questions")
        if any(marker in text for marker in ("自拍", "生活照", "照片", "看看你")):
            return cls(kind="selfie_request")
        if "只听" in text:
            return cls(kind="listen_only")
        return cls(kind="unspecified", strength="implicit")


@dataclass(frozen=True)
class RecordedDraw:
    """A draw already owned by the world ledger, expressed in [0, 9999]."""

    draw_id: str
    basis_points: int

    def __post_init__(self) -> None:
        if not self.draw_id:
            raise ValueError("draw_id must not be empty")
        if not 0 <= self.basis_points <= 9999:
            raise ValueError("basis_points must be between 0 and 9999")


@dataclass(frozen=True)
class StanceCandidate:
    stance: Stance
    score: int
    weight: int


@dataclass(frozen=True)
class CandidateSelection:
    mode: SelectionMode
    candidates: tuple[StanceCandidate, ...]
    chosen_stance: Stance
    draw_id: str | None = None
    draw_basis_points: int | None = None


@dataclass(frozen=True)
class DeliberationDecision:
    appraisal: str
    drives: tuple[tuple[str, int], ...]
    conflicts: tuple[str, ...]
    stances_considered: tuple[Stance, ...]
    chosen_stance: Stance
    display_strategy: str
    action_candidates: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    user_request: UserRequest
    selection: CandidateSelection
    rule_version: str
    causation_ids: tuple[str, ...]


class CharacterDeliberation:
    """Choose an explainable stance from state and an optional recorded draw."""

    RULE_VERSION = "character-deliberation-v2"

    def decide(
        self,
        situation: Mapping[str, object],
        self_core: Mapping[str, object],
        relationship: Mapping[str, object],
        affect: Mapping[str, object],
        needs: Mapping[str, object],
        user_request: UserRequest,
        open_commitments: Sequence[object],
        available_actions: Sequence[str],
        *,
        recorded_draw: RecordedDraw | None = None,
    ) -> DeliberationDecision:
        appraisal, conflicts, scores = self._score_stances(
            situation,
            self_core,
            relationship,
            affect,
            needs,
            user_request,
            open_commitments,
        )
        candidates = tuple(
            StanceCandidate(stance=stance, score=score, weight=max(1, score))
            for stance, score in scores
        )
        chosen = _select(candidates, recorded_draw)
        selection = CandidateSelection(
            mode="recorded_weighted" if recorded_draw else "highest_score",
            candidates=candidates,
            chosen_stance=chosen,
            draw_id=recorded_draw.draw_id if recorded_draw else None,
            draw_basis_points=recorded_draw.basis_points if recorded_draw else None,
        )
        actions = tuple(available_actions)
        drives = (
            ("care", _number(self_core, "care", 50)),
            ("autonomy", _number(self_core, "autonomy", 50)),
            ("irritation", _number(affect, "irritation", 0)),
            ("energy", _number(needs, "energy", 50)),
        )
        return DeliberationDecision(
            appraisal=appraisal,
            drives=drives,
            conflicts=conflicts,
            stances_considered=tuple(candidate.stance for candidate in candidates),
            chosen_stance=chosen,
            display_strategy=_display_strategy(chosen),
            action_candidates=actions,
            rejection_reasons=(),
            user_request=user_request,
            selection=selection,
            rule_version=self.RULE_VERSION,
            causation_ids=_causation_ids(situation),
        )

    def _score_stances(
        self,
        situation: Mapping[str, object],
        self_core: Mapping[str, object],
        relationship: Mapping[str, object],
        affect: Mapping[str, object],
        needs: Mapping[str, object],
        request: UserRequest,
        open_commitments: Sequence[object],
    ) -> tuple[str, tuple[str, ...], tuple[tuple[Stance, int], ...]]:
        open_commitment_count = len(tuple(open_commitments))
        energy = _number(needs, "energy", 50)
        hurt = _number(affect, "hurt", 0)
        boundary = _number(needs, "boundary", 0)
        stage = str(relationship.get("stage", "stranger"))
        trust = _number(relationship, "trust", 0)
        text = str(situation.get("text") or "")
        vulnerable = any(
            marker in text
            for marker in ("撑不住", "崩溃", "害怕", "难受", "救命", "陪我")
        )
        if str(situation.get("kind") or "") == "proactive":
            initiative = _number(needs, "initiative", 50)
            return (
                "self_initiated_contact",
                ("desire_to_connect_vs_respect_attention",),
                (
                    ("initiate", 45 + initiative // 4 + open_commitment_count * 12),
                    ("defer", 35 + max(0, 45 - energy)),
                    (
                        "remain_silent",
                        20 + max(0, 35 - energy) * 2 + boundary // 3,
                    ),
                ),
            )
        appraisal = str(situation.get("appraisal") or "")
        if appraisal in HARMFUL_INTERACTION_APPRAISALS:
            severity = max(1, min(4, int(situation.get("severity") or 3)))
            return (
                "offense_or_coercion",
                ("preserve_dignity_vs_continue_connection",),
                (
                    ("set_boundary", 90 + severity * 12 + boundary // 3),
                    ("refuse_to_affirm", 65 + severity * 8 + hurt // 4),
                    ("defer", 35 + severity * 7 + max(0, 45 - energy)),
                    (
                        "remain_silent",
                        25 + severity * 8 + max(0, 35 - energy) + hurt // 5,
                    ),
                    ("seek_repair", 25 + trust // 10),
                ),
            )
        if appraisal in {"user_withdrawing", "user_confused"}:
            severity = max(1, min(4, int(situation.get("severity") or 2)))
            return (
                "relation_repair_needed",
                ("repair_connection_vs_continue_topic",),
                (
                    ("seek_repair", 110 + severity * 10 + trust // 10),
                    ("care_despite_hurt", 45 + _number(self_core, "care", 50) // 5),
                    ("comply", 20),
                    ("defer", 10 + max(0, 35 - energy)),
                ),
            )
        if request.kind == "avoid_address":
            return "boundary_request", (), (("comply", 100), ("set_boundary", 25))
        if request.kind == "selfie_request":
            closeness_bonus = 35 if stage in {"close_friend", "ambiguous", "lover"} else 0
            return (
                "media_request",
                ("respond_to_request_vs_choose_privacy",),
                (
                    ("comply", 30 + closeness_bonus - hurt - boundary // 2),
                    ("set_boundary", 35 + hurt + boundary),
                    ("defer", 30 + hurt // 2),
                ),
            )
        if request.kind != "no_advice":
            repair_score = (
                30 + hurt // 2 + trust // 10 + open_commitment_count * 30
            )
            care_score = (
                25
                + (55 if vulnerable else 0)
                + _number(self_core, "care", 50) // 5
                + hurt // 6
            )
            return (
                "vulnerable_disclosure" if vulnerable else "ordinary_request",
                (
                    ("care_for_user_vs_preserve_own_hurt",)
                    if vulnerable and hurt > 0
                    else ("repair_vs_self_protection",)
                    if hurt > 0 and open_commitment_count
                    else ()
                ),
                (
                    ("comply", 70),
                    ("seek_repair", repair_score),
                    ("care_despite_hurt", care_score),
                    ("defer", 20 + max(0, 40 - energy)),
                    (
                        "remain_silent",
                        10 + max(0, 30 - energy) * 2 + boundary // 4 + hurt // 5,
                    ),
                ),
            )

        care = _number(self_core, "care", 50)
        directness = _number(self_core, "directness", 50)
        irritation = _number(affect, "irritation", 0)
        close_bonus = 15 if stage in {"close_friend", "ambiguous", "lover"} else 0
        low_energy_bonus = max(0, 45 - energy)
        risk = str(situation.get("risk", "low"))
        if risk in {"high", "imminent"}:
            return (
                "safety_concern",
                ("respect_request_vs_prevent_harm",),
                (("care_override", 120), ("disagree_gently", 45), ("comply", 5)),
            )
        return (
            "care_conflict",
            ("respect_no_advice_request_vs_express_concern",),
            (
                ("comply", 55 + (100 - directness) // 5 + low_energy_bonus),
                ("comply_then_revisit", 48 + (100 - directness) // 8),
                ("disagree_gently", care * 11 // 20 + directness * 9 // 20 + close_bonus),
                ("refuse_to_affirm", 25 + irritation // 3),
                ("defer", 35 + low_energy_bonus * 2),
                (
                    "seek_repair",
                    25 + hurt // 2 + trust // 10 + open_commitment_count * 25,
                ),
                (
                    "care_despite_hurt",
                    20
                    + (50 if vulnerable else 0)
                    + care // 5
                    + hurt // 6,
                ),
                (
                    "remain_silent",
                    40
                    + max(0, 35 - energy) * 3
                    + hurt // 2
                    + boundary // 3,
                ),
            ),
        )


def _select(candidates: tuple[StanceCandidate, ...], draw: RecordedDraw | None) -> Stance:
    if draw is None:
        return max(candidates, key=lambda candidate: candidate.score).stance
    total = sum(candidate.weight for candidate in candidates)
    target = draw.basis_points * total // 10_000
    cumulative = 0
    for candidate in candidates:
        cumulative += candidate.weight
        if target < cumulative:
            return candidate.stance
    return candidates[-1].stance


def _display_strategy(stance: Stance) -> str:
    return {
        "comply": "acknowledge_and_follow_request",
        "comply_then_revisit": "listen_now_revisit_later",
        "disagree_gently": "acknowledge_then_state_one_objection",
        "refuse_to_affirm": "decline_without_escalating",
        "set_boundary": "state_boundary_briefly",
        "seek_repair": "name_the_gap_and_invite_repair",
        "care_despite_hurt": "offer_care_without_erasing_hurt",
        "care_override": "name_concern_and_prioritize_safety",
        "defer": "acknowledge_and_pause",
        "remain_silent": "withhold_expression_without_fabricating_agreement",
        "initiate": "open_a_thread_from_owned_motive",
    }[stance]


def _number(source: Mapping[str, object], key: str, default: int) -> int:
    value = source.get(key, default)
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _causation_ids(situation: Mapping[str, object]) -> tuple[str, ...]:
    value = situation.get("causation_ids", ())
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)
