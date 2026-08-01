"""Canonical replay identities for independent Life semantic reviews.

This module deliberately accepts only already-canonical primitive values.  It
has no dependency on the Life runtime, reviewer compiler, ledger, or proposal
reader, so every boundary can share one exact identity formula without an
import cycle.
"""

from __future__ import annotations

import hashlib
import json


SOURCE_REVIEW_SUBJECT_CONTRACT = "life-development-source-review-subject.2"
GENERAL_EVIDENCE_PACKET_CONTRACT = (
    "life-development-general-source-review-evidence-packet.3"
)
NOVEL_ORIGIN_REVIEW_SUBJECT_CONTRACT = (
    "life-development-novel-origin-review-subject.3"
)
NOVEL_EVIDENCE_PACKET_CONTRACT = (
    "life-development-novel-origin-review-evidence-packet.4"
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _current_review_subject_hash(
    *,
    subject_contract: str,
    evidence_packet_contract: str,
    review_request_hashes: tuple[str, ...],
    world_author_raw_output_hash: str,
    capability_manifest_hash: str,
    context_cursor: dict[str, object],
    wake_event_ref: str,
    wake_world_id: str,
    wake_logical_time: str,
) -> str:
    return _digest(
        {
            "contract": subject_contract,
            "evidence_packet_contract": evidence_packet_contract,
            "review_request_hashes": list(review_request_hashes),
            "world_author_raw_output_hash": world_author_raw_output_hash,
            "capability_manifest_hash": capability_manifest_hash,
            "context_cursor": context_cursor,
            "wake_event_ref": wake_event_ref,
            "wake_world_id": wake_world_id,
            "wake_logical_time": wake_logical_time,
        }
    )


def current_source_review_subject_hash(
    *,
    review_request_hashes: tuple[str, ...],
    world_author_raw_output_hash: str,
    capability_manifest_hash: str,
    context_cursor: dict[str, object],
    wake_event_ref: str,
    wake_world_id: str,
    wake_logical_time: str,
) -> str:
    return _current_review_subject_hash(
        subject_contract=SOURCE_REVIEW_SUBJECT_CONTRACT,
        evidence_packet_contract=GENERAL_EVIDENCE_PACKET_CONTRACT,
        review_request_hashes=review_request_hashes,
        world_author_raw_output_hash=world_author_raw_output_hash,
        capability_manifest_hash=capability_manifest_hash,
        context_cursor=context_cursor,
        wake_event_ref=wake_event_ref,
        wake_world_id=wake_world_id,
        wake_logical_time=wake_logical_time,
    )


def current_novel_origin_review_subject_hash(
    *,
    review_request_hashes: tuple[str, ...],
    world_author_raw_output_hash: str,
    capability_manifest_hash: str,
    context_cursor: dict[str, object],
    wake_event_ref: str,
    wake_world_id: str,
    wake_logical_time: str,
) -> str:
    return _current_review_subject_hash(
        subject_contract=NOVEL_ORIGIN_REVIEW_SUBJECT_CONTRACT,
        evidence_packet_contract=NOVEL_EVIDENCE_PACKET_CONTRACT,
        review_request_hashes=review_request_hashes,
        world_author_raw_output_hash=world_author_raw_output_hash,
        capability_manifest_hash=capability_manifest_hash,
        context_cursor=context_cursor,
        wake_event_ref=wake_event_ref,
        wake_world_id=wake_world_id,
        wake_logical_time=wake_logical_time,
    )


def legacy_source_review_subject_hash(
    *,
    world_author_raw_output_hash: str,
    capability_manifest_hash: str,
) -> str:
    return _digest(
        {
            "capability_manifest_hash": capability_manifest_hash,
            "world_author_raw_output_hash": world_author_raw_output_hash,
        }
    )


def legacy_novel_origin_review_subject_hashes(
    *,
    world_author_raw_output_hash: str,
    capability_manifest_hash: str,
) -> frozenset[str]:
    return frozenset(
        _digest(
            {
                "contract": contract,
                "capability_manifest_hash": capability_manifest_hash,
                "world_author_raw_output_hash": world_author_raw_output_hash,
            }
        )
        for contract in (
            "life-development-novel-origin-review.1",
            "life-development-novel-origin-review.2",
        )
    )


__all__ = [
    "GENERAL_EVIDENCE_PACKET_CONTRACT",
    "NOVEL_EVIDENCE_PACKET_CONTRACT",
    "NOVEL_ORIGIN_REVIEW_SUBJECT_CONTRACT",
    "SOURCE_REVIEW_SUBJECT_CONTRACT",
    "current_novel_origin_review_subject_hash",
    "current_source_review_subject_hash",
    "legacy_novel_origin_review_subject_hashes",
    "legacy_source_review_subject_hash",
]
