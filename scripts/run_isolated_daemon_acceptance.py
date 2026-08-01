#!/usr/bin/env python3
"""Exercise the real QQ daemon process without any route to a real QQ provider.

The runner starts the installed ``companion_daemon.napcat_cli`` entry point
twice against one temporary World V2 database.  Its configured OneBot API is a
loopback-only capture server owned by this process, so every provider call is
observable and no real QQ send is possible.  Inbound turns still cross the
real HTTP application, scheduler lifespan, durable ingress store, World V2
composition, Action pump, provider receipt parser, and process lock.

The default remains a deterministic reliability acceptance.  An explicit
``--model-mode real-provider --allow-real-provider`` manual mode sends model
requests through a hash-only loopback forwarding proxy while OneBot remains
capture-only.  That mode is observation evidence, never a CI wording gate.
"""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
import sys
import tempfile
from threading import Event, Lock, Thread
import time
from typing import Any, Iterator, Literal
from urllib.parse import urlparse

import httpx
import uvicorn

from companion_daemon.config import Settings
from companion_daemon.llm import DeepSeekChatModel, FakeCompanionModel
from companion_daemon.qq_outbound_owner import (
    QQOutboundOwnerLease,
    qq_outbound_owner_lock_path,
)
from companion_daemon.world_v2.isolated_daemon_acceptance import (
    deterministic_acceptance_exit_code,
    evaluate_deterministic_invariants,
    qualified_full_review_route_models,
    qualified_inventory_route_models,
)
from companion_daemon.world_v2.qq_c2c_host import qq_c2c_world_id
from companion_daemon.world_v2.qq_c2c_onebot_app import create_qq_c2c_onebot_app
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.structured_source_review_model import (
    StrictOutputCapabilityEvidence,
    StructuredSourceReviewModel,
)


_ROOT = Path(__file__).resolve().parents[1]
_RECIPIENT_ID = "10001"
_PRIMARY_USER_ID = "isolated-daemon-acceptance-user"
_VISIBLE_MODALITIES = frozenset({"text", "reaction", "sticker", "media"})
_ModelMode = Literal["fake", "loopback-stub", "real-provider"]
_PROVIDER_MODES = frozenset({"loopback-stub", "real-provider"})
_INTERRUPTION_MARKER = "ISOLATED-INTERRUPTION-FIRST"
_INTERRUPTION_SECOND_MARKER = "ISOLATED-INTERRUPTION-SECOND"
_LOOPBACK_ROLE_MODEL = "isolated-loopback-role"
_LOOPBACK_REVIEW_MODEL = "isolated-loopback-source-reviewer"
_LOOPBACK_ROLE_AUTHORITY = "semantic-authority:test:isolated-loopback-role.1"
_LOOPBACK_REVIEW_AUTHORITY = (
    "semantic-authority:test:isolated-loopback-source-reviewer.1"
)
_LOOPBACK_LIFE_REVIEW_MODEL = "isolated-deterministic-life-source-reviewer"
_LOOPBACK_LIFE_REVIEW_AUTHORITY = (
    "semantic-authority:test:isolated-deterministic-life-source-reviewer.1"
)
_LOOPBACK_REVIEW_CONTRACTS = (
    "report-relative-entailment-adjudication.3",
    "source-closure-review.7",
)
_LOOPBACK_LIFE_REVIEW_CONTRACTS = (
    "life-development-source-closure-review.1",
    "life-development-novel-origin-review.2",
)


def _canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _decoded_json_material(value: object, *, depth: int = 0) -> object:
    """Decode nested provider JSON without retaining it outside one request."""

    if depth >= 6:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                return _decoded_json_material(json.loads(stripped), depth=depth + 1)
            except json.JSONDecodeError:
                return value
        return value
    if isinstance(value, list):
        return [_decoded_json_material(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _decoded_json_material(item, depth=depth + 1) for key, item in value.items()
        }
    return value


def _named_material(
    value: object,
    *,
    names: frozenset[str],
    contains: tuple[str, ...] = (),
) -> list[object]:
    found: list[object] = []

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for raw_key, child in item.items():
                key = str(raw_key).lower()
                if key in names or any(fragment in key for fragment in contains):
                    found.append(child)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _has_source_bound_semantic_material(value: object) -> bool:
    """Return whether a provider-visible view contains inspectable sourced state."""

    ignored_fields = frozenset(
        {
            "attention_source_refs",
            "availability",
            "authority",
            "contract",
            "item_ref",
            "source_ref",
            "source_refs",
        }
    )

    def nonempty(item: object) -> bool:
        if isinstance(item, str):
            return bool(item.strip())
        if isinstance(item, list):
            return any(nonempty(child) for child in item)
        if isinstance(item, dict):
            return any(nonempty(child) for child in item.values())
        return item is not None

    def visit(item: object) -> bool:
        if isinstance(item, dict):
            if item.get("availability") == "unavailable":
                return False
            source_ref = item.get("source_ref")
            if (
                isinstance(source_ref, str)
                and source_ref.strip()
                and any(
                    key not in ignored_fields and nonempty(child) for key, child in item.items()
                )
            ):
                return True
            return any(visit(child) for child in item.values())
        if isinstance(item, list):
            return any(visit(child) for child in item)
        return False

    return visit(value)


def _available_recall_material(value: object) -> list[object]:
    """Return only recall items that the provider could actually inspect."""

    found: list[object] = []
    recall_keys = frozenset(
        {
            "remembered_material",
            "recalled_emotional_associations",
            "recent_self_experiences",
        }
    )

    def inspectable(item: object) -> bool:
        if not isinstance(item, dict) or item.get("availability") == "unavailable":
            return False
        raw_material = item.get("value")
        material = raw_material if isinstance(raw_material, dict) else item
        source_refs = material.get("source_refs")
        source_present = (
            (
                isinstance(material.get("source_ref"), str)
                and bool(str(material["source_ref"]).strip())
            )
            or (isinstance(item.get("item_ref"), str) and bool(str(item["item_ref"]).strip()))
            or (
                isinstance(source_refs, list)
                and any(
                    isinstance(source_ref, str) and bool(source_ref.strip())
                    for source_ref in source_refs
                )
            )
        )
        content_present = any(
            (isinstance(material.get(name), str) and bool(str(material[name]).strip()))
            or (isinstance(material.get(name), list) and bool(material[name]))
            for name in (
                "text",
                "summary",
                "source_excerpts",
            )
        )
        return source_present and content_present

    def visit(item: object) -> None:
        if isinstance(item, dict):
            if item.get("availability") == "unavailable":
                return
            if item.get("recall_injected") is True and inspectable(item):
                found.append(item)
            for raw_key, child in item.items():
                key = str(raw_key).lower()
                if key in recall_keys:
                    if isinstance(child, list):
                        available_items = [
                            candidate for candidate in child if inspectable(candidate)
                        ]
                        if available_items:
                            found.append(available_items)
                    elif isinstance(child, dict):
                        items = child.get("items")
                        if child.get("availability") == "available" and isinstance(items, list):
                            available_items = [
                                candidate for candidate in items if inspectable(candidate)
                            ]
                            if available_items:
                                found.append(available_items)
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return found


def _provider_request_evidence(payload: dict[str, object]) -> dict[str, object]:
    raw_messages = payload.get("messages")
    messages = raw_messages if isinstance(raw_messages, list) else []
    decoded = _decoded_json_material(messages)
    current_self = [
        state
        for state in _named_material(
            decoded,
            names=frozenset({"current_self_state"}),
        )
        if _has_source_bound_semantic_material(state)
    ]
    recall = _available_recall_material(decoded)
    emotion: list[object] = []
    for state in current_self:
        emotion.extend(
            material
            for material in _named_material(
                state,
                names=frozenset(
                    {
                        "affect",
                        "affect_episodes",
                        "active_appraisals",
                        "appraisals",
                        "emotion",
                        "mood",
                    }
                ),
            )
            if _has_source_bound_semantic_material(material)
        )
    joined = "\n".join(
        str(message.get("content") or "") for message in messages if isinstance(message, dict)
    )
    source_closure = (
        "Audit only factual source closure" in joined or "Audit factual source closure" in joined
    )
    authoritative_role_request = (
        "COMBINED OUTPUT ENVELOPE" in joined
        or "exactly two keys: appraisal_draft and expression_draft" in joined
        or (
            "Decide the next expression as the independent person" in joined
            and "This is a provisional first beat" not in joined
        )
    ) and not source_closure
    current_self_hash = _canonical_hash(current_self) if current_self else None
    recall_hash = _canonical_hash(recall) if recall else None
    emotion_hash = _canonical_hash(emotion) if emotion else None
    temperature = payload.get("temperature")
    model_invocation_request_hash = (
        _canonical_hash(
            {
                "messages": messages,
                "temperature": temperature,
            }
        )
        if isinstance(temperature, (int, float)) and not isinstance(temperature, bool)
        else None
    )
    return {
        "request_hash": _canonical_hash(payload),
        "presentation_hash": _canonical_hash(messages),
        "model_invocation_request_hash": model_invocation_request_hash,
        "current_self_state_hash": current_self_hash,
        "recall_context_hash": recall_hash,
        "emotion_context_hash": emotion_hash,
        "source_closure_request": source_closure,
        "authoritative_role_request": authoritative_role_request,
        "contains_interruption_marker": _INTERRUPTION_MARKER in joined,
        "contains_second_interruption_marker": (_INTERRUPTION_SECOND_MARKER in joined),
    }


class _ProviderCaptureState:
    """Hash-only provider boundary used by stub and manual forwarding modes."""

    def __init__(
        self,
        *,
        mode: Literal["loopback-stub", "real-provider"],
        upstream_base_url: str | None,
    ) -> None:
        self.mode = mode
        self.upstream_base_url = (
            upstream_base_url.rstrip("/") if upstream_base_url is not None else None
        )
        self._lock = Lock()
        self._records: list[dict[str, object]] = []
        self._interruption_marker_seen = Event()
        self._second_interruption_marker_seen = Event()
        self._second_interruption_overlapped_first = Event()
        self._first_interruption_provider_active = Event()
        self._first_interruption_tracking_consumed = False
        self._stub_delay_consumed = False

    def wait_for_interruption_marker(self, timeout: float) -> bool:
        return self._interruption_marker_seen.wait(timeout)

    def wait_for_second_interruption_marker(self, timeout: float) -> bool:
        return self._second_interruption_marker_seen.wait(timeout)

    def wait_for_second_interruption_overlap(self, timeout: float) -> bool:
        """Observe overlap atomically at the second provider request boundary."""

        return self._second_interruption_overlapped_first.wait(timeout)

    def snapshot(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(dict(record) for record in self._records)

    def _record(self, payload: dict[str, object]) -> tuple[bool, bool]:
        evidence = _provider_request_evidence(payload)
        should_delay = False
        tracks_first_interruption = False
        with self._lock:
            self._records.append(evidence)
            if (
                evidence["contains_interruption_marker"] is True
                and evidence["authoritative_role_request"] is True
            ):
                self._interruption_marker_seen.set()
                if not self._first_interruption_tracking_consumed:
                    self._first_interruption_tracking_consumed = True
                    tracks_first_interruption = True
                    self._first_interruption_provider_active.set()
                if self.mode == "loopback-stub" and not self._stub_delay_consumed:
                    self._stub_delay_consumed = True
                    should_delay = True
            if (
                evidence["contains_second_interruption_marker"] is True
                and evidence["authoritative_role_request"] is True
            ):
                if self._first_interruption_provider_active.is_set():
                    self._second_interruption_overlapped_first.set()
                self._second_interruption_marker_seen.set()
        return should_delay, tracks_first_interruption

    @staticmethod
    async def _stub_completion(messages: list[dict[str, str]]) -> str:
        joined = "\n".join(message["content"] for message in messages)
        if (
            "COMBINED OUTPUT ENVELOPE" in joined
            or "exactly two keys: appraisal_draft and expression_draft" in joined
        ):
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": True,
                        "affect": "open",
                        "brief_rationale": (
                            "The user's relief feels meaningful enough to leave "
                            "a small warm residue."
                        ),
                        "behavior_tendency": "stay_present",
                        "stance": "warm",
                        "display_strategy": "natural",
                        "confidence": 7200,
                        "meanings": [{"meaning": "social_warmth", "confidence": 7200}],
                        "attribution": "user",
                        "severity": 4200,
                        "components": [
                            {
                                "dimension": "warmth",
                                "target_intensity_bp": 4200,
                            }
                        ],
                    },
                    "expression_draft": {
                        "private_turn_state": {
                            "inner_state_summary": (
                                "Her update catches my attention and leaves me warmly present."
                            ),
                            "attended_source_refs": [],
                        },
                        "timing_choice": "now",
                        "beats": [
                            {
                                "modality": "text",
                                "text": "我在，刚刚这句我有接到。",
                            }
                        ],
                        "cadence": "conversational",
                        "stance": "warm",
                        "brief_rationale": (
                            "Stay with the current turn without inventing history."
                        ),
                        "confidence": 7200,
                        "world_claims": [],
                    },
                },
                ensure_ascii=False,
            )
        return await FakeCompanionModel().complete(messages)

    def handle(
        self,
        *,
        path: str,
        payload: dict[str, object],
        authorization: str,
    ) -> tuple[int, dict[str, object]]:
        if path != "/chat/completions":
            return 404, {"error": {"message": "unsupported capture endpoint"}}
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list) or not all(
            isinstance(message, dict)
            and isinstance(message.get("role"), str)
            and isinstance(message.get("content"), str)
            for message in raw_messages
        ):
            return 400, {"error": {"message": "messages must be role/content objects"}}
        should_delay, tracks_first_interruption = self._record(payload)
        try:
            if should_delay:
                # The marker event is set before this bounded delay. The runner
                # can then drive a second ingress to the provider boundary while
                # this first provider request is observably still active.
                time.sleep(3.0)
            messages = [
                {"role": str(message["role"]), "content": str(message["content"])}
                for message in raw_messages
                if isinstance(message, dict)
            ]
            if self.mode == "loopback-stub":
                content = asyncio.run(self._stub_completion(messages))
                completion_tokens = max(1, len(content) // 4)
                prompt_tokens = max(
                    1,
                    sum(len(message["content"]) for message in messages) // 4,
                )
                return 200, {
                    "id": f"isolated-stub-{secrets.token_hex(8)}",
                    "choices": [{"message": {"role": "assistant", "content": content}}],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                        "prompt_cache_hit_tokens": 0,
                        "prompt_cache_miss_tokens": prompt_tokens,
                        "completion_tokens_details": {"reasoning_tokens": 0},
                    },
                }
            if self.upstream_base_url is None:
                raise RuntimeError("real provider forwarding has no upstream URL")
            with httpx.Client(timeout=90, trust_env=True) as client:
                response = client.post(
                    f"{self.upstream_base_url}/chat/completions",
                    headers={
                        "Authorization": authorization,
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            try:
                response_payload = response.json()
            except ValueError:
                response_payload = {
                    "error": {
                        "message": "upstream provider returned non-JSON content",
                        "status_code": response.status_code,
                    }
                }
            if not isinstance(response_payload, dict):
                response_payload = {"error": {"message": "upstream response was not an object"}}
            return response.status_code, response_payload
        finally:
            if tracks_first_interruption:
                self._first_interruption_provider_active.clear()

    def report(self) -> dict[str, object]:
        records = self.snapshot()

        def hashes(key: str) -> list[str]:
            return list(
                dict.fromkeys(
                    str(record[key]) for record in records if isinstance(record.get(key), str)
                )
            )

        source_records = [record for record in records if record["source_closure_request"] is True]
        current_self_records = [
            record for record in records if isinstance(record.get("current_self_state_hash"), str)
        ]
        recall_records = [
            record for record in records if isinstance(record.get("recall_context_hash"), str)
        ]
        causal_records = [
            record
            for record in records
            if all(
                isinstance(record.get(key), str)
                for key in (
                    "current_self_state_hash",
                    "recall_context_hash",
                    "emotion_context_hash",
                )
            )
        ]
        return {
            "contract": "provider-presentation-capture.1",
            "capture_mode": self.mode,
            "raw_prompt_retained": False,
            "raw_response_retained": False,
            "request_count": len(records),
            "request_hashes": hashes("request_hash"),
            "presentation_hashes": hashes("presentation_hash"),
            "model_invocation_request_hashes": hashes("model_invocation_request_hash"),
            "current_self_state_present_count": sum(
                isinstance(record.get("current_self_state_hash"), str) for record in records
            ),
            "current_self_state_hashes": hashes("current_self_state_hash"),
            "current_self_state_model_request_hashes": list(
                dict.fromkeys(
                    str(record["model_invocation_request_hash"])
                    for record in current_self_records
                    if isinstance(
                        record.get("model_invocation_request_hash"),
                        str,
                    )
                )
            ),
            "recall_material_present_count": sum(
                isinstance(record.get("recall_context_hash"), str) for record in records
            ),
            "recall_material_hashes": hashes("recall_context_hash"),
            "recall_material_model_request_hashes": list(
                dict.fromkeys(
                    str(record["model_invocation_request_hash"])
                    for record in recall_records
                    if isinstance(
                        record.get("model_invocation_request_hash"),
                        str,
                    )
                )
            ),
            # Compatibility aliases now use the same strict non-empty
            # semantics; unavailable lane placeholders no longer count.
            "recall_context_present_count": sum(
                isinstance(record.get("recall_context_hash"), str) for record in records
            ),
            "recall_context_hashes": hashes("recall_context_hash"),
            "emotion_context_present_count": sum(
                isinstance(record.get("emotion_context_hash"), str) for record in records
            ),
            "emotion_context_hashes": hashes("emotion_context_hash"),
            "source_closure_request_count": len(source_records),
            "source_closure_request_hashes": list(
                dict.fromkeys(str(record["request_hash"]) for record in source_records)
            ),
            "source_closure_model_request_hashes": list(
                dict.fromkeys(
                    str(record["model_invocation_request_hash"])
                    for record in source_records
                    if isinstance(record.get("model_invocation_request_hash"), str)
                )
            ),
            "request_evidence": [
                {
                    "model_invocation_request_hash": record.get("model_invocation_request_hash"),
                    "current_self_state_hash": record.get("current_self_state_hash"),
                    "recall_context_hash": record.get("recall_context_hash"),
                    "emotion_context_hash": record.get("emotion_context_hash"),
                    "source_closure_request": record["source_closure_request"],
                }
                for record in records
                if isinstance(record.get("model_invocation_request_hash"), str)
            ],
            "causal_context_request_count": len(causal_records),
            "causal_context_request_hashes": list(
                dict.fromkeys(str(record["request_hash"]) for record in causal_records)
            ),
            "causal_context_model_request_hashes": list(
                dict.fromkeys(
                    str(record["model_invocation_request_hash"])
                    for record in causal_records
                    if isinstance(
                        record.get("model_invocation_request_hash"),
                        str,
                    )
                )
            ),
        }


def _provider_capture_handler(
    state: _ProviderCaptureState,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "GirlAgentProviderCapture/1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(decoded, dict):
                    raise ValueError("request body must be a JSON object")
                status, response = state.handle(
                    path=self.path,
                    payload=decoded,
                    authorization=str(self.headers.get("Authorization") or ""),
                )
            except (httpx.HTTPError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                status = 502
                response = {"error": {"message": f"provider capture failure: {type(exc).__name__}"}}
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                # Expected when an interrupted role call closes its provider
                # connection before the stub's delayed result is returned.
                return

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


@contextmanager
def _provider_capture_server(
    *,
    mode: Literal["loopback-stub", "real-provider"],
    upstream_base_url: str | None,
) -> Iterator[tuple[_ProviderCaptureState, str]]:
    state = _ProviderCaptureState(mode=mode, upstream_base_url=upstream_base_url)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _provider_capture_handler(state),
    )
    thread = Thread(
        target=server.serve_forever,
        name="isolated-model-provider-capture",
        daemon=True,
    )
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield state, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _loopback_port() -> int:
    """Reserve and release one loopback port for the immediately following spawn."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _CaptureState:
    """Thread-safe, positive provider evidence for the isolated OneBot API."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._effects: list[dict[str, object]] = []
        self._messages: dict[str, object] = {}

    def _next_message_id(self) -> str:
        return f"isolated-capture-{secrets.token_hex(8)}"

    def handle(self, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        with self._lock:
            if path == "/get_friend_msg_history":
                # The provider history is deliberately empty. Restart recovery
                # is proved from the daemon-owned ledger, not manufactured by
                # re-injecting messages from this test double.
                return 200, {"status": "ok", "retcode": 0, "data": {"messages": []}}
            if path == "/get_msg":
                message_id = str(payload.get("message_id") or "")
                if message_id in self._messages:
                    return 200, {
                        "status": "ok",
                        "retcode": 0,
                        "data": {
                            "message_id": message_id,
                            "message": self._messages[message_id],
                        },
                    }
                return 200, {
                    "status": "failed",
                    "retcode": 1404,
                    "message": "isolated capture has no such message",
                }

            message_id = self._next_message_id()
            if path == "/send_private_msg":
                raw_message = payload.get("message")
                if isinstance(raw_message, str):
                    modality = "text"
                    content: object = raw_message
                elif isinstance(raw_message, list) and any(
                    isinstance(item, dict) and item.get("type") == "face" for item in raw_message
                ):
                    modality = "sticker"
                    content = raw_message
                else:
                    modality = "media"
                    content = raw_message
            elif path == "/set_msg_emoji_like":
                modality = "reaction"
                content = {
                    "target_message_id": payload.get("message_id"),
                    "emoji_id": payload.get("emoji_id"),
                }
            elif path == "/set_input_status":
                modality = "typing"
                content = str(payload.get("event_type") or "")
            else:
                return 404, {
                    "status": "failed",
                    "retcode": 1404,
                    "message": f"unsupported isolated OneBot endpoint: {path}",
                }

            effect = {
                "sequence": len(self._effects) + 1,
                "observed_at": datetime.now(UTC).isoformat(),
                "endpoint": path,
                "modality": modality,
                "recipient_id": str(payload.get("user_id") or _RECIPIENT_ID),
                "content": content,
                "message_id": message_id,
            }
            self._effects.append(effect)
            self._messages[message_id] = content
            return 200, {
                "status": "ok",
                "retcode": 0,
                "data": {"message_id": message_id},
            }

    def snapshot(self) -> tuple[dict[str, object], ...]:
        with self._lock:
            return tuple(dict(item) for item in self._effects)

    def report_snapshot(self, *, hash_only: bool) -> tuple[dict[str, object], ...]:
        effects = self.snapshot()
        if not hash_only:
            return effects
        redacted: list[dict[str, object]] = []
        for item in effects:
            content = item.get("content")
            retained = {key: value for key, value in item.items() if key != "content"}
            retained["content_hash"] = _canonical_hash(content)
            retained["content_bytes"] = len(
                json.dumps(
                    content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            redacted.append(retained)
        return tuple(redacted)

    def visible_count(self) -> int:
        return sum(str(item["modality"]) in _VISIBLE_MODALITIES for item in self.snapshot())


def _capture_handler(
    state: _CaptureState,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "GirlAgentIsolatedCapture/1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(decoded, dict):
                    raise ValueError("request body must be a JSON object")
                status, response = state.handle(self.path, decoded)
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
                status = 400
                response = {
                    "status": "failed",
                    "retcode": 1400,
                    "message": type(exc).__name__,
                }
            body = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return Handler


@contextmanager
def _capture_server() -> Iterator[tuple[_CaptureState, str]]:
    state = _CaptureState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _capture_handler(state))
    thread = Thread(
        target=server.serve_forever,
        name="isolated-onebot-capture",
        daemon=True,
    )
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield state, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@dataclass
class _DaemonProcess:
    process: subprocess.Popen[str]
    log_path: Path
    log_stream: Any
    base_url: str

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log_stream.close()

    def log_tail(self, *, limit: int = 8_000) -> str:
        if not self.log_stream.closed:
            self.log_stream.flush()
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(encoding="utf-8", errors="replace")[-limit:]


class _IsolatedLoopbackRoleModel(DeepSeekChatModel):
    """Acceptance-only character authority served by the hash capture stub."""

    semantic_authority_id = _LOOPBACK_ROLE_AUTHORITY


class _IsolatedDeterministicLifeSourceReviewer:
    """Acceptance-only Life truth reviewer with no shared mutable runtime.

    The process acceptance is about daemon, ledger, HTTP, restart and causal
    evidence behavior. Life source review is a separate hard-boundary model
    role, so this fixture answers its two closed contracts locally instead of
    reusing either the character authority or the conversation reviewer.
    """

    model = _LOOPBACK_LIFE_REVIEW_MODEL
    semantic_authority_id = _LOOPBACK_LIFE_REVIEW_AUTHORITY

    def __init__(self) -> None:
        self._closed = False

    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract in _LOOPBACK_LIFE_REVIEW_CONTRACTS

    def installs_strict_output_contract(self, contract: str) -> bool:
        return contract in _LOOPBACK_LIFE_REVIEW_CONTRACTS

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> str:
        del temperature
        if self._closed:
            raise RuntimeError("isolated Life reviewer is closed")
        joined = "\n".join(message["content"] for message in messages)
        if "life-development-source-closure-review.1" in joined:
            review = {
                "decision": "supported",
                "unsupported_claim_ids": [],
                "undeclared_fact_fragments": [],
                "undeclared_fact_paths": [],
                "typed_location_conflicts": [],
                "reason": "The acceptance fixture found no unsupported existing-world claim.",
            }
        elif "life-development-novel-origin-review.2" in joined:
            review = {
                "decision": "supported",
                "unsupported_claims": [],
                "unsupported_provisional_npcs": [],
                "unsupported_outcome_prerequisites": [],
                "undeclared_premise_fragments": [],
                "reason": "The acceptance fixture found no imported prior-world premise.",
            }
        else:
            raise ValueError("isolated Life reviewer received an unknown contract")
        return json.dumps({"review": review}, ensure_ascii=False)

    async def aclose(self) -> None:
        self._closed = True

    @property
    def shutdown_pending_task_count(self) -> int:
        return 0

    async def wait_for_shutdown_quiescence(self) -> None:
        return None


class _IsolatedLoopbackSourceReviewer(StructuredSourceReviewModel):
    """Acceptance-only strict reviewer, distinct from the role authority."""

    semantic_authority_id = _LOOPBACK_REVIEW_AUTHORITY

    def fork_isolated_runtime(self) -> _IsolatedDeterministicLifeSourceReviewer:
        """Provide Life with a dedicated deterministic acceptance authority."""

        return _IsolatedDeterministicLifeSourceReviewer()

    def request_payload(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        json_object: bool = False,
    ) -> dict[str, object]:
        payload = super().request_payload(
            messages,
            temperature=temperature,
            json_object=json_object,
        )
        # The hash-only capture correlates the exact model invocation from
        # messages + temperature. Official OpenAI review routes deliberately
        # omit this knob, but the isolated fixture endpoint accepts it and
        # records the same caller-visible value used by the audit ledger.
        payload["temperature"] = temperature
        return payload


def _serve_isolated_loopback_daemon(*, port: int) -> None:
    """Run the real QQ app with explicit, test-only loopback model authorities.

    A dynamic loopback URL is intentionally absent from the production
    semantic-authority registry.  The acceptance subprocess therefore injects
    two conspicuous authorities at the app construction seam instead of
    teaching production configuration to trust arbitrary localhost models.
    The reviewer still uses the exact RR.3/V7 structured transport schemas;
    the capture server retains only request hashes.
    """

    settings = Settings()
    role_model = _IsolatedLoopbackRoleModel(
        api_key=settings.deepseek_api_key or "isolated-loopback-role",
        base_url=settings.deepseek_base_url,
        model=_LOOPBACK_ROLE_MODEL,
        thinking_enabled=False,
    )
    review_evidence = StrictOutputCapabilityEvidence.verified(
        evidence_source="isolated_acceptance_contract_fixture",
        provider="openai",
        model=_LOOPBACK_REVIEW_MODEL,
        contracts=_LOOPBACK_REVIEW_CONTRACTS,
        observed_at="2026-08-01",
        evidence_revision="isolated-loopback-rra3-v7.1",
        audit_sample_count=2,
        audit_success_count=2,
    )
    reviewer = _IsolatedLoopbackSourceReviewer(
        api_key="isolated-loopback-reviewer",
        base_url=settings.deepseek_base_url,
        model=_LOOPBACK_REVIEW_MODEL,
        reasoning_effort="none",
        max_completion_tokens=1_200,
        strict_output_capability_evidence=review_evidence,
    )
    app = create_qq_c2c_onebot_app(
        adapter="napcat",
        settings=settings,
        _test_only_model=role_model,
        _test_only_advisory_model=role_model,
        _test_only_source_closure_model=reviewer,
        scheduler_interval_seconds=settings.qq_c2c_scheduler_interval_seconds,
    )
    try:
        with QQOutboundOwnerLease(
            qq_outbound_owner_lock_path(settings.database_path),
            adapter="napcat",
        ):
            uvicorn.run(app, host="127.0.0.1", port=port)
    finally:
        async def close_models() -> None:
            await reviewer.aclose()
            await role_model.aclose()

        asyncio.run(close_models())


def _is_exact_ipv4_loopback_http_url(value: str) -> bool:
    """Accept only a path-free HTTP endpoint on literal ``127.0.0.1``."""

    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


def _daemon_environment(
    *,
    database: Path,
    capture_url: str,
    attachment_cache: Path,
    model_mode: _ModelMode,
    provider_capture_url: str | None,
    production_source_authority: bool = False,
) -> dict[str, str]:
    if not _is_exact_ipv4_loopback_http_url(capture_url):
        raise ValueError("OneBot capture must bind exact IPv4 loopback")
    if production_source_authority and model_mode != "real-provider":
        raise ValueError("production source authority is valid only with real-provider mode")
    environment = dict(os.environ)
    environment.update(
        {
            # OneBot and the DeepSeek hash-capture hop are exact loopback.
            # Explicitly clear ambient proxy variables so neither can be
            # redirected off-host.  The production source-authority opt-in
            # uses its configured HTTPS base URLs directly and is reported as
            # uncaptured external reviewer traffic.
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
            "http_proxy": "",
            "https_proxy": "",
            "all_proxy": "",
            "DATABASE_PATH": str(database),
            "QQ_ADAPTER": "napcat",
            "NAPCAT_API_URL": capture_url,
            "NAPCAT_ALLOWED_PRIVATE_USER_IDS": _RECIPIENT_ID,
            "NAPCAT_PROACTIVE_USER_ID": _RECIPIENT_ID,
            "NAPCAT_ACCEPT_UNAUTHENTICATED_LOCAL_EVENTS": "true",
            "PRIMARY_USER_ID": _PRIMARY_USER_ID,
            "WORLD_V2_QQ_C2C_MODE": "v2",
            # One immediate scheduler pass proves the lifespan task is alive;
            # the long interval prevents unrelated background work from racing
            # the restart/replay assertions.
            "QQ_C2C_SCHEDULER_INTERVAL_SECONDS": "600",
            "QQ_C2C_IDLE_HEARTBEAT_SECONDS": "600",
            "WORLD_V2_RECALL_SEMANTIC_ENABLED": "false",
            "LOCAL_APPRAISAL_ENABLED": "false",
            "WORLD_V2_CONTEXTUAL_FAILSAFE_ENABLED": "false",
            # The legacy loopback provider fixture returns ordinary JSON and
            # does not implement the expression-units SSE contract. Streaming
            # has its own transport + production-host acceptance tests; keep
            # this hash-capture harness on its declared non-streaming surface.
            "WORLD_V2_EXPRESSION_EPISODE_MODE": "shadow",
            "ATTACHMENT_CACHE_PATH": str(attachment_cache),
        }
    )
    if not production_source_authority:
        # Every default mode is hermetic with respect to external reviewer
        # providers, including ``--fake``.  Ambient shell credentials or a
        # production redundancy flag must never silently widen this manual
        # acceptance's network boundary.
        environment.update(
            {
                "OPENAI_API_KEY": "",
                "OPENROUTER_API_KEY": "",
                "WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED": "false",
            }
        )
    if model_mode in _PROVIDER_MODES:
        if provider_capture_url is None or not _is_exact_ipv4_loopback_http_url(
            provider_capture_url
        ):
            raise ValueError("provider capture must bind exact IPv4 loopback")
        environment["DEEPSEEK_BASE_URL"] = provider_capture_url
        environment.update(
            {
                "ARK_API_KEY": "",
                "CIVITAI_API_KEY": "",
                "CIVITAI_KREA2_ENABLED": "false",
                "WORLD_V2_MEDIA_PREVIEW_ENABLED": "false",
                "ALLOW_AUTO_IMAGE_GENERATION": "false",
                "ALLOW_AUTO_VISION": "false",
                "ALLOW_AUTO_TRANSCRIPTION": "false",
                "WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED": (
                    "true" if production_source_authority else "false"
                ),
            }
        )
        if production_source_authority:
            # Keep the two explicitly authorized source-authority
            # credentials/base URLs inherited from the manual shell.  Clear
            # the optional client-specific proxy so the report's "direct"
            # network claim remains true.
            environment["OPENAI_PROXY_URL"] = ""
        if model_mode == "loopback-stub":
            environment.update(
                {
                    "DEEPSEEK_API_KEY": "isolated-loopback-stub",
                    "DEEPSEEK_MODEL": "isolated-loopback-stub",
                    "DEEPSEEK_REPLY_MODEL": "isolated-loopback-stub",
                    "DEEPSEEK_EXPRESSIVE_MODEL": "isolated-loopback-stub",
                    "DEEPSEEK_DEEP_APPRAISAL_MODEL": "isolated-loopback-stub",
                }
            )
    # Keep import behavior explicit when the repository is not installed in
    # editable mode. The daemon entry point itself remains the production
    # module and composition root.
    source_path = str(_ROOT / "src")
    current_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not current_python_path
        else os.pathsep.join((source_path, current_python_path))
    )
    return environment


def _start_daemon(
    *,
    database: Path,
    capture_url: str,
    attachment_cache: Path,
    log_path: Path,
    model_mode: _ModelMode,
    provider_capture_url: str | None,
    production_source_authority: bool = False,
) -> _DaemonProcess:
    port = _loopback_port()
    log_stream = log_path.open("a", encoding="utf-8")
    if model_mode == "loopback-stub":
        command = [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from scripts.run_isolated_daemon_acceptance import "
                "_serve_isolated_loopback_daemon; "
                "_serve_isolated_loopback_daemon(port=int(sys.argv[1]))"
            ),
            str(port),
        ]
    else:
        command = [
            sys.executable,
            "-m",
            "companion_daemon.napcat_cli",
            "--adapter",
            "napcat",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--world-v2-c2c",
        ]
        if model_mode == "fake":
            command.insert(-1, "--fake")
    try:
        process = subprocess.Popen(
            command,
            cwd=_ROOT,
            env=_daemon_environment(
                database=database,
                capture_url=capture_url,
                attachment_cache=attachment_cache,
                model_mode=model_mode,
                provider_capture_url=provider_capture_url,
                production_source_authority=production_source_authority,
            ),
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except Exception:
        log_stream.close()
        raise
    return _DaemonProcess(
        process=process,
        log_path=log_path,
        log_stream=log_stream,
        base_url=f"http://127.0.0.1:{port}",
    )


def _wait_for_health(
    daemon: _DaemonProcess,
    *,
    timeout_seconds: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    with httpx.Client(base_url=daemon.base_url, timeout=3, trust_env=False) as client:
        while time.monotonic() < deadline:
            if daemon.process.poll() is not None:
                raise RuntimeError(
                    "isolated daemon exited before health became ready\n" + daemon.log_tail()
                )
            try:
                response = client.get("/health")
                response.raise_for_status()
                payload = response.json()
                scheduler = payload.get("scheduler") if isinstance(payload, dict) else None
                if (
                    isinstance(payload, dict)
                    and payload.get("status") == "running"
                    and isinstance(scheduler, dict)
                    and int(scheduler.get("passes_completed") or 0) >= 1
                ):
                    return payload
            except (httpx.HTTPError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.1)
    raise TimeoutError(f"isolated daemon health timed out ({last_error})\n{daemon.log_tail()}")


def _post_turn(
    *,
    daemon: _DaemonProcess,
    source_event_id: str,
    text: str,
) -> dict[str, object]:
    event = {
        "time": time.time(),
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "user_id": int(_RECIPIENT_ID),
        "message_id": source_event_id,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": int(_RECIPIENT_ID), "nickname": "isolated-user"},
    }
    started = time.perf_counter()
    with httpx.Client(base_url=daemon.base_url, timeout=45, trust_env=False) as client:
        response = client.post("/onebot/event", json=event)
    duration_ms = round((time.perf_counter() - started) * 1_000, 3)
    try:
        payload = response.json()
    except ValueError:
        payload = {"unparsed_body": response.text}
    return {
        "source_event_id": source_event_id,
        "http_status": response.status_code,
        "daemon_outcome": payload,
        "roundtrip_ms": duration_ms,
    }


def _post_turn_after(
    *,
    delay_seconds: float,
    daemon: _DaemonProcess,
    source_event_id: str,
    text: str,
) -> dict[str, object]:
    if delay_seconds > 0:
        time.sleep(delay_seconds)
    return _post_turn(
        daemon=daemon,
        source_event_id=source_event_id,
        text=text,
    )


def _run_burst(daemon: _DaemonProcess) -> tuple[dict[str, object], ...]:
    turns = (
        (
            0.0,
            "isolated-daemon-burst-1",
            "连发一：我先把事情的背景说一下。",
        ),
        (
            0.08,
            "isolated-daemon-burst-2",
            "连发二：中间其实还有个让我纠结的细节。",
        ),
        (
            0.16,
            "isolated-daemon-burst-3",
            "连发三：总之现在算是告一段落了。",
        ),
    )
    with ThreadPoolExecutor(max_workers=len(turns)) as executor:
        futures = [
            executor.submit(
                _post_turn_after,
                delay_seconds=delay,
                daemon=daemon,
                source_event_id=source_event_id,
                text=text,
            )
            for delay, source_event_id, text in turns
        ]
        return tuple(future.result(timeout=60) for future in futures)


def _wait_for_durable_observation_source(
    database: Path,
    *,
    source_event_id: str,
    timeout_seconds: float,
) -> bool:
    """Confirm ingress crossed the durable Observation boundary.

    Merely scheduling a client future does not prove that the daemon accepted
    the second ingress.  The committed source identity is the earliest
    process-independent evidence that the second turn is actually inside the
    World before provider overlap is evaluated.
    """

    deadline = time.monotonic() + timeout_seconds
    database_uri = database.resolve().as_uri() + "?mode=ro"
    while time.monotonic() < deadline:
        try:
            with sqlite3.connect(database_uri, uri=True, timeout=0.5) as connection:
                rows = connection.execute(
                    """
                    SELECT event_json
                    FROM world_v2_events
                    WHERE world_id = ?
                      AND json_extract(event_json, '$.event_type')
                          = 'ObservationRecorded'
                    """,
                    (qq_c2c_world_id(_PRIMARY_USER_ID),),
                ).fetchall()
        except sqlite3.OperationalError:
            time.sleep(0.02)
            continue
        for row in rows:
            try:
                event = json.loads(str(row[0]))
                raw_payload = event.get("payload_json")
                payload = (
                    json.loads(raw_payload)
                    if isinstance(raw_payload, str)
                    else event.get("payload")
                )
            except (AttributeError, TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            metadata = payload.get("coalescing_metadata")
            source_ids = metadata.get("source_event_ids") if isinstance(metadata, dict) else None
            if isinstance(source_ids, list) and source_event_id in source_ids:
                return True
            if payload.get("source_event_id") == source_event_id:
                return True
        time.sleep(0.02)
    return False


def _run_interruption(
    *,
    daemon: _DaemonProcess,
    database: Path,
    provider_capture: _ProviderCaptureState,
) -> dict[str, object]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        first: Future[dict[str, object]] = executor.submit(
            _post_turn,
            daemon=daemon,
            source_event_id="isolated-daemon-interruption-1",
            text=(f"{_INTERRUPTION_MARKER}：我刚才想说一件稍微复杂的事，先从前半段讲起。"),
        )
        marker_reached_provider = provider_capture.wait_for_interruption_marker(15)
        second: Future[dict[str, object]] = executor.submit(
            _post_turn,
            daemon=daemon,
            source_event_id="isolated-daemon-interruption-2",
            text=f"{_INTERRUPTION_SECOND_MARKER}：打断一下，我先更正刚才最关键的一点。",
        )
        second_ingress_committed = _wait_for_durable_observation_source(
            database,
            source_event_id="isolated-daemon-interruption-2",
            timeout_seconds=15,
        )
        second_reached_provider = provider_capture.wait_for_second_interruption_marker(15)
        overlap_at_second_provider_entry = provider_capture.wait_for_second_interruption_overlap(0)
        overlap_observed = (
            marker_reached_provider
            and second_ingress_committed
            and second_reached_provider
            and overlap_at_second_provider_entry
        )
        first_turn = first.result(timeout=60)
        second_turn = second.result(timeout=60)
    return {
        "marker_reached_provider": marker_reached_provider,
        "second_ingress_started": second_ingress_committed,
        "second_ingress_committed": second_ingress_committed,
        "second_ingress_reached_provider": second_reached_provider,
        "overlap_observed_at_second_provider_entry": (overlap_at_second_provider_entry),
        "first_provider_in_flight_when_second_reached_provider": (overlap_at_second_provider_entry),
        "second_ingress_started_before_first_completed": overlap_observed,
        "overlap_observed": overlap_observed,
        "first_turn": first_turn,
        "second_turn": second_turn,
    }


def _cold_replay(database: Path) -> dict[str, object]:
    ledger = SQLiteWorldLedger(
        path=database,
        world_id=qq_c2c_world_id(_PRIMARY_USER_ID),
    )
    try:
        evidence = ledger.export_replay_evidence()
    finally:
        ledger.close()

    source_event_ids: list[str] = []
    event_type_counts: dict[str, int] = {}
    model_result_request_hashes: list[str] = []
    recall_trace_count = 0
    presented_prefetch_count = 0
    private_turn_state_hashes: list[str] = []
    proposal_count = 0
    model_results: dict[str, dict[str, object]] = {}
    model_result_records: list[dict[str, object]] = []
    proposals: dict[str, dict[str, object]] = {}
    accepted_expression_plans: dict[str, dict[str, object]] = {}
    action_authorizations: dict[str, dict[str, object]] = {}
    receipts_by_action: dict[str, list[dict[str, object]]] = {}
    settlements_by_action: dict[str, list[dict[str, object]]] = {}
    observations_by_event_ref: dict[str, dict[str, object]] = {}
    observation_source_groups: list[list[str]] = []
    for item in evidence.events:
        event_type = item.event.event_type
        event_ref = item.event.event_id
        event_sequence = item.cursor.ledger_sequence
        event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
        payload = item.event.payload()
        if event_type == "ModelResultRecorded":
            raw_audit = payload.get("audit_json")
            if isinstance(raw_audit, str):
                try:
                    audit = json.loads(raw_audit)
                except json.JSONDecodeError:
                    audit = None
                if isinstance(audit, dict):
                    route = audit.get("route")
                    request_hash = audit.get("request_hash")
                    if isinstance(request_hash, str):
                        model_result_request_hashes.append(request_hash)
                        model_result_ref = payload.get("model_result_ref")
                        trigger_ref = payload.get("trigger_ref")
                        attempt_id = payload.get("attempt_id")
                        model_call_id = payload.get("model_call_id")
                        parent_model_call_id = payload.get("parent_model_call_id")
                        recall_trace = audit.get("recall_trace")
                        authored_candidates = audit.get("authored_candidate_audits")
                        authored_candidate_model_call_ids = (
                            [
                                str(candidate["model_call_id"])
                                for candidate in authored_candidates
                                if isinstance(candidate, dict)
                                and isinstance(candidate.get("model_call_id"), str)
                            ]
                            if isinstance(authored_candidates, list)
                            else []
                        )
                        related_author_model_call_ids = list(
                            dict.fromkeys(
                                [
                                    *([model_call_id] if isinstance(model_call_id, str) else []),
                                    *authored_candidate_model_call_ids,
                                ]
                            )
                        )
                        if (
                            isinstance(trigger_ref, str)
                            and isinstance(attempt_id, str)
                            and isinstance(model_call_id, str)
                        ):
                            record = {
                                "model_result_ref": (
                                    model_result_ref if isinstance(model_result_ref, str) else None
                                ),
                                "request_hash": request_hash,
                                "trigger_ref": trigger_ref,
                                "attempt_id": attempt_id,
                                "model_call_id": model_call_id,
                                "parent_model_call_id": (
                                    parent_model_call_id
                                    if isinstance(parent_model_call_id, str)
                                    else None
                                ),
                                "related_author_model_call_ids": (related_author_model_call_ids),
                                "model_id": (
                                    audit.get("model_id")
                                    if isinstance(audit.get("model_id"), str)
                                    else None
                                ),
                                "model_version": (
                                    audit.get("model_version")
                                    if isinstance(audit.get("model_version"), str)
                                    else None
                                ),
                                "attempted_model_id": (
                                    audit.get("attempted_model_id")
                                    if isinstance(audit.get("attempted_model_id"), str)
                                    else None
                                ),
                                "router_version": (
                                    route.get("router_version")
                                    if isinstance(route, dict)
                                    and isinstance(route.get("router_version"), str)
                                    else None
                                ),
                                "route_reason_code": (
                                    route.get("reason_code")
                                    if isinstance(route, dict)
                                    and isinstance(route.get("reason_code"), str)
                                    else None
                                ),
                                "status": (
                                    audit.get("status")
                                    if isinstance(audit.get("status"), str)
                                    else None
                                ),
                                "slot": (
                                    audit.get("slot")
                                    if isinstance(audit.get("slot"), str)
                                    else None
                                ),
                                "outcome": (
                                    audit.get("outcome")
                                    if isinstance(audit.get("outcome"), str)
                                    else None
                                ),
                                "failure_code": (
                                    audit.get("failure_code")
                                    if isinstance(audit.get("failure_code"), str)
                                    else None
                                ),
                                "character_recall_selected": isinstance(
                                    recall_trace,
                                    dict,
                                ),
                                "character_recall_trace_result_hash": (
                                    recall_trace.get("result_hash")
                                    if isinstance(recall_trace, dict)
                                    and isinstance(
                                        recall_trace.get("result_hash"),
                                        str,
                                    )
                                    else None
                                ),
                                "event_ref": event_ref,
                                "event_sequence": event_sequence,
                            }
                            model_result_records.append(record)
                            if isinstance(model_result_ref, str):
                                model_results[model_result_ref] = record
                    if isinstance(audit.get("recall_trace"), dict) or isinstance(
                        audit.get("prefetch_trace"), dict
                    ):
                        recall_trace_count += 1
                    presentations = audit.get("presented_prefetch_traces")
                    if isinstance(presentations, list):
                        presented_prefetch_count += len(presentations)
        if event_type == "ProposalRecorded":
            raw_proposal = payload.get("proposal_json")
            if isinstance(raw_proposal, str):
                try:
                    proposal = json.loads(raw_proposal)
                except json.JSONDecodeError:
                    proposal = None
                if isinstance(proposal, dict):
                    proposal_count += 1
                    private_state = proposal.get("private_turn_state")
                    if isinstance(private_state, dict):
                        private_state_hash = _canonical_hash(private_state)
                        private_turn_state_hashes.append(private_state_hash)
                    proposal_id = payload.get("proposal_id")
                    attempt_id = payload.get("attempt_id")
                    model_result_ref = payload.get("model_result_ref")
                    trigger_ref = payload.get("trigger_ref")
                    if (
                        isinstance(proposal_id, str)
                        and isinstance(attempt_id, str)
                        and isinstance(model_result_ref, str)
                        and isinstance(trigger_ref, str)
                    ):
                        proposals[proposal_id] = {
                            "attempt_id": attempt_id,
                            "model_result_ref": model_result_ref,
                            "trigger_ref": trigger_ref,
                            "timing_choice": proposal.get("timing_choice"),
                            "source_review_eligible": (
                                any(
                                    isinstance(beat, dict)
                                    and isinstance(beat.get("text"), str)
                                    and bool(str(beat["text"]).strip())
                                    for beat in proposal.get("beats", ())
                                )
                                if isinstance(proposal.get("beats"), list)
                                else False
                            )
                            or (
                                isinstance(proposal.get("world_claims"), list)
                                and bool(proposal["world_claims"])
                            ),
                            "private_state_hash": (
                                _canonical_hash(private_state)
                                if isinstance(private_state, dict)
                                else None
                            ),
                            "event_ref": event_ref,
                            "event_sequence": event_sequence,
                        }
        if event_type == "ExpressionPlanAccepted":
            proposal_id = payload.get("proposal_id")
            plan_id = payload.get("plan_id")
            if isinstance(proposal_id, str) and isinstance(plan_id, str):
                accepted_expression_plans[plan_id] = {
                    "proposal_id": proposal_id,
                    "acceptance_id": payload.get("acceptance_id"),
                    "event_ref": event_ref,
                    "event_sequence": event_sequence,
                }
        if event_type == "ActionAuthorized":
            action = payload.get("action")
            if isinstance(action, dict):
                action_id = action.get("action_id")
                plan_id = action.get("expression_plan_id")
                if isinstance(action_id, str) and isinstance(plan_id, str):
                    action_authorizations[action_id] = {
                        "plan_id": plan_id,
                        "kind": action.get("kind"),
                        "event_ref": event_ref,
                        "event_sequence": event_sequence,
                    }
        if event_type == "ExecutionReceiptRecorded":
            receipt = payload.get("receipt")
            if isinstance(receipt, dict):
                action_id = receipt.get("action_id")
                receipt_id = receipt.get("receipt_id")
                observed_state = receipt.get("observed_state")
                if (
                    isinstance(action_id, str)
                    and isinstance(receipt_id, str)
                    and isinstance(observed_state, str)
                ):
                    receipts_by_action.setdefault(action_id, []).append(
                        {
                            "receipt_id": receipt_id,
                            "observed_state": observed_state,
                            "event_ref": event_ref,
                            "event_sequence": event_sequence,
                        }
                    )
        if event_type in {
            "ActionProviderAccepted",
            "ActionDelivered",
            "ActionFailed",
            "ActionUnknown",
            "ActionCancelled",
            "ActionExpired",
        }:
            action_id = payload.get("action_id")
            if isinstance(action_id, str):
                settlements_by_action.setdefault(action_id, []).append(
                    {
                        "event_type": event_type,
                        "event_ref": event_ref,
                        "event_sequence": event_sequence,
                    }
                )
        if event_type != "ObservationRecorded":
            continue
        observation_id = payload.get("observation_id")
        metadata = payload.get("coalescing_metadata")
        raw_source_ids = metadata.get("source_event_ids") if isinstance(metadata, dict) else None
        source_group = (
            [str(source_id) for source_id in raw_source_ids if isinstance(source_id, str)]
            if isinstance(raw_source_ids, list)
            else []
        )
        fallback_source_id = payload.get("source_event_id")
        if not source_group and isinstance(fallback_source_id, str):
            source_group = [fallback_source_id]
        source_event_ids.extend(source_group)
        observation_source_groups.append(source_group)
        if isinstance(observation_id, str):
            observations_by_event_ref[event_ref] = {
                "observation_id": observation_id,
                "source_event_ids": source_group,
                "event_ref": event_ref,
                "event_sequence": event_sequence,
            }
    projection = evidence.projection
    terminal_expression_processes = tuple(
        process
        for process in projection.trigger_processes
        if process.process_kind == "expression_episode"
        and process.state == "terminal"
        and process.claim_lease is not None
        and isinstance(process.runtime_outcome_ref, str)
    )
    expected_settlement_types = {
        "provider_accepted": "ActionProviderAccepted",
        "delivered": "ActionDelivered",
    }
    accepted_expression_chains: list[dict[str, object]] = []
    for plan_id, acceptance in accepted_expression_plans.items():
        proposal_id = acceptance["proposal_id"]
        if not isinstance(proposal_id, str):
            continue
        proposal = proposals.get(proposal_id)
        if proposal is None:
            continue
        model_result_ref = proposal["model_result_ref"]
        if not isinstance(model_result_ref, str):
            continue
        model_result = model_results.get(model_result_ref)
        trigger_ref = proposal["trigger_ref"]
        if (
            model_result is None
            or not isinstance(trigger_ref, str)
            or model_result["trigger_ref"] != trigger_ref
            or model_result["attempt_id"] != proposal["attempt_id"]
        ):
            continue
        observation = observations_by_event_ref.get(trigger_ref)
        if observation is None or not observation["source_event_ids"]:
            continue
        candidate_actions = sorted(
            (
                (action_id, action)
                for action_id, action in action_authorizations.items()
                if action["plan_id"] == plan_id and action["kind"] != "typing"
            ),
            key=lambda item: int(item[1]["event_sequence"]),
        )
        for action_id, action in candidate_actions:
            matching_receipts = sorted(
                (
                    receipt
                    for receipt in receipts_by_action.get(action_id, ())
                    if receipt["observed_state"] in expected_settlement_types
                ),
                key=lambda receipt: int(receipt["event_sequence"]),
            )
            matched: tuple[dict[str, object], dict[str, object]] | None = None
            for receipt in matching_receipts:
                expected_type = expected_settlement_types[str(receipt["observed_state"])]
                settlement = next(
                    (
                        item
                        for item in settlements_by_action.get(action_id, ())
                        if item["event_type"] == expected_type
                    ),
                    None,
                )
                if settlement is not None:
                    matched = (receipt, settlement)
                    break
            if matched is None:
                continue
            receipt, settlement = matched
            event_sequences = [
                int(observation["event_sequence"]),
                int(model_result["event_sequence"]),
                int(proposal["event_sequence"]),
                int(acceptance["event_sequence"]),
                int(action["event_sequence"]),
                int(settlement["event_sequence"]),
                int(receipt["event_sequence"]),
            ]
            if event_sequences != sorted(event_sequences) or len(set(event_sequences)) != len(
                event_sequences
            ):
                continue
            accepted_expression_chains.append(
                {
                    "source_event_ids": list(observation["source_event_ids"]),
                    "observation_id": observation["observation_id"],
                    "observation_event_ref": observation["event_ref"],
                    "trigger_ref": trigger_ref,
                    "attempt_id": model_result["attempt_id"],
                    "request_hash": model_result["request_hash"],
                    "model_call_id": model_result["model_call_id"],
                    "parent_model_call_id": model_result["parent_model_call_id"],
                    "related_author_model_call_ids": model_result["related_author_model_call_ids"],
                    "character_recall_selected": model_result["character_recall_selected"],
                    "character_recall_trace_result_hash": model_result[
                        "character_recall_trace_result_hash"
                    ],
                    "model_result_ref": model_result_ref,
                    "model_result_event_ref": model_result["event_ref"],
                    "proposal_id": proposal_id,
                    "proposal_event_ref": proposal["event_ref"],
                    "proposal_event_sequence": proposal["event_sequence"],
                    "acceptance_id": acceptance["acceptance_id"],
                    "acceptance_event_ref": acceptance["event_ref"],
                    "expression_plan_id": plan_id,
                    "action_id": action_id,
                    "action_event_ref": action["event_ref"],
                    "receipt_id": receipt["receipt_id"],
                    "receipt_event_ref": receipt["event_ref"],
                    "receipt_state": receipt["observed_state"],
                    "settlement_event_ref": settlement["event_ref"],
                    "event_sequences": event_sequences,
                }
            )
            break

    expression_chain_by_proposal = {
        str(chain["proposal_id"]): chain for chain in accepted_expression_chains
    }
    accepted_character_choices: list[dict[str, object]] = []
    for proposal_id, proposal in proposals.items():
        model_result_ref = proposal["model_result_ref"]
        trigger_ref = proposal["trigger_ref"]
        if not isinstance(model_result_ref, str) or not isinstance(trigger_ref, str):
            continue
        model_result = model_results.get(model_result_ref)
        observation = observations_by_event_ref.get(trigger_ref)
        if (
            model_result is None
            or observation is None
            or not observation["source_event_ids"]
            or model_result["trigger_ref"] != trigger_ref
            or model_result["attempt_id"] != proposal["attempt_id"]
        ):
            continue
        process = next(
            (
                item
                for item in terminal_expression_processes
                if item.source_evidence_ref == observation["observation_id"]
                and item.claim_lease is not None
                and proposal["attempt_id"] == item.claim_lease.attempt_id
            ),
            None,
        )
        if process is None:
            continue
        expression_chain = expression_chain_by_proposal.get(proposal_id)
        if expression_chain is not None and any(
            value in str(process.runtime_outcome_ref) for value in ("action_authorized", "deferred")
        ):
            disposition = "effect_accepted"
        elif proposal["timing_choice"] == "silent" and "model-silent" in str(
            process.runtime_outcome_ref
        ):
            disposition = "model_silent"
        else:
            continue
        accepted_character_choices.append(
            {
                "proposal_id": proposal_id,
                "request_hash": model_result["request_hash"],
                "model_result_event_ref": model_result["event_ref"],
                "proposal_event_ref": proposal["event_ref"],
                "private_state_hash": proposal["private_state_hash"],
                "source_review_eligible": proposal["source_review_eligible"],
                "source_event_ids": list(observation["source_event_ids"]),
                "trigger_ref": trigger_ref,
                "attempt_id": model_result["attempt_id"],
                "model_call_id": model_result["model_call_id"],
                "related_author_model_call_ids": model_result["related_author_model_call_ids"],
                "proposal_event_sequence": proposal["event_sequence"],
                "terminal_trigger_id": process.trigger_id,
                "terminal_outcome_ref": process.runtime_outcome_ref,
                "disposition": disposition,
            }
        )
    provider_effected_proposal_ids = set(expression_chain_by_proposal)
    accepted_private_state_hashes = [
        str(proposals[proposal_id]["private_state_hash"])
        for proposal_id in sorted(provider_effected_proposal_ids)
        if isinstance(proposals[proposal_id]["private_state_hash"], str)
    ]
    accepted_expression_request_hashes = [
        str(chain["request_hash"]) for chain in accepted_expression_chains
    ]
    accepted_character_choice_request_hashes = [
        str(item["request_hash"]) for item in accepted_character_choices
    ]
    accepted_character_choice_private_state_hashes = [
        str(item["private_state_hash"])
        for item in accepted_character_choices
        if isinstance(item["private_state_hash"], str)
    ]
    return {
        "source_event_ids": source_event_ids,
        "observation_source_event_id_groups": observation_source_groups,
        "cursor": {
            "world_revision": evidence.cursor.world_revision,
            "deliberation_revision": evidence.cursor.deliberation_revision,
            "ledger_sequence": evidence.cursor.ledger_sequence,
        },
        "semantic_hash": projection.semantic_hash,
        "event_count": len(evidence.events),
        "action_count": len(projection.actions),
        "provider_accepted_action_count": sum(
            item.state in {"provider_accepted", "delivered"} for item in projection.actions
        ),
        "event_type_counts": event_type_counts,
        "model_result_request_hashes": list(dict.fromkeys(model_result_request_hashes)),
        "model_result_records": model_result_records,
        "recall_trace_count": recall_trace_count,
        "presented_prefetch_count": presented_prefetch_count,
        "private_turn_state_proposal_count": len(private_turn_state_hashes),
        "private_turn_state_hashes": list(dict.fromkeys(private_turn_state_hashes)),
        "proposal_count": proposal_count,
        "accepted_expression_candidate_count": len(accepted_expression_chains),
        "accepted_expression_chains": accepted_expression_chains,
        "accepted_private_turn_state_hashes": list(dict.fromkeys(accepted_private_state_hashes)),
        "accepted_expression_request_hashes": list(
            dict.fromkeys(accepted_expression_request_hashes)
        ),
        "accepted_character_choice_count": len(accepted_character_choices),
        "accepted_character_choices": accepted_character_choices,
        "accepted_private_turn_state_count": len(accepted_character_choice_private_state_hashes),
        "accepted_character_choice_request_hashes": list(
            dict.fromkeys(accepted_character_choice_request_hashes)
        ),
        "accepted_character_choice_private_state_hashes": list(
            dict.fromkeys(accepted_character_choice_private_state_hashes)
        ),
        "provider_effected_expression_proposal_count": len(provider_effected_proposal_ids),
        "persisted_projection_matches_independent_replay": (evidence.projection == evidence.replay),
    }


def _source_authority_health(
    health: dict[str, object],
) -> dict[str, object]:
    scheduler = health.get("scheduler")
    if not isinstance(scheduler, dict):
        return {}
    source_authority = scheduler.get("proactive_source_authority")
    return dict(source_authority) if isinstance(source_authority, dict) else {}


def _source_authority_acceptance_report(
    *,
    requested: bool,
    first_health: dict[str, object],
    restart_health: dict[str, object],
    final_replay: dict[str, object],
) -> dict[str, object]:
    """Bind each terminal review-eligible choice to its durable Inventory call."""

    first_source_health = _source_authority_health(first_health)
    restart_source_health = _source_authority_health(restart_health)
    # Provider-subcall audits persist the winning leaf model.  The composite
    # Nano -> Mini availability authority is only a scheduling identity, so it
    # must never be accepted as proof of an actual Inventory call.  Final
    # replay belongs to the restarted process and is checked against that
    # process's exact release-qualified route evidence.
    inventory_model_order = qualified_inventory_route_models(
        restart_source_health
    )
    inventory_models = set(inventory_model_order)
    full_review_model_order = qualified_full_review_route_models(
        restart_source_health
    )
    full_review_models = set(full_review_model_order)
    terminal_choices = [
        item
        for item in final_replay.get("accepted_character_choices", [])
        if isinstance(item, dict)
    ]
    inventory_eligible = [
        item for item in terminal_choices if item.get("source_review_eligible") is True
    ]
    model_result_records = [
        item for item in final_replay.get("model_result_records", []) if isinstance(item, dict)
    ]
    evidence: list[dict[str, object]] = []
    source_authority_evidence: list[dict[str, object]] = []
    for candidate in inventory_eligible:
        raw_related_author_ids = candidate.get("related_author_model_call_ids")
        related_author_ids = {
            str(item)
            for item in (raw_related_author_ids if isinstance(raw_related_author_ids, list) else [])
            if isinstance(item, str)
        }
        model_call_id = candidate.get("model_call_id")
        if isinstance(model_call_id, str):
            related_author_ids.add(model_call_id)
        proposal_sequence = candidate.get("proposal_event_sequence")
        matches = [
            record
            for record in model_result_records
            if record.get("parent_model_call_id") in related_author_ids
            and record.get("trigger_ref") == candidate.get("trigger_ref")
            and record.get("attempt_id") == candidate.get("attempt_id")
            and record.get("model_id") in inventory_models
            and record.get("route_reason_code") == "validation.source_inventory_v5"
            and record.get("router_version") == "provider-subcall-audit.1"
            and record.get("status") == "proposal_validated"
            and record.get("outcome") == "winner"
            and isinstance(record.get("event_sequence"), int)
            and isinstance(proposal_sequence, int)
            and int(record["event_sequence"]) < proposal_sequence
        ]
        full_review_matches = [
            record
            for record in model_result_records
            if record.get("parent_model_call_id") in related_author_ids
            and record.get("trigger_ref") == candidate.get("trigger_ref")
            and record.get("attempt_id") == candidate.get("attempt_id")
            and record.get("model_id") in full_review_models
            and record.get("route_reason_code")
            == "validation.source_closure_review_v7"
            and record.get("router_version") == "provider-subcall-audit.1"
            and record.get("status") == "proposal_validated"
            and record.get("outcome") == "winner"
            and isinstance(record.get("event_sequence"), int)
            and isinstance(proposal_sequence, int)
            and int(record["event_sequence"]) < proposal_sequence
        ]
        if matches:
            evidence.append(
                {
                    "proposal_id": candidate.get("proposal_id"),
                    "inventory_model_call_ids": [
                        str(record["model_call_id"])
                        for record in matches
                        if isinstance(record.get("model_call_id"), str)
                    ],
                    "inventory_model_result_event_refs": [
                        str(record["event_ref"])
                        for record in matches
                        if isinstance(record.get("event_ref"), str)
                    ],
                    "inventory_models": list(
                        dict.fromkeys(
                            str(record["model_id"])
                            for record in matches
                            if isinstance(record.get("model_id"), str)
                        )
                    ),
                }
            )
        winning_matches = matches or full_review_matches
        if not winning_matches:
            continue
        winning_protocol = (
            "inventory_v5" if matches else "full_source_closure_review.7"
        )
        source_authority_evidence.append(
            {
                "proposal_id": candidate.get("proposal_id"),
                "winning_protocol": winning_protocol,
                "model_call_ids": [
                    str(record["model_call_id"])
                    for record in winning_matches
                    if isinstance(record.get("model_call_id"), str)
                ],
                "model_result_event_refs": [
                    str(record["event_ref"])
                    for record in winning_matches
                    if isinstance(record.get("event_ref"), str)
                ],
                "models": list(
                    dict.fromkeys(
                        str(record["model_id"])
                        for record in winning_matches
                        if isinstance(record.get("model_id"), str)
                    )
                ),
            }
        )
    return {
        "contract": "isolated-source-authority-acceptance.2",
        "requested": requested,
        "first_start_health": first_source_health,
        "after_restart_health": restart_source_health,
        "terminal_candidate_inventory": {
            "scope": "terminal_source_review_eligible_character_choices",
            "terminal_character_choice_count": len(terminal_choices),
            "model_silent_terminal_count": sum(
                item.get("disposition") == "model_silent" for item in terminal_choices
            ),
            "non_silent_source_review_ineligible_terminal_count": sum(
                item.get("disposition") != "model_silent"
                and item.get("source_review_eligible") is not True
                for item in terminal_choices
            ),
            "inventory_eligible_terminal_candidate_count": len(inventory_eligible),
            "inventory_proven_terminal_candidate_count": len(evidence),
            "all_inventory_eligible_terminal_candidates_proven": (
                len(evidence) == len(inventory_eligible)
            ),
            "qualified_inventory_models": list(inventory_model_order),
            "evidence": evidence,
        },
        "terminal_candidate_source_authority": {
            "scope": "terminal_source_review_eligible_character_choices",
            "source_review_eligible_terminal_candidate_count": len(inventory_eligible),
            "source_authority_proven_terminal_candidate_count": len(
                source_authority_evidence
            ),
            "all_source_review_eligible_terminal_candidates_proven": (
                len(source_authority_evidence) == len(inventory_eligible)
            ),
            "qualified_inventory_models": list(inventory_model_order),
            "qualified_full_review_models": list(full_review_model_order),
            "evidence": source_authority_evidence,
        },
        "inventory_evidence_basis": (
            "immutable ModelResultRecorded provider-subcall lineage before "
            "the terminal ProposalRecorded event"
        ),
        "coverage_assurance": {
            "proof_source": "private_self_expression_audit",
            "evaluated_by_this_process": False,
            "character_wording_forced": False,
        },
    }


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value if isinstance(item, str)}


def build_causal_audit(
    *,
    final_replay: dict[str, object],
    provider_audit: dict[str, object],
) -> dict[str, object]:
    """Correlate provider presentation with one accepted effect chain.

    Run-wide counts remain useful diagnostics, but they are deliberately kept
    outside this causal evidence.  A chain gains current-self, Recall, or
    source-review evidence only through the exact provider request hash and,
    for nested reviews, the same trigger/attempt plus an explicit parent model
    call.
    """

    event_type_counts = final_replay.get("event_type_counts")
    if not isinstance(event_type_counts, dict):
        raise RuntimeError("cold replay event counts are malformed")
    appraisal_event_count = int(event_type_counts.get("AppraisalAccepted") or 0)
    affect_event_count = sum(
        int(event_type_counts.get(event_type) or 0)
        for event_type in (
            "AffectEpisodeOpened",
            "AffectEpisodeUpdated",
        )
    )
    current_self_model_hashes = _string_set(
        provider_audit.get("current_self_state_model_request_hashes")
    )
    recall_material_model_hashes = _string_set(
        provider_audit.get("recall_material_model_request_hashes")
    )
    source_closure_model_hashes = _string_set(
        provider_audit.get("source_closure_model_request_hashes")
    )
    causal_model_hashes = _string_set(provider_audit.get("causal_context_model_request_hashes"))
    accepted_model_hashes = _string_set(final_replay.get("accepted_expression_request_hashes"))
    accepted_character_choice_request_hashes = _string_set(
        final_replay.get("accepted_character_choice_request_hashes")
    )

    request_evidence_by_hash: dict[str, dict[str, object]] = {}
    raw_request_evidence = provider_audit.get("request_evidence")
    if isinstance(raw_request_evidence, list):
        for item in raw_request_evidence:
            if not isinstance(item, dict):
                continue
            request_hash = item.get("model_invocation_request_hash")
            if isinstance(request_hash, str):
                request_evidence_by_hash[request_hash] = item

    model_result_records = [
        item for item in final_replay.get("model_result_records", []) if isinstance(item, dict)
    ]
    selected_recall_model_results = [
        item for item in model_result_records if item.get("character_recall_selected") is True
    ]
    sanitized_model_result_diagnostics = [
        {
            "model_call_id": item.get("model_call_id"),
            "parent_model_call_id": item.get("parent_model_call_id"),
            "model": item.get("model_id") or item.get("attempted_model_id"),
            "router_version": item.get("router_version"),
            "route_reason_code": item.get("route_reason_code"),
            "slot": item.get("slot"),
            "status": item.get("status"),
            "outcome": item.get("outcome"),
            "failure_code": item.get("failure_code"),
        }
        for item in model_result_records
    ]
    accepted_expression_causal_chains: list[dict[str, object]] = []
    raw_chains = final_replay.get("accepted_expression_chains")
    for chain in raw_chains if isinstance(raw_chains, list) else []:
        if not isinstance(chain, dict):
            continue
        request_hash = chain.get("request_hash")
        trigger_ref = chain.get("trigger_ref")
        attempt_id = chain.get("attempt_id")
        related_author_ids = _string_set(chain.get("related_author_model_call_ids"))
        model_call_id = chain.get("model_call_id")
        if isinstance(model_call_id, str):
            related_author_ids.add(model_call_id)
        provider_evidence = (
            request_evidence_by_hash.get(request_hash) if isinstance(request_hash, str) else None
        )
        source_closure_calls = sorted(
            (
                {
                    "model_call_id": record.get("model_call_id"),
                    "parent_model_call_id": record.get("parent_model_call_id"),
                    "request_hash": record.get("request_hash"),
                    "event_ref": record.get("event_ref"),
                    "event_sequence": record.get("event_sequence"),
                }
                for record in model_result_records
                if isinstance(record.get("request_hash"), str)
                and record["request_hash"] in source_closure_model_hashes
                and record.get("trigger_ref") == trigger_ref
                and record.get("attempt_id") == attempt_id
                and record.get("parent_model_call_id") in related_author_ids
            ),
            key=lambda item: int(item.get("event_sequence") or 0),
        )
        current_self_presented = (
            isinstance(request_hash, str) and request_hash in current_self_model_hashes
        )
        recall_material_presented = (
            isinstance(request_hash, str) and request_hash in recall_material_model_hashes
        )
        enriched = dict(chain)
        enriched.update(
            {
                "current_self_presented": current_self_presented,
                "current_self_state_hash": (
                    provider_evidence.get("current_self_state_hash")
                    if provider_evidence is not None
                    and isinstance(provider_evidence.get("current_self_state_hash"), str)
                    else None
                ),
                "recall_material_presented": recall_material_presented,
                "recall_material_hash": (
                    provider_evidence.get("recall_context_hash")
                    if provider_evidence is not None
                    and isinstance(provider_evidence.get("recall_context_hash"), str)
                    else None
                ),
                "source_closure_model_calls": source_closure_calls,
            }
        )
        accepted_expression_causal_chains.append(enriched)

    recall_selected_accepted_expression_chains = [
        chain
        for chain in accepted_expression_causal_chains
        if chain.get("character_recall_selected") is True
    ]
    source_closure_request_count = int(provider_audit.get("source_closure_request_count") or 0)
    return {
        "model_result_request_hashes": final_replay["model_result_request_hashes"],
        "private_turn_state_proposal_count": final_replay["private_turn_state_proposal_count"],
        "private_turn_state_hashes": final_replay["private_turn_state_hashes"],
        "accepted_private_turn_state_hashes": final_replay["accepted_private_turn_state_hashes"],
        "accepted_expression_chains": final_replay["accepted_expression_chains"],
        "accepted_expression_causal_chains": accepted_expression_causal_chains,
        "recall_selected_accepted_expression_chains": (recall_selected_accepted_expression_chains),
        "recall_trace_count": final_replay["recall_trace_count"],
        "presented_prefetch_count": final_replay["presented_prefetch_count"],
        "appraisal_event_count": appraisal_event_count,
        "affect_event_count": affect_event_count,
        "global_coverage": {
            "scope": "run_wide_not_causal",
            "current_self_provider_request_count": int(
                provider_audit.get("current_self_state_present_count") or 0
            ),
            "recall_material_provider_request_count": int(
                provider_audit.get("recall_material_present_count") or 0
            ),
            "source_closure_provider_request_count": source_closure_request_count,
            "character_selected_recall_model_result_count": len(selected_recall_model_results),
            "presented_prefetch_count": int(final_replay["presented_prefetch_count"]),
            "appraisal_event_count": appraisal_event_count,
            "affect_event_count": affect_event_count,
        },
        "source_closure": {
            "provider_request_count": source_closure_request_count,
            "provider_request_hashes": provider_audit.get(
                "source_closure_request_hashes",
                [],
            ),
            "provider_model_request_hashes": sorted(source_closure_model_hashes),
            "accepted_expression_candidate_count": final_replay[
                "accepted_expression_candidate_count"
            ],
            "provider_effected_expression_proposal_count": final_replay[
                "provider_effected_expression_proposal_count"
            ],
        },
        "correlation_contract": (
            "provider_invocation_request_hash"
            "->ModelResultRecorded"
            "->ProposalRecorded"
            "->ExpressionPlanAccepted"
            "->ActionAuthorized"
            "->ActionSettlement"
            "->ExecutionReceiptRecorded"
        ),
        "correlated_expression_request_hashes": sorted(causal_model_hashes & accepted_model_hashes),
        "accepted_character_choice_count": final_replay["accepted_character_choice_count"],
        "accepted_character_choices": final_replay["accepted_character_choices"],
        "accepted_private_turn_state_count": final_replay["accepted_private_turn_state_count"],
        "accepted_character_choice_request_hashes": sorted(
            accepted_character_choice_request_hashes
        ),
        "current_self_correlated_character_choice_request_hashes": sorted(
            accepted_character_choice_request_hashes & current_self_model_hashes
        ),
        # Deliberately excludes prompts, responses and visible text. This is
        # enough to distinguish an aggregate validation failure from the exact
        # leaf provider/route that failed after the isolated process exits.
        "sanitized_model_result_diagnostics": sanitized_model_result_diagnostics,
    }


def _wait_for_durable_provider_acceptance_count(
    database: Path,
    *,
    expected_count: int,
    timeout_seconds: float = 5.0,
) -> None:
    """Wait for the exact effect-once provider terminal to cross the ledger.

    The ingress HTTP response may legitimately stop waiting at its bounded
    dispatch deadline while the process-owned Action drain finishes the
    already-started receipt commit.  Process acceptance must observe that
    durable terminal before stopping the daemon; OneBot capture alone is too
    early because it precedes the ledger CAS.
    """

    deadline = time.monotonic() + timeout_seconds
    last_count = 0
    database_uri = database.resolve().as_uri() + "?mode=ro"
    while time.monotonic() < deadline:
        try:
            with sqlite3.connect(
                database_uri,
                uri=True,
                timeout=0.5,
            ) as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM world_v2_events
                    WHERE world_id = ?
                      AND json_extract(event_json, '$.event_type')
                          = 'ActionProviderAccepted'
                    """,
                    (qq_c2c_world_id(_PRIMARY_USER_ID),),
                ).fetchone()
            last_count = int(row[0]) if row is not None else 0
        except sqlite3.OperationalError:
            time.sleep(0.02)
            continue
        if last_count > expected_count:
            raise RuntimeError(
                "effect-once provider acceptance count exceeded the expected "
                f"terminal count ({last_count} > {expected_count})"
            )
        if last_count == expected_count:
            return
        time.sleep(0.02)
    raise TimeoutError(
        f"provider acceptance did not reach its durable terminal ({last_count} != {expected_count})"
    )


def _write_report(path: Path, document: dict[str, object]) -> None:
    resolved = path.expanduser().resolve()
    if resolved.exists():
        raise ValueError("isolated daemon acceptance output must not already exist")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(
        resolved,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _ci_environment_detected() -> bool:
    return any(
        str(os.environ.get(name) or "").strip().lower() not in {"", "0", "false", "no", "off"}
        for name in (
            "CI",
            "GITHUB_ACTIONS",
            "GITLAB_CI",
            "BUILDKITE",
            "TF_BUILD",
            "JENKINS_URL",
            "CIRCLECI",
            "TRAVIS",
            "APPVEYOR",
            "BITBUCKET_BUILD_NUMBER",
            "TEAMCITY_VERSION",
            "DRONE",
            "CODEBUILD_BUILD_ID",
        )
    )


def _network_topology(
    *,
    model_mode: _ModelMode,
    upstream_base_url: str | None,
    production_source_authority: bool = False,
    openai_base_url: str | None = None,
    openrouter_base_url: str | None = None,
) -> dict[str, object]:
    """Describe each network boundary without conflating daemon and model I/O."""

    if production_source_authority and model_mode != "real-provider":
        raise ValueError("production source authority topology requires real-provider mode")
    if model_mode == "fake":
        model_gateway_scope = "in_process"
        model_upstream_scope = "none"
        external_model_network = False
    elif model_mode == "loopback-stub":
        model_gateway_scope = "loopback_stub"
        model_upstream_scope = "none"
        external_model_network = False
    else:
        if upstream_base_url is None:
            raise ValueError("real-provider topology requires an upstream URL")
        parsed = urlparse(upstream_base_url)
        loopback_upstream = (parsed.hostname or "").lower() in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
        model_gateway_scope = "loopback_hash_proxy"
        model_upstream_scope = (
            "loopback_configured_provider" if loopback_upstream else "external_https"
        )
        external_model_network = not loopback_upstream
    topology: dict[str, object] = {
        "daemon_http_scope": "loopback",
        "onebot_provider_scope": "loopback_capture",
        "model_gateway_scope": model_gateway_scope,
        "model_upstream_scope": model_upstream_scope,
        "external_model_network": external_model_network,
        "aggregate_loopback_only": not external_model_network,
    }
    if not production_source_authority:
        return topology
    if openai_base_url is None or openrouter_base_url is None:
        raise ValueError("production source authority topology requires OpenAI and OpenRouter URLs")
    openai_scope = _provider_endpoint_scope(openai_base_url)
    openrouter_scope = _provider_endpoint_scope(openrouter_base_url)
    reviewer_external = openai_scope == "external_https" and openrouter_scope == "external_https"
    topology.update(
        {
            "model_hash_capture_coverage": "partial_deepseek_only",
            "source_authority_network": {
                "enabled": True,
                "reviewer_transport_scope": (
                    "direct_external_https" if reviewer_external else "direct_configured_routes"
                ),
                "captured_by_deepseek_hash_proxy": False,
                "openai_endpoint_scope": openai_scope,
                "openrouter_endpoint_scope": openrouter_scope,
            },
            "external_model_network": (
                external_model_network
                or openai_scope == "external_https"
                or openrouter_scope == "external_https"
            ),
        }
    )
    topology["aggregate_loopback_only"] = not bool(topology["external_model_network"])
    return topology


def _provider_endpoint_scope(base_url: str) -> str:
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if host in {"127.0.0.1", "::1", "localhost"}:
        return "loopback_configured_provider"
    return "external_https" if parsed.scheme == "https" else "external_non_https"


def _validate_provider_base_url(*, label: str, base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{label} must be an absolute HTTP URL")
    if (
        _provider_endpoint_scope(base_url) != "loopback_configured_provider"
        and parsed.scheme != "https"
    ):
        raise ValueError(f"{label} non-loopback endpoint must use HTTPS")


def _validated_provider_settings(
    *,
    model_mode: _ModelMode,
    allow_real_provider: bool,
    production_source_authority: bool = False,
) -> Settings:
    if production_source_authority and not (model_mode == "real-provider" and allow_real_provider):
        raise ValueError(
            "--production-source-authority is valid only with "
            "--model-mode real-provider and --allow-real-provider"
        )
    if model_mode == "real-provider" and not allow_real_provider:
        raise ValueError("real-provider mode requires explicit --allow-real-provider opt-in")
    if model_mode != "real-provider" and allow_real_provider:
        raise ValueError("--allow-real-provider is valid only with --model-mode real-provider")
    if model_mode == "real-provider" and _ci_environment_detected():
        raise ValueError("real-provider mode is manual-only and refuses CI environments")
    settings = Settings()
    if model_mode != "real-provider":
        return settings
    if not settings.deepseek_api_key:
        raise ValueError("real-provider mode requires DEEPSEEK_API_KEY")
    _validate_provider_base_url(
        label="real-provider DEEPSEEK_BASE_URL",
        base_url=settings.deepseek_base_url,
    )
    if production_source_authority:
        if not settings.openai_api_key:
            raise ValueError("production source authority requires OPENAI_API_KEY")
        if not settings.openrouter_api_key:
            raise ValueError("production source authority requires OPENROUTER_API_KEY")
        _validate_provider_base_url(
            label="production source authority OPENAI_BASE_URL",
            base_url=settings.openai_base_url,
        )
        _validate_provider_base_url(
            label="production source authority OPENROUTER_BASE_URL",
            base_url=settings.openrouter_base_url,
        )
    return settings


def run(
    *,
    output: Path,
    startup_timeout_seconds: float,
    model_mode: _ModelMode = "fake",
    allow_real_provider: bool = False,
    production_source_authority: bool = False,
) -> dict[str, object]:
    if model_mode not in {"fake", "loopback-stub", "real-provider"}:
        raise ValueError(f"unsupported model mode: {model_mode}")
    settings = _validated_provider_settings(
        model_mode=model_mode,
        allow_real_provider=allow_real_provider,
        production_source_authority=production_source_authority,
    )
    ambient_database = Path(settings.database_path).expanduser().resolve()
    upstream_base_url = settings.deepseek_base_url if model_mode == "real-provider" else None
    network_topology = _network_topology(
        model_mode=model_mode,
        upstream_base_url=upstream_base_url,
        production_source_authority=production_source_authority,
        openai_base_url=(settings.openai_base_url if production_source_authority else None),
        openrouter_base_url=(settings.openrouter_base_url if production_source_authority else None),
    )
    with tempfile.TemporaryDirectory(prefix="girl-agent-daemon-acceptance-") as raw_temp:
        temporary_root = Path(raw_temp).resolve()
        database = temporary_root / "isolated-world.sqlite"
        if database.resolve() == ambient_database:
            raise ValueError("isolated acceptance database resolved to production")
        attachment_cache = temporary_root / "attachments"
        log_path = temporary_root / "daemon.log"
        daemon_logs: list[str] = []
        process_start_count = 0

        with _capture_server() as (capture, capture_url):
            if not capture_url.startswith("http://127.0.0.1:"):
                raise RuntimeError("capture provider must bind exact IPv4 loopback")
            provider_manager = (
                _provider_capture_server(
                    mode=model_mode,
                    upstream_base_url=upstream_base_url,
                )
                if model_mode in _PROVIDER_MODES
                else nullcontext((None, None))
            )
            with provider_manager as (provider_capture, provider_capture_url):
                if model_mode in _PROVIDER_MODES and not isinstance(
                    provider_capture, _ProviderCaptureState
                ):
                    raise RuntimeError("provider capture did not start")
                first = _start_daemon(
                    database=database,
                    capture_url=capture_url,
                    attachment_cache=attachment_cache,
                    log_path=log_path,
                    model_mode=model_mode,
                    provider_capture_url=provider_capture_url,
                    production_source_authority=production_source_authority,
                )
                process_start_count += 1
                pre_restart_turns: list[dict[str, object]] = []
                burst_turns: tuple[dict[str, object], ...] = ()
                interruption: dict[str, object] | None = None
                try:
                    first_health = _wait_for_health(
                        first,
                        timeout_seconds=startup_timeout_seconds,
                    )
                    first_turn = _post_turn(
                        daemon=first,
                        source_event_id="isolated-daemon-inbound-1",
                        text="第一轮：我今天把那件拖了很久的事做完了。",
                    )
                    pre_restart_turns.append(first_turn)
                    if model_mode == "fake":
                        _wait_for_durable_provider_acceptance_count(
                            database,
                            expected_count=1,
                        )
                        second_turn = _post_turn(
                            daemon=first,
                            source_event_id="isolated-daemon-inbound-2",
                            text="第二轮：刚才说的事让我终于松了口气。",
                        )
                        pre_restart_turns.append(second_turn)
                        _wait_for_durable_provider_acceptance_count(
                            database,
                            expected_count=2,
                        )
                        duplicate_source_event_id = "isolated-daemon-inbound-2"
                        duplicate_text = "第二轮：刚才说的事让我终于松了口气。"
                    else:
                        if not isinstance(provider_capture, _ProviderCaptureState):
                            raise RuntimeError("provider stress run has no capture state")
                        burst_turns = _run_burst(first)
                        pre_restart_turns.extend(burst_turns)
                        interruption = _run_interruption(
                            daemon=first,
                            database=database,
                            provider_capture=provider_capture,
                        )
                        first_interruption = interruption["first_turn"]
                        second_interruption = interruption["second_turn"]
                        if not isinstance(first_interruption, dict) or not isinstance(
                            second_interruption, dict
                        ):
                            raise RuntimeError("interruption run returned malformed turns")
                        pre_restart_turns.extend((first_interruption, second_interruption))
                        duplicate_source_event_id = "isolated-daemon-interruption-2"
                        duplicate_text = (
                            f"{_INTERRUPTION_SECOND_MARKER}：打断一下，我先更正刚才最关键的一点。"
                        )
                    if any(turn.get("http_status") != 200 for turn in pre_restart_turns):
                        raise RuntimeError(
                            "isolated daemon rejected a pre-restart turn "
                            + json.dumps(pre_restart_turns, ensure_ascii=False)
                            + "\n"
                            + first.log_tail()
                        )
                finally:
                    first.stop()
                    daemon_logs.append(first.log_tail())

                visible_before_restart = capture.visible_count()
                first_replay = _cold_replay(database)

                second = _start_daemon(
                    database=database,
                    capture_url=capture_url,
                    attachment_cache=attachment_cache,
                    log_path=log_path,
                    model_mode=model_mode,
                    provider_capture_url=provider_capture_url,
                    production_source_authority=production_source_authority,
                )
                process_start_count += 1
                try:
                    second_health = _wait_for_health(
                        second,
                        timeout_seconds=startup_timeout_seconds,
                    )
                    visible_before_duplicate = capture.visible_count()
                    model_requests_before_duplicate = (
                        len(provider_capture.snapshot())
                        if isinstance(provider_capture, _ProviderCaptureState)
                        else 0
                    )
                    duplicate_turn = _post_turn(
                        daemon=second,
                        source_event_id=duplicate_source_event_id,
                        text=duplicate_text,
                    )
                    # Give a wrongly detached Action task one bounded chance to
                    # expose a replay duplicate.
                    time.sleep(0.2)
                    visible_after_duplicate = capture.visible_count()
                    model_requests_after_duplicate = (
                        len(provider_capture.snapshot())
                        if isinstance(provider_capture, _ProviderCaptureState)
                        else 0
                    )
                    new_source_event_id = (
                        "isolated-daemon-inbound-3"
                        if model_mode == "fake"
                        else "isolated-daemon-post-restart"
                    )
                    new_turn = _post_turn(
                        daemon=second,
                        source_event_id=new_source_event_id,
                        text="重启之后还能从刚才停下的地方接着聊吗？",
                    )
                    if duplicate_turn["http_status"] != 200 or new_turn["http_status"] != 200:
                        raise RuntimeError("isolated daemon rejected a post-restart turn")
                    if model_mode == "fake":
                        _wait_for_durable_provider_acceptance_count(
                            database,
                            expected_count=3,
                        )
                    visible_after_new_turn = capture.visible_count()
                finally:
                    second.stop()
                    daemon_logs.append(second.log_tail())

                final_replay = _cold_replay(database)
                expected_sources = [str(turn["source_event_id"]) for turn in pre_restart_turns] + [
                    new_source_event_id
                ]
                duplicate_effects = visible_after_duplicate - visible_before_duplicate
                duplicate_model_requests = (
                    model_requests_after_duplicate - model_requests_before_duplicate
                )
                new_turn_effects = visible_after_new_turn - visible_after_duplicate
                if model_mode != "real-provider" and visible_before_restart < 1:
                    raise RuntimeError(
                        "pre-restart turns did not reach the isolated provider capture"
                    )
                if model_mode != "real-provider" and new_turn_effects < 1:
                    raise RuntimeError("new post-restart turn produced no visible provider effect")
                replay_sources = final_replay["source_event_ids"]
                if not isinstance(replay_sources, list) or (
                    set(replay_sources) != set(expected_sources)
                    or len(replay_sources) != len(expected_sources)
                ):
                    raise RuntimeError("cold replay did not retain every submitted source identity")

                provider_audit = (
                    provider_capture.report()
                    if isinstance(provider_capture, _ProviderCaptureState)
                    else {
                        "contract": "provider-presentation-capture.1",
                        "capture_mode": "disabled_fake",
                        "raw_prompt_retained": False,
                        "raw_response_retained": False,
                        "request_count": 0,
                    }
                )
                if production_source_authority:
                    provider_audit = {
                        **provider_audit,
                        "model_traffic_capture_coverage": ("partial_deepseek_only"),
                        "all_model_traffic_hash_captured": False,
                        "uncaptured_model_traffic": (
                            "OpenRouter/OpenAI source-authority calls use "
                            "direct configured provider routes"
                        ),
                    }
                causal_audit = build_causal_audit(
                    final_replay=final_replay,
                    provider_audit=provider_audit,
                )
                source_authority_acceptance = (
                    _source_authority_acceptance_report(
                        requested=True,
                        first_health=first_health,
                        restart_health=second_health,
                        final_replay=final_replay,
                    )
                    if production_source_authority
                    else None
                )
                burst_source_ids = [str(turn["source_event_id"]) for turn in burst_turns]
                observation_source_groups = final_replay["observation_source_event_id_groups"]
                burst_source_set = set(burst_source_ids)
                matching_burst_groups = [
                    [str(value) for value in group]
                    for group in observation_source_groups
                    if isinstance(group, list)
                    and burst_source_set.issubset(str(value) for value in group)
                ]
                coalesced_observation_source_event_ids = (
                    matching_burst_groups[0] if len(matching_burst_groups) == 1 else []
                )
                burst_action_ids = list(
                    dict.fromkeys(
                        str(outcome["world_action_id"])
                        for turn in burst_turns
                        for outcome in (turn.get("daemon_outcome"),)
                        if isinstance(outcome, dict)
                        and isinstance(outcome.get("world_action_id"), str)
                    )
                )
                interruption_report = (
                    {
                        **interruption,
                        "latest_source_retained": (
                            "isolated-daemon-interruption-2" in replay_sources
                        ),
                    }
                    if interruption is not None
                    else {
                        "marker_reached_provider": False,
                        "second_ingress_started": False,
                        "second_ingress_committed": False,
                        "second_ingress_reached_provider": False,
                        "overlap_observed_at_second_provider_entry": False,
                        "first_provider_in_flight_when_second_reached_provider": False,
                        "second_ingress_started_before_first_completed": False,
                        "overlap_observed": False,
                        "latest_source_retained": False,
                    }
                )
                all_latency_turns = [
                    *pre_restart_turns,
                    new_turn,
                ]
                model_provider_network = {
                    "fake": "in_process_fake",
                    "loopback-stub": "loopback_stub",
                    "real-provider": (
                        ("mixed_deepseek_via_loopback_hash_proxy_and_direct_source_authority")
                        if production_source_authority
                        else (
                            "external_provider_via_loopback_hash_proxy"
                            if network_topology["external_model_network"] is True
                            else "loopback_provider_via_loopback_hash_proxy"
                        )
                    ),
                }[model_mode]
                combined_log_tail = "\n".join(daemon_logs)[-8_000:]
                report = {
                    "contract": (
                        "isolated-daemon-process-acceptance.1"
                        if model_mode == "fake"
                        else "isolated-daemon-process-acceptance.2"
                    ),
                    "generated_at": datetime.now(UTC).isoformat(),
                    "safety": {
                        "capture_transport_only": True,
                        "loopback_only": network_topology["aggregate_loopback_only"],
                        "onebot_loopback_only": True,
                        "production_database_touched": False,
                        "real_qq_send_possible": False,
                        "daemon_proxy_bypass_enforced": True,
                        "real_provider_https_guard_enforced": True,
                        "model_provider_network": model_provider_network,
                        "network_topology": network_topology,
                    },
                    "assessment_policy": {
                        "manual_observation_only": True,
                        "wording_quality_gate": False,
                        "character_choice_gate": False,
                        "ci_real_provider_calls": False,
                        "ci_environment_detected": _ci_environment_detected(),
                        "real_provider_ci_guard_enforced": True,
                        "real_provider_execution_policy": "manual_only",
                    },
                    "daemon": {
                        "entrypoint": (
                            "scripts.run_isolated_daemon_acceptance:"
                            "_serve_isolated_loopback_daemon"
                            if model_mode == "loopback-stub"
                            else "companion_daemon.napcat_cli"
                        ),
                        "command_mode": (
                            "explicit-test-authorities"
                            if model_mode == "loopback-stub"
                            else (
                                "--fake --world-v2-c2c"
                                if model_mode == "fake"
                                else "--world-v2-c2c"
                            )
                        ),
                        "model_mode": model_mode,
                        "fake_cli_flag_used": model_mode == "fake",
                        "test_only_semantic_authority_injection": (
                            model_mode == "loopback-stub"
                        ),
                        "semantic_authorities": (
                            {
                                "role": _LOOPBACK_ROLE_AUTHORITY,
                                "source_reviewer": _LOOPBACK_REVIEW_AUTHORITY,
                                "life_source_reviewer": (
                                    _LOOPBACK_LIFE_REVIEW_AUTHORITY
                                ),
                                "review_contracts": _LOOPBACK_REVIEW_CONTRACTS,
                                "life_review_contracts": (
                                    _LOOPBACK_LIFE_REVIEW_CONTRACTS
                                ),
                            }
                            if model_mode == "loopback-stub"
                            else None
                        ),
                        "real_provider_explicitly_allowed": (
                            model_mode == "real-provider" and allow_real_provider
                        ),
                        "production_source_authority_enabled": (production_source_authority),
                        "model_scope": (
                            "fake_model_for_process_reliability_only"
                            if model_mode == "fake"
                            else (
                                "loopback_stub_exercises_provider_http_boundary"
                                if model_mode == "loopback-stub"
                                else (
                                    "manual_real_provider_observation_only;"
                                    "never_a_wording_quality_gate"
                                )
                            )
                        ),
                        "process_start_count": process_start_count,
                        "temporary_database": True,
                        **(
                            {"combined_log_tail": combined_log_tail}
                            if model_mode == "fake"
                            else {
                                "combined_log_tail_hash": hashlib.sha256(
                                    combined_log_tail.encode("utf-8")
                                ).hexdigest(),
                                "combined_log_tail_bytes": len(combined_log_tail.encode("utf-8")),
                            }
                        ),
                    },
                    "liveness": {
                        "first_start": {
                            "status": first_health.get("status"),
                            "scheduler": first_health.get("scheduler"),
                        },
                        "after_restart": {
                            "status": second_health.get("status"),
                            "scheduler": second_health.get("scheduler"),
                        },
                    },
                    "continuity": {
                        "submitted_source_event_ids": expected_sources,
                        "cold_replay_source_event_ids": replay_sources,
                        "first_shutdown_replay_source_event_ids": first_replay["source_event_ids"],
                        "duplicate_after_restart_visible_effect_count": (duplicate_effects),
                        "duplicate_after_restart_model_request_count": (duplicate_model_requests),
                        "duplicate_source_event_id": duplicate_source_event_id,
                        "duplicate_source_persisted_once": (
                            replay_sources.count(duplicate_source_event_id) == 1
                        ),
                        "new_turn_after_restart_visible_effect_count": (new_turn_effects),
                        "visible_effect_count_before_restart": (visible_before_restart),
                        "visible_effect_count_after_restart": (visible_after_new_turn),
                        "cold_replay_matches_live_head": final_replay[
                            "persisted_projection_matches_independent_replay"
                        ],
                        "first_shutdown_cursor": first_replay["cursor"],
                        "final_cursor": final_replay["cursor"],
                        "final_semantic_hash": final_replay["semantic_hash"],
                        "final_event_count": final_replay["event_count"],
                        "final_action_count": final_replay["action_count"],
                        "provider_accepted_action_count": final_replay[
                            "provider_accepted_action_count"
                        ],
                    },
                    "interaction_stress": {
                        "burst": {
                            "source_event_ids": burst_source_ids,
                            "turns": list(burst_turns),
                            "distinct_world_action_ids": burst_action_ids,
                            "coalesced_into_single_action": len(burst_action_ids) == 1,
                            "coalesced_observation_source_event_ids": (
                                coalesced_observation_source_event_ids
                            ),
                            "all_sources_retained": set(burst_source_ids).issubset(
                                set(replay_sources)
                            ),
                        },
                        "interruption": interruption_report,
                    },
                    "provider_presentation_audit": provider_audit,
                    "causal_audit": causal_audit,
                    **(
                        {"source_authority_acceptance": (source_authority_acceptance)}
                        if source_authority_acceptance is not None
                        else {}
                    ),
                    "latency": {
                        "measurement": (
                            "loopback_http_request_to_daemon_response_including_"
                            "captured_provider_acceptance"
                        ),
                        "model_provider": model_provider_network,
                        "turns": all_latency_turns,
                        "duplicate_after_restart": duplicate_turn,
                    },
                    "captured_provider_effects": list(
                        capture.report_snapshot(hash_only=model_mode in _PROVIDER_MODES)
                    ),
                }
                report["deterministic_invariants"] = evaluate_deterministic_invariants(
                    report=report,
                    model_mode=model_mode,
                )

    _write_report(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Start the real QQ daemon twice against a temporary capture-only "
            "OneBot provider and report restart continuity."
        )
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--startup-timeout-seconds",
        type=float,
        default=45.0,
    )
    parser.add_argument(
        "--model-mode",
        choices=("fake", "loopback-stub", "real-provider"),
        default="fake",
        help=(
            "fake is deterministic; loopback-stub exercises the provider HTTP "
            "boundary; real-provider is manual and requires a second opt-in flag"
        ),
    )
    parser.add_argument(
        "--allow-real-provider",
        action="store_true",
        help=(
            "explicitly permit real model API use; OneBot remains loopback "
            "capture-only and the report is never a wording quality gate"
        ),
    )
    parser.add_argument(
        "--production-source-authority",
        action="store_true",
        help=(
            "add the production OpenRouter/OpenAI Inventory/Coverage authority "
            "lanes; reviewer traffic is direct external and is not captured by "
            "the DeepSeek hash proxy"
        ),
    )
    args = parser.parse_args()
    if not 5 <= args.startup_timeout_seconds <= 120:
        parser.error("startup timeout must be between 5 and 120 seconds")
    if args.production_source_authority and not (
        args.model_mode == "real-provider" and args.allow_real_provider
    ):
        parser.error(
            "--production-source-authority is valid only with "
            "--model-mode real-provider and --allow-real-provider"
        )
    if args.model_mode == "real-provider" and not args.allow_real_provider:
        parser.error("--model-mode real-provider requires explicit --allow-real-provider")
    if args.model_mode != "real-provider" and args.allow_real_provider:
        parser.error("--allow-real-provider is valid only with --model-mode real-provider")
    report = run(
        output=args.output,
        startup_timeout_seconds=args.startup_timeout_seconds,
        model_mode=args.model_mode,
        allow_real_provider=args.allow_real_provider,
        production_source_authority=args.production_source_authority,
    )
    print(json.dumps(report, ensure_ascii=False))
    return deterministic_acceptance_exit_code(
        report=report,
        model_mode=args.model_mode,
    )


if __name__ == "__main__":
    raise SystemExit(main())
