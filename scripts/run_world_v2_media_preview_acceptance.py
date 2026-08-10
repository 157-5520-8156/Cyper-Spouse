#!/usr/bin/env python
"""Qualify the isolated World v2 media production chain.

This harness follows the current production topology:

    source-bound evidence -> candidate -> CharacterInterior required tool
    -> Proposal/Acceptance -> planning -> render -> visual inspection
    -> MediaPreviewGenerated -> restart/replay/effect-once checks

It deliberately stops before QQ delivery.  Every database, provider sidecar,
rendered file, and report is placed below one fresh ``/tmp`` directory.  A
real CharacterInterior selection is always attempted first.  The optional
``ACCEPTANCE_DETERMINISTIC_SELECTION=1`` switch can only create a second,
downstream qualification scratch world after that real role has legally
returned ``no_op``; it never bypasses the role in the same world.

Run from the repository root with the project environment, for example:

    /Users/geoff/homebrew/Caskroom/miniconda/base/bin/python \
      scripts/run_world_v2_media_preview_acceptance.py

The final JSON report prints the scratch paths and is also kept in the
scratch directory so the generated image and durable evidence can be audited
after the process exits.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

# Scratch qualification is an explicit offline/test root.  This does not
# enable any external dispatch; it only allows the ledger's root authority to
# be provisioned in the isolated SQLite file.
os.environ.setdefault("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")

from companion_daemon.config import Settings  # noqa: E402
from companion_daemon.llm import DeepSeekChatModel, ModelCallUsage  # noqa: E402
from companion_daemon.world_v2.activity_plan_runtime import (  # noqa: E402
    ActivityPlanCommand,
    ActivityPlanTransitionCommand,
)
from companion_daemon.world_v2.character_interior.production import (  # noqa: E402
    compose_production_character_interior,
)
from companion_daemon.world_v2.character_interior.structured_role_tool_contract import (  # noqa: E402
    StructuredRoleToolContracts,
)
from companion_daemon.world_v2.companion_identity import CompanionIdentityFrame  # noqa: E402
from companion_daemon.world_v2.deliberation import ModelRoute  # noqa: E402
from companion_daemon.world_v2.event_ecology_media import EcologyPolicy  # noqa: E402
from companion_daemon.world_v2.expression_draft import (  # noqa: E402
    PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
)
from companion_daemon.world_v2.image_evidence_contract import ImageEvidenceV1  # noqa: E402
from companion_daemon.world_v2.image_evidence_runtime import (  # noqa: E402
    ImageEvidenceDeclarationCommand,
)
from companion_daemon.world_v2.media_authority_provisioning import (  # noqa: E402
    MediaAuthorityProvisioner,
)
from companion_daemon.world_v2.media_delivery_runtime import (  # noqa: E402
    require_current_media_delivery_approval,
)
from companion_daemon.world_v2.media_v2 import (  # noqa: E402
    MediaAutomaticDeliveryApproval,
)
from companion_daemon.world_v2.production_turn_application import (  # noqa: E402
    MediaSelectionAcceptanceComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.qq_media_deployment import (  # noqa: E402
    build_qq_media_preview_deployment,
)
from companion_daemon.world_v2.replay_evaluator import ReplayEvaluator  # noqa: E402
from companion_daemon.world_v2.schemas import (  # noqa: E402
    Action,
    LedgerProjection,
    MediaDeliveryApprovalBinding,
    ProviderMediaGrantBinding,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger  # noqa: E402


WORLD_ID = "world:media-preview-acceptance"
RECIPIENT_REF = "user:acceptance"
INBOUND_OBSERVATION_ID = "observation:acceptance:acceptance:message:acceptance"
MEDIA_ACTION_KINDS = frozenset({"media_render", "media_repair", "media_inspection"})
MAX_REAL_RENDER_ATTEMPTS = 2
RESTART_REPORT_FIELDS = (
    "provider_dispatch_rows_unchanged",
    "provider_receipt_ids_unchanged",
    "same_idempotency_not_resent",
    "planning_result_rows_unchanged",
    "planning_same_idempotency_not_resent",
    "artifact_bytes_unchanged",
    "artifact_hash_unchanged",
    "inspection_result_unchanged",
    "inspection_hash_unchanged",
    "projection_semantic_hash_unchanged",
    "cold_projection_matches_cold_replay",
    "cold_projection_semantic_hash_matches_replay",
    "repeat_drain_preview_count",
)


@contextmanager
def _cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return (f"user:{platform_user_id}", RECIPIENT_REF)


class _Router:
    async def route(self, _request: object) -> ModelRoute:
        return ModelRoute(
            tier="flash",
            reason_code="media_preview_acceptance",
            router_version="media-preview-qualification.1",
        )


class _NoDeliveryTransport:
    """Platform sink that makes any accidental QQ/text dispatch fail loudly."""

    provider = "platform:acceptance-null"

    async def send(self, request: object) -> object:
        kind = getattr(request, "kind", "unknown")
        raise AssertionError(f"isolated media qualification must not deliver kind={kind}")

    async def lookup(self, **_kwargs: object) -> None:
        return None


def _new_scratch_root() -> Path:
    """Create one fresh, explicitly bounded acceptance root below ``/tmp``."""

    root = Path(tempfile.mkdtemp(prefix="girl-agent-wt-e.", dir="/tmp"))
    if root.parent.resolve() != Path("/tmp").resolve():
        raise RuntimeError("acceptance scratch root escaped /tmp")
    return root


def _require_scratch_path(path: Path, scratch_root: Path) -> Path:
    """Return a resolved scratch path or reject production/shared locations."""

    resolved = Path(path).resolve()
    root = Path(scratch_root).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("path is outside the isolated scratch root") from exc
    return resolved


def _qualification_fields(
    *, status: object, deterministic_selection_double: bool
) -> dict[str, object]:
    """Derive qualification flags without allowing a failed phase to qualify."""

    if deterministic_selection_double:
        scope = "downstream_provider_stages_only"
    elif status == "qualification_complete":
        scope = "full_chain"
    else:
        scope = "character_selection_only"
    complete = status == "qualification_complete" and not deterministic_selection_double
    return {
        "status": status,
        "qualification_scope": scope,
        "character_selection_qualified": complete,
        "deterministic_selection_double": deterministic_selection_double,
        "qualification_complete": complete,
    }


def _provider_failure_did_not_become_role_no_op(
    selection_report: Mapping[str, object], failure_events: Sequence[object]
) -> bool:
    """Keep technical provider failure distinct from a CharacterInterior no-op."""

    return bool(failure_events) and selection_report.get("status") == "proposed" and (
        selection_report.get("decision") != "no_op"
    )


def _restart_report_is_complete(report: Mapping[str, object]) -> bool:
    return all(field in report for field in RESTART_REPORT_FIELDS)


def _artifact_report_is_valid(
    artifact: Mapping[str, object] | object, *, scratch_root: Path
) -> bool:
    if not isinstance(artifact, Mapping):
        return False
    path_value = artifact.get("path")
    if not isinstance(path_value, str):
        return False
    try:
        path = _require_scratch_path(Path(path_value), scratch_root)
    except ValueError:
        return False
    if not path.is_file():
        return False
    data = path.read_bytes()
    actual_hash = "sha256:" + hashlib.sha256(data).hexdigest()
    dimensions = artifact.get("dimensions")
    mime_type = artifact.get("mime_type")
    return (
        bool(data)
        and artifact.get("bytes") == len(data)
        and artifact.get("sha256") == actual_hash
        and isinstance(mime_type, str)
        and mime_type.startswith("image/")
        and isinstance(dimensions, Mapping)
        and isinstance(dimensions.get("width"), int)
        and isinstance(dimensions.get("height"), int)
        and dimensions["width"] > 0
        and dimensions["height"] > 0
    )


def _render_attempt_within_limit(attempt_count: int) -> bool:
    return 0 <= attempt_count < MAX_REAL_RENDER_ATTEMPTS


class _ObservedRoleModel:
    """A CharacterInterior provider wrapper that records only safe role facts."""

    def __init__(
        self,
        delegate: object,
        *,
        usage: list[ModelCallUsage] | None = None,
    ) -> None:
        self._delegate = delegate
        self.usage = usage if usage is not None else []
        self.media_attempts: list[dict[str, object]] = []
        self.model = str(getattr(delegate, "model", "character-interior-role"))
        self.provider = str(getattr(delegate, "provider", "unknown"))
        self.model_version = str(getattr(delegate, "model_version", self.model))
        self.supports_required_tool_choice = bool(
            getattr(delegate, "supports_required_tool_choice", False)
        )
        self.supports_strict_tool_choice = bool(
            getattr(delegate, "supports_strict_tool_choice", False)
        )
        self.reports_exact_request_emission = bool(
            getattr(delegate, "reports_exact_request_emission", False)
        )

    @staticmethod
    def _media_context(messages: Sequence[Mapping[str, object]]) -> tuple[dict[str, object], dict[str, object]]:
        if not messages:
            raise ValueError("media role request has no messages")
        raw_content = messages[-1].get("content")
        if not isinstance(raw_content, str):
            raise ValueError("media role request content is not text")
        decoded = json.loads(raw_content)
        if not isinstance(decoded, dict):
            raise ValueError("media role request payload is not an object")
        manifest = decoded.get("capability_manifest")
        if not isinstance(manifest, dict):
            raise ValueError("media role request has no capability manifest")
        payload = manifest.get("payload")
        source_refs = manifest.get("source_refs")
        if not isinstance(payload, dict) or not isinstance(source_refs, list):
            raise ValueError("media role request capability manifest is malformed")
        return manifest, {"payload": payload, "source_refs": source_refs}

    @staticmethod
    def _tool_name(tools: Sequence[Mapping[str, object]] | None) -> str | None:
        if not tools:
            return None
        function = tools[0].get("function")
        return str(function.get("name")) if isinstance(function, dict) else None

    @staticmethod
    def _tool_recall_allowed(tools: Sequence[Mapping[str, object]] | None) -> bool:
        if not tools or not isinstance(tools[0].get("function"), dict):
            return False
        parameters = tools[0]["function"].get("parameters")
        branches = parameters.get("anyOf") if isinstance(parameters, dict) else None
        if not isinstance(branches, list):
            return False
        for branch in branches:
            if not isinstance(branch, dict) or not isinstance(branch.get("properties"), dict):
                continue
            status = branch["properties"].get("status")
            values = status.get("enum") if isinstance(status, dict) else None
            if isinstance(values, list) and "recall_request" in values:
                return True
        return False

    def _capture_contract(
        self,
        messages: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]] | None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        manifest, context = self._media_context(messages)
        tool_name = self._tool_name(tools)
        if tool_name != "character_role_media_selection_v1":
            raise ValueError(f"unexpected CharacterInterior tool {tool_name!r}")
        source_refs = tuple(str(item) for item in context["source_refs"])
        # The live provider contract is supplied as a tool, not as a prompt
        # claim.  Recompile its identity from the exact capability manifest so
        # the report can prove schema/capability/contract closure without
        # retaining the prompt or candidate descriptions.
        recall_allowed = self._tool_recall_allowed(tools)
        contract = StructuredRoleToolContracts().media_selection(
            capability_payload=context["payload"],
            source_refs=source_refs,
            recall_allowed=recall_allowed,
        )
        identity = asdict(contract.identity)
        attempt = {
            "tool_name": tool_name,
            "contract_identity": identity,
            "capability_ref": manifest.get("capability_ref"),
            "capability_payload_hash": manifest.get("payload_hash"),
            "source_refs": list(source_refs),
        }
        self.media_attempts.append(attempt)
        return attempt, context

    def _record_response(self, raw: str) -> None:
        if not self.media_attempts:
            return
        current = self.media_attempts[-1]
        current["response_hash"] = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            current["decision_parse_error"] = "response_is_not_json"
            return
        if not isinstance(decoded, dict):
            current["decision_parse_error"] = "response_is_not_object"
            return
        current["status"] = decoded.get("status")
        decision = decoded.get("decision")
        payload = decision.get("payload") if isinstance(decision, dict) else None
        if isinstance(payload, dict):
            current["decision"] = payload.get("decision")
            current["selected_token"] = payload.get("selected_token")
        else:
            current["decision_parse_error"] = "missing_nested_decision_payload"

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        operation = getattr(self._delegate, "complete", None)
        if not callable(operation):
            raise RuntimeError("CharacterInterior role provider has no complete operation")
        return await operation(messages, temperature=temperature)

    async def complete_json(
        self,
        messages: list[dict[str, object]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> str:
        if self._tool_name(tools) == "character_role_media_selection_v1":
            self._capture_contract(messages, tools)
        operation = getattr(self._delegate, "complete_json", None)
        if not callable(operation):
            raise RuntimeError("CharacterInterior role provider has no complete_json operation")
        raw = await operation(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )
        if self.media_attempts and self._tool_name(tools) == "character_role_media_selection_v1":
            self._record_response(raw)
        return raw

    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, dict[str, object]]:
        operation = getattr(self._delegate, "complete_with_usage", None)
        if not callable(operation):
            raise RuntimeError("CharacterInterior role provider has no metered completion")
        return await operation(messages, temperature=temperature)

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, object]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> tuple[str, dict[str, object]]:
        operation = getattr(self._delegate, "complete_json_with_usage", None)
        if not callable(operation):
            raise RuntimeError("CharacterInterior role provider has no metered JSON completion")
        return await operation(
            messages,
            temperature=temperature,
            tools=tools,
            tool_choice=tool_choice,
        )


class _DeterministicSelectionDouble(_ObservedRoleModel):
    """Explicit downstream-only CharacterInterior provider double."""

    def __init__(self, *, text_delegate: _ObservedRoleModel | None = None) -> None:
        super().__init__(
            delegate=text_delegate if text_delegate is not None else object(),
            usage=(text_delegate.usage if text_delegate is not None else []),
        )
        self._text_delegate = text_delegate
        self.model = "acceptance-deterministic-selection-double"
        self.provider = "acceptance"
        self.model_version = "acceptance-deterministic-selection-double.1"
        self.supports_required_tool_choice = True
        self.supports_strict_tool_choice = True
        self.reports_exact_request_emission = True

    async def complete(
        self, _messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        if self._text_delegate is not None:
            return await self._text_delegate.complete(_messages, temperature=temperature)
        raise RuntimeError(
            "deterministic selection double does not author inbound expressions"
        )

    async def complete_json(
        self,
        messages: list[dict[str, object]],
        *,
        temperature: float = 0.8,
        tools: list[dict[str, object]] | None = None,
        tool_choice: object | None = None,
    ) -> str:
        if self._tool_name(tools) != "character_role_media_selection_v1":
            if self._text_delegate is not None:
                return await self._text_delegate.complete_json(
                    messages,
                    temperature=temperature,
                    tools=tools,
                    tool_choice=tool_choice,
                )
            raise RuntimeError(
                "deterministic selection double has no non-media JSON author"
            )
        _attempt, context = self._capture_contract(messages, tools)
        candidates = context["payload"].get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise RuntimeError("deterministic selection double received no candidates")
        first = candidates[0]
        if not isinstance(first, dict) or not isinstance(first.get("token"), str):
            raise RuntimeError("deterministic selection double received a malformed token")
        raw = json.dumps(
            {
                "status": "decision",
                "summary": "下游资格验证使用显式的选择 double。",
                "attended_source_refs": [],
                "decision": {
                    "source_refs": list(context["source_refs"]),
                    "payload": {
                        "decision": "select",
                        "selected_token": first["token"],
                    },
                },
                "recall_query": None,
                "proposals": [],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._record_response(raw)
        return raw


def _role_model(settings: Settings, usage: list[ModelCallUsage]) -> _ObservedRoleModel:
    delegate = DeepSeekChatModel(
        api_key=str(settings.deepseek_api_key),
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        thinking_enabled=False,
        usage_observer=usage.append,
    )
    return _ObservedRoleModel(delegate, usage=usage)


def _new_character_interior(role_model: _ObservedRoleModel):
    return compose_production_character_interior(
        flash_model=role_model,
        thinking_model=None,
        source_closure_model=None,
        report_relative_source_closure_model=None,
        candidate_external_proposition_inventory_model=None,
        source_closure_reselection_lane=None,
        expression_episode_observer_model=None,
        flash_model_id=role_model.model,
        thinking_model_id=None,
        expression_capabilities=PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
        identity_frame=CompanionIdentityFrame(
            companion_name="枝枝",
            counterpart_name="acceptance counterpart",
            personality_frame="有自己的生活、判断和表达选择。",
        ),
    )


def _config(
    *,
    media_bundle: object | None,
    acceptance: MediaSelectionAcceptanceComposition | None = None,
) -> WorldV2TurnApplicationConfig:
    deployment = getattr(media_bundle, "deployment", None)
    selected_acceptance = acceptance if acceptance is not None else (
        getattr(deployment, "acceptance", None) if deployment is not None else None
    )
    continuation = getattr(deployment, "continuation", None) if deployment is not None else None
    return WorldV2TurnApplicationConfig(
        world_id=WORLD_ID,
        companion_actor_ref="agent:companion",
        reply_target=RECIPIENT_REF,
        action_pump_owner="pump:media-preview-acceptance",
        character_memory_enabled=False,
        event_ecology_policy=EcologyPolicy(max_candidates_per_drain=1),
        media_selection_acceptance=selected_acceptance,
        media_continuation=continuation,
        # The QQ factory composes this value for normal deployment, but this
        # harness intentionally leaves it out.  A generated preview is the
        # terminal qualification artifact, not a send authorization.
        media_auto_delivery=None,
    )


def _build_app(
    *,
    database_path: Path,
    now: datetime,
    role_model: _ObservedRoleModel,
    media_bundle: object | None,
    acceptance: MediaSelectionAcceptanceComposition | None = None,
):
    transport = getattr(media_bundle, "transport", None)
    deployment = getattr(media_bundle, "deployment", None)
    return build_sqlite_world_v2_turn_application(
        path=database_path,
        config=_config(media_bundle=media_bundle, acceptance=acceptance),
        identities=_Identities(),
        router=_Router(),
        character_interior=_new_character_interior(role_model),
        transport=_NoDeliveryTransport(),
        media_transport=transport,
        media_planner=(getattr(deployment, "planner", None) if deployment is not None else None),
        now=now,
    )


def _build_media_bundle(*, settings: Settings, phase_root: Path, artifacts_root: Path):
    """Compose the real provider bundle with an absolute scratch artifact root."""

    output_dir = (artifacts_root / "event-media").resolve()
    with _cwd(phase_root):
        return build_qq_media_preview_deployment(
            settings=settings,
            world_id=WORLD_ID,
            output_dir=output_dir,
        )


async def _close_app(app: object | None) -> None:
    if app is None:
        return
    errors: list[Exception] = []
    close_async = getattr(app, "aclose", None)
    if callable(close_async):
        try:
            await close_async()
            return
        except Exception as exc:
            errors.append(exc)
    close_sync = getattr(app, "close", None)
    if callable(close_sync):
        try:
            close_sync()
            return
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise RuntimeError(
            "application close failed: "
            + "; ".join(f"{type(error).__name__}: {error}" for error in errors)
        ) from errors[-1]


async def _close_bundle(bundle: object | None) -> None:
    if bundle is None:
        return
    deployment = getattr(bundle, "deployment", None)
    planner = getattr(deployment, "planner", None)
    close_planner_async = getattr(planner, "aclose", None)
    if callable(close_planner_async):
        await close_planner_async()
    else:
        close_planner = getattr(planner, "close", None)
        if callable(close_planner):
            close_planner()
    transport = getattr(bundle, "transport", None)
    close_transport = getattr(transport, "close", None)
    if callable(close_transport):
        close_transport()


def _safe_endpoint(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value.split("?", 1)[0].split("#", 1)[0]
    return urlunsplit((parsed.scheme, parsed.hostname or parsed.netloc, parsed.path, "", ""))


def _git_state() -> dict[str, object]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    return {
        "sha": run("rev-parse", "HEAD"),
        "dirty": bool(run("status", "--porcelain", "--untracked-files=all")),
        "branch": run("branch", "--show-current"),
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _latency(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def _event_types(evidence: object) -> list[str]:
    return [item.event.event_type for item in getattr(evidence, "events", ())]


def _selection_model_result(evidence: object) -> dict[str, object] | None:
    for item in reversed(getattr(evidence, "events", ())):
        event = item.event
        if event.event_type not in {
            "MediaSelectionProposalRecorded",
            "MediaSelectionAttemptRecorded",
        }:
            continue
        payload = event.payload()
        raw_audit = payload.get("character_interior_model_result")
        if not isinstance(raw_audit, dict):
            return None
        audit_json = raw_audit.get("audit_json")
        audit: dict[str, object] = {}
        if isinstance(audit_json, str):
            try:
                decoded = json.loads(audit_json)
                if isinstance(decoded, dict):
                    audit = decoded
            except (TypeError, ValueError):
                audit = {}
        route = audit.get("route")
        lineage = audit.get("character_interior_lineage")
        return {
            "event_ref": event.event_id,
            "model_result_ref": raw_audit.get("model_result_ref"),
            "audit_contract": raw_audit.get("audit_contract"),
            "deliberation_result_id": raw_audit.get("deliberation_result_id"),
            "proposal_hash": raw_audit.get("proposal_hash"),
            "model_call_id": raw_audit.get("model_call_id"),
            "attempt_id": raw_audit.get("attempt_id"),
            "capsule_id": raw_audit.get("capsule_id"),
            "audit_hash": raw_audit.get("audit_hash"),
            "model_id": audit.get("model_id"),
            "request_hash": audit.get("request_hash"),
            "response_hash": audit.get("response_hash"),
            "attempt_index": audit.get("attempt_index"),
            "attempt_count": audit.get("attempt_count"),
            "status": audit.get("status"),
            "outcome": audit.get("outcome"),
            "failure_code": audit.get("failure_code"),
            "usage_status": audit.get("usage_status"),
            "route": route if isinstance(route, dict) else None,
            "lineage": lineage if isinstance(lineage, dict) else None,
        }
    return None


def _proposal_binding(evidence: object, proposal_event_ref: str | None) -> dict[str, object] | None:
    if not proposal_event_ref:
        return None
    for item in getattr(evidence, "events", ()):
        event = item.event
        if event.event_id != proposal_event_ref:
            continue
        payload = event.payload()
        return {
            "proposal_id": payload.get("proposal_id"),
            "candidate_id": payload.get("candidate_id"),
            "evaluated_world_revision": payload.get("evaluated_world_revision"),
            "evaluated_deliberation_revision": payload.get("evaluated_deliberation_revision"),
            "evaluated_ledger_sequence": payload.get("evaluated_ledger_sequence"),
            "candidate_authority_hash": payload.get("candidate_authority_hash"),
            "selection_hash": payload.get("selection_hash"),
            "change_id": payload.get("change_id"),
            "policy_digest": payload.get("policy_digest"),
        }
    return None


def _media_selection_report(
    *,
    result: object,
    role_model: _ObservedRoleModel,
    evidence: object,
    usage: Sequence[ModelCallUsage],
    usage_start: int,
) -> dict[str, object]:
    latest_attempt = role_model.media_attempts[-1] if role_model.media_attempts else {}
    selected_event = _selection_model_result(evidence)
    usage_delta = [asdict(item) for item in usage[usage_start:]]
    return {
        "status": getattr(result, "status", None),
        "reason_code": getattr(result, "reason_code", None),
        "proposal_event_ref": getattr(result, "proposal_event_ref", None),
        "provider": role_model.provider,
        "model": role_model.model,
        "base_url": None,
        "decision": latest_attempt.get("decision"),
        "selected_token": latest_attempt.get("selected_token"),
        "tool_contract": latest_attempt.get("contract_identity"),
        "capability_ref": latest_attempt.get("capability_ref"),
        "capability_payload_hash": latest_attempt.get("capability_payload_hash"),
        "source_refs": latest_attempt.get("source_refs", []),
        "response_hash": latest_attempt.get("response_hash"),
        # A recall is a second legal attempt by the same CharacterInterior
        # faculty.  Keep the attempt metadata, but never retain the prompt or
        # candidate descriptions in this report.
        "attempts": [dict(item) for item in role_model.media_attempts],
        "first_legal": (
            len(role_model.media_attempts) == 1
            and getattr(result, "status", None) in {"proposed", "no_op"}
        ),
        "model_result": selected_event,
        "usage": usage_delta,
        "cost": {
            "actual": None,
            "currency": None,
            "state": "unknown",
            "note": "DeepSeek price is not returned by the model adapter.",
        },
    }


async def _setup_source_bound_evidence(app: object, *, logical_time: datetime) -> dict[str, object]:
    inbound = await app.inbound(
        platform="acceptance",
        platform_user_id="acceptance",
        platform_message_id="message:acceptance",
        text="傍晚我想去公园走走，看看晚霞。",
        observed_at=logical_time,
        trace_id="trace:acceptance:inbound",
    )
    plan = await app.plan_activity(
        ActivityPlanCommand(
            command_id="command:acceptance:plan",
            world_id=WORLD_ID,
            source_observation_id=INBOUND_OBSERVATION_ID,
            plan_id="plan:acceptance-walk",
            activity_id="activity:acceptance-walk",
            activity_kind="walk",
            importance_bp=4_000,
            location_ref="location:park",
            participant_refs=("agent:companion",),
            privacy_class="shareable",
        ),
        logical_time=logical_time,
        created_at=logical_time,
        trace_id="trace:acceptance:plan",
        causation_id="cause:acceptance:plan",
        correlation_id="correlation:acceptance",
    )
    started = await app.transition_activity(
        ActivityPlanTransitionCommand(
            command_id="command:acceptance:start",
            world_id=WORLD_ID,
            source_observation_id=INBOUND_OBSERVATION_ID,
            plan_id="plan:acceptance-walk",
            operation="start",
        ),
        logical_time=logical_time,
        created_at=logical_time,
        trace_id="trace:acceptance:start",
        causation_id=plan.event_ids[-1],
        correlation_id="correlation:acceptance",
    )
    declaration = await app.declare_image_evidence(
        ImageEvidenceDeclarationCommand(
            command_id="command:acceptance:evidence",
            source_event_ref=started.event_ids[-1],
            image_evidence=ImageEvidenceV1(
                visibility="shareable",
                summary="傍晚的公园散步，晚霞正好",
                activity={
                    "evidence_visibility": "shareable",
                    "id": "activity:acceptance-walk",
                    "kind": "walk",
                    "description": "傍晚在公园的小径散步",
                    "phase": "active",
                },
                location={
                    "evidence_visibility": "shareable",
                    "id": "location:park",
                    "kind": "park",
                    "publicness": "public",
                },
                environment={
                    "evidence_visibility": "shareable",
                    "light": "golden_hour",
                },
            ),
        ),
        logical_time=logical_time,
        created_at=logical_time,
        trace_id="trace:acceptance:evidence",
        correlation_id="correlation:acceptance",
    )
    ecology = await app.drain_media_ecology_once(
        wake_event_ref=declaration.event_ids[-1],
        logical_time=logical_time,
        trace_id="trace:acceptance:ecology",
        correlation_id="correlation:acceptance",
    )
    return {
        "inbound_status": getattr(inbound, "status", None),
        "inbound_terminal_errors": list(getattr(inbound, "terminal_errors", ())),
        "plan_event_ref": plan.event_ids[-1],
        "activity_start_event_ref": started.event_ids[-1],
        "image_evidence_event_ref": declaration.event_ids[-1],
        "ecology_status": getattr(ecology, "status", None),
        "candidate_ids": list(getattr(ecology, "candidate_ids", ())),
    }


def _dispatch_rows(database_path: Path) -> list[dict[str, object]]:
    if not database_path.exists():
        return []
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT d.idempotency_key, d.request_fingerprint, d.receipt_json, "
            "d.result_type, d.result_json, p.diagnostic_json "
            "FROM world_v2_media_provider_dispatch AS d "
            "LEFT JOIN world_v2_media_provider_diagnostic AS p "
            "ON p.world_id = d.world_id AND p.idempotency_key = d.idempotency_key "
            "ORDER BY d.idempotency_key"
        ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot read isolated media dispatch evidence: {type(exc).__name__}") from exc
    finally:
        connection.close()
    result: list[dict[str, object]] = []
    for key, fingerprint, receipt_json, result_type, result_json, diagnostic_json in rows:
        receipt: dict[str, object] = {}
        try:
            decoded = json.loads(str(receipt_json))
            if isinstance(decoded, dict):
                receipt = decoded
        except (TypeError, ValueError):
            pass
        diagnostic: dict[str, object] | None = None
        if diagnostic_json is not None:
            try:
                decoded_diagnostic = json.loads(str(diagnostic_json))
                if isinstance(decoded_diagnostic, dict):
                    diagnostic = decoded_diagnostic
            except (TypeError, ValueError):
                diagnostic = None
        result.append(
            {
                "idempotency_key": str(key),
                "request_fingerprint": str(fingerprint),
                "receipt_id": receipt.get("provider_receipt_id"),
                "status": receipt.get("status"),
                "error_class": receipt.get("error_class"),
                "diagnostic": diagnostic,
                "result_type": result_type,
                "result_json_hash": (
                    "sha256:" + hashlib.sha256(str(result_json).encode("utf-8")).hexdigest()
                    if result_json is not None
                    else None
                ),
                "_result_json": str(result_json) if result_json is not None else None,
            }
        )
    return result


def _planning_rows(database_path: Path) -> list[dict[str, str | None]]:
    if not database_path.exists():
        return []
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT planning_request_id, result_hash, result_json "
            "FROM world_v2_event_media_planning_result ORDER BY planning_request_id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(
            "cannot read isolated media planning evidence: " + type(exc).__name__
        ) from exc
    finally:
        connection.close()
    result: list[dict[str, str | None]] = []
    for request_id, result_hash, result_json in rows:
        status: str | None = None
        reason_code: str | None = None
        try:
            decoded = json.loads(str(result_json))
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            if isinstance(decoded.get("plan"), dict):
                status = "planned"
            terminal = decoded.get("not_renderable")
            if isinstance(terminal, dict):
                status = "not_renderable"
                reason = terminal.get("reason_code")
                reason_code = str(reason) if reason is not None else None
        result.append(
            {
                "planning_request_id": str(request_id),
                "result_hash": str(result_hash),
                "status": status,
                "reason_code": reason_code,
            }
        )
    return result


def _artifact_sidecar_info(rows: Sequence[Mapping[str, object]]) -> dict[str, object] | None:
    for row in rows:
        if row.get("result_type") != "MediaProviderArtifactResult":
            continue
        raw = row.get("_result_json")
        if not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        return {
            "idempotency_key": row.get("idempotency_key"),
            "artifact_payload_ref": payload.get("artifact_payload_ref"),
            "artifact_payload_hash": payload.get("artifact_payload_hash"),
            "artifact_content_type": payload.get("artifact_content_type"),
        }
    return None


def _public_dispatch_rows(rows: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {key: value for key, value in row.items() if not key.startswith("_")}
        for row in rows
    ]


def _usage_cost_report(
    *,
    phase_report: Mapping[str, object],
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    selection = phase_report.get("character_selection")
    planning = phase_report.get("planning")
    selection_usage = selection.get("usage", []) if isinstance(selection, dict) else []
    planning_usage = planning.get("usage") if isinstance(planning, dict) else None
    return {
        "character_selection": selection_usage,
        "planning": planning_usage,
        "provider_receipts": [
            {
                "idempotency_key": row.get("idempotency_key"),
                "ledger_amount": 0,
                "provider_cost_actual": None,
                "currency": None,
                "state": "unknown",
                "note": "durable media transport records no provider price",
            }
            for row in rows
        ],
    }


def _image_dimensions(data: bytes) -> dict[str, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return {"width": width, "height": height}
    return None


def _inspection_state(evidence: object) -> list[dict[str, object]]:
    return [
        item.model_dump(mode="json")
        for item in getattr(getattr(evidence, "projection", None), "media_inspections", ())
    ]


def _preview_artifact(
    *,
    app: object,
    preview_dir: Path,
    rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    queue = app.media_preview_operator(preview_dir=preview_dir).queue()
    if not queue:
        return {"queue": [], "artifact": None, "preview": None}
    row = queue[0]
    image_path_value = row.get("image_path") if isinstance(row, dict) else None
    image_path = Path(str(image_path_value)) if image_path_value else None
    data = image_path.read_bytes() if image_path is not None and image_path.exists() else b""
    evidence = app.export_replay_evidence()
    projection = evidence.projection
    artifact = projection.media_artifacts[0] if projection.media_artifacts else None
    inspection = projection.media_inspections[0] if projection.media_inspections else None
    preview = projection.media_previews[0] if projection.media_previews else None
    actual_hash = "sha256:" + hashlib.sha256(data).hexdigest() if data else None
    return {
        "queue": [
            {
                key: (str(value) if isinstance(value, Path) else value)
                for key, value in row.items()
            }
        ],
        "preview": {
            "preview_id": getattr(preview, "preview_id", row.get("preview_id")),
            "plan_id": getattr(preview, "plan_id", row.get("plan_id")),
            "artifact_id": getattr(preview, "artifact_id", row.get("artifact_id")),
            "inspection_id": getattr(preview, "inspection_id", row.get("inspection_id")),
            "delivered": bool(row.get("delivered", False)),
        },
        "artifact": {
            "path": str(image_path) if image_path is not None else None,
            "bytes": len(data),
            "sha256": actual_hash,
            "projection_artifact_hash": getattr(artifact, "artifact_hash", None),
            "mime_type": (
                mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"
                if image_path is not None
                else None
            ),
            "sidecar_content_type": getattr(artifact, "media_type", None),
            "dimensions": _image_dimensions(data),
            "artifact_id": getattr(artifact, "artifact_id", None),
            "plan_id": getattr(artifact, "plan_id", None),
            "inspection_id": getattr(inspection, "inspection_id", None),
            "inspection_hash": getattr(inspection, "inspection_payload_hash", None),
            "sidecar": _artifact_sidecar_info(rows),
        },
    }


def _media_ids(evidence: object) -> dict[str, object]:
    projection = evidence.projection
    media_actions = [item for item in projection.actions if item.kind in MEDIA_ACTION_KINDS | {"media_planning"}]
    receipt_by_action = {
        item.action_id: item.receipt_id
        for item in projection.execution_receipts
        if item.action_id in {action.action_id for action in media_actions}
    }
    return {
        "candidate_ids": [item.candidate_id for item in projection.photo_candidates],
        "opportunity_ids": [item.opportunity_id for item in projection.media_opportunities],
        "plan_ids": [item.plan_id for item in projection.media_plans],
        "artifact_ids": [item.artifact_id for item in projection.media_artifacts],
        "inspection_ids": [item.inspection_id for item in projection.media_inspections],
        "preview_ids": [item.preview_id for item in projection.media_previews],
        "action_ids": [item.action_id for item in media_actions],
        "receipt_ids": list(receipt_by_action.values()),
    }


async def _probe_grant_rejection(
    *,
    database_path: Path,
    now: datetime,
    role_model: _ObservedRoleModel,
    bundle: object,
    proposal_event_ref: str,
    grant: ProviderMediaGrantBinding,
    label: str,
) -> dict[str, object]:
    base_acceptance = bundle.deployment.acceptance
    acceptance = replace(base_acceptance, grant=grant)
    app = _build_app(
        database_path=database_path,
        now=now,
        role_model=role_model,
        media_bundle=bundle,
        acceptance=acceptance,
    )
    try:
        logical_time = await app.current_logical_time()
        try:
            result = await app.accept_media_selection_once(
                proposal_event_ref=proposal_event_ref,
                logical_time=logical_time,
                trace_id=f"trace:acceptance:probe:{label}",
                correlation_id="correlation:acceptance",
            )
        except Exception as exc:
            return {
                "label": label,
                "fail_closed": True,
                "exception": type(exc).__name__,
                "reason": str(exc)[:300],
            }
        return {
            "label": label,
            "fail_closed": False,
            "unexpected_result": str(result),
        }
    finally:
        await _close_app(app)


def _expired_approval_probe(now: datetime) -> dict[str, object]:
    artifact_hash = "sha256:" + "a" * 64
    approval = MediaAutomaticDeliveryApproval(
        approval_id="approval:qualification:expired",
        entity_revision=1,
        plan_id="plan:qualification",
        inspection_id="inspection:qualification",
        artifact_id="artifact:qualification",
        artifact_hash=artifact_hash,
        sample_hash=artifact_hash,
        recipient_ref=RECIPIENT_REF,
        operator_ref="operator:qualification-probe",
        family="life_share",
        approved_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    action = Action(
        schema_version="world-v2.1",
        action_id="action:qualification:expired-approval",
        world_id=WORLD_ID,
        logical_time=now,
        created_at=now,
        trace_id="trace:qualification:expired-approval",
        causation_id="cause:qualification",
        correlation_id="correlation:qualification",
        kind="media_delivery",
        layer="external_action",
        intent_ref=approval.inspection_id,
        actor="agent:companion",
        target=RECIPIENT_REF,
        payload_ref="sidecar:artifact:qualification",
        payload_hash=artifact_hash,
        media_delivery_approval=MediaDeliveryApprovalBinding(
            approval_id=approval.approval_id,
            approval_revision=approval.entity_revision,
        ),
        idempotency_key="media-delivery:qualification:expired",
        budget_reservation_id="reservation:qualification:expired",
        state="authorized",
        recovery_policy="effect_once",
    )
    projection = LedgerProjection(
        world_id=WORLD_ID,
        world_revision=0,
        deliberation_revision=0,
        ledger_sequence=0,
        semantic_hash="0" * 64,
        media_delivery_approvals=(approval,),
        actions=(action,),
        pending_actions=(action,),
    )
    try:
        require_current_media_delivery_approval(
            action=action,
            projection=projection,
            logical_time=now,
        )
    except Exception as exc:
        return {
            "fail_closed": True,
            "exception": type(exc).__name__,
            "reason": str(exc)[:300],
        }
    return {"fail_closed": False, "reason": "expired approval was unexpectedly accepted"}


async def _drain_provider_stages(
    *,
    app: object,
    logical_time: datetime,
    phase_report: dict[str, object],
) -> None:
    render_latency = 0.0
    inspection_latency = 0.0
    render_attempts = 0
    action_evidence: list[dict[str, object]] = []
    for round_index in range(16):
        await app.drain_media_continuation_once(
            logical_time=logical_time,
            trace_id=f"trace:acceptance:continuation:{round_index}",
            correlation_id="correlation:acceptance",
        )
        evidence = app.export_replay_evidence()
        pending = tuple(
            sorted(
                (
                    action
                    for action in evidence.projection.pending_actions
                    if action.kind in MEDIA_ACTION_KINDS
                ),
                key=lambda item: item.action_id,
            )
        )
        if pending:
            for action in pending[:1]:
                if action.kind in {"media_render", "media_repair"}:
                    if not _render_attempt_within_limit(render_attempts):
                        phase_report["render_attempt_limit_exceeded"] = True
                        phase_report["real_render_attempts"] = render_attempts
                        raise RuntimeError(
                            "isolated qualification exceeded the maximum real render attempts"
                        )
                    render_attempts += 1
                started = time.perf_counter()
                result = await app.drain_action(action.action_id)
                elapsed = _latency(started)
                if action.kind in {"media_render", "media_repair"}:
                    render_latency += elapsed
                elif action.kind == "media_inspection":
                    inspection_latency += elapsed
                action_evidence.append(
                    {
                        "action_id": action.action_id,
                        "kind": action.kind,
                        "status": getattr(result, "status", None),
                        "latency_ms": elapsed,
                    }
                )
        await app.drain_media_results_once(logical_time=logical_time)
        evidence = app.export_replay_evidence()
        if evidence.projection.media_previews:
            break
    phase_report["latency_ms"] = {
        **dict(phase_report.get("latency_ms", {})),
        "render": round(render_latency, 1),
        "inspection": round(inspection_latency, 1),
    }
    phase_report["latency_semantics"] = {
        "render": "media_render action includes image generation and the visual inspection provider call",
        "inspection": "media_inspection action replays the durable inspection sidecar locally",
        "inspection_provider_call": "included in render latency by the current provider transport seam",
    }
    phase_report["provider_actions"] = action_evidence
    phase_report["real_render_attempts"] = render_attempts
    phase_report["max_real_render_attempts"] = MAX_REAL_RENDER_ATTEMPTS


async def _run_phase(
    *,
    phase_root: Path,
    settings_template: Settings,
    role_model: _ObservedRoleModel,
    now: datetime,
    deterministic_selection_double: bool,
) -> dict[str, object]:
    phase_root.mkdir(parents=True, exist_ok=True)
    artifacts_root = phase_root / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    database_path = phase_root / "media.sqlite"
    settings = settings_template.model_copy(
        update={
            "database_path": database_path,
            "visual_identity_path": (REPO_ROOT / "configs" / "visual_identity.yaml").resolve(),
        }
    )
    report: dict[str, object] = {
        "phase": "downstream-provider-stages" if deterministic_selection_double else "real-character-selection",
        "scratch_db": str(database_path),
        "artifacts_root": str(artifacts_root),
        "deterministic_selection_double": deterministic_selection_double,
        "character_selection_qualified": False,
        "status": "started",
        "latency_ms": {},
        "exclusions": {
            "real_qq_dispatch": False,
            "production_db": False,
            "shared_output": False,
            "ports": "none",
            "local_comfyui": False,
            "auto_delivery": False,
        },
    }
    app: object | None = None
    bundle: object | None = None
    try:
        # Clear only this new scratch target.  It is never a repository or a
        # configured production path.
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(database_path) + suffix)
            if candidate.exists():
                candidate.unlink()

        app = _build_app(
            database_path=database_path,
            now=now,
            role_model=role_model,
            media_bundle=None,
        )
        try:
            await app.tick(
                tick_id=f"acceptance:{report['phase']}:clock",
                logical_time_from=now,
                logical_time_to=now + timedelta(minutes=1),
                observed_at=now + timedelta(minutes=1),
                trace_id=f"trace:acceptance:{report['phase']}:clock",
                causation_id="scheduler:acceptance",
                correlation_id="correlation:acceptance",
                reason="media_preview_acceptance",
            )
        finally:
            await _close_app(app)
            app = None

        ledger = SQLiteWorldLedger(path=database_path, world_id=WORLD_ID)
        try:
            signing_key = os.environ.get("WORLD_V2_ROOT_SIGNING_KEY_HEX", "11" * 32)
            MediaAuthorityProvisioner(
                ledger=ledger,
                signing_key_hex=signing_key,
                subject_ref="user:acceptance",
            ).ensure()
        finally:
            ledger.close()

        # The factory is the real planner/render/inspection composition.  Its
        # relative output path is intentionally resolved while cwd is this
        # phase's scratch root, never the repository or shared output folder.
        bundle = _build_media_bundle(
            settings=settings,
            phase_root=phase_root,
            artifacts_root=artifacts_root,
        )
        if bundle is None:
            report.update(
                {
                    "status": "provider_prerequisite_unavailable",
                    "failure": "real media deployment factory returned disabled",
                }
            )
            return report

        app = _build_app(
            database_path=database_path,
            now=now,
            role_model=role_model,
            media_bundle=bundle,
        )
        logical_time = await app.current_logical_time()
        if logical_time is None:
            raise RuntimeError("scratch world has no logical clock after bootstrap")
        source_report = await _setup_source_bound_evidence(app, logical_time=logical_time)
        report["source_setup"] = source_report
        if source_report.get("ecology_status") not in {"created", "available"}:
            report.update(
                {
                    "status": "source_candidate_unavailable",
                    "failure": "source-bound ecology did not create a candidate",
                }
            )
            return report

        usage_start = len(role_model.usage)
        selection_started = time.perf_counter()
        selection = await app.drain_media_selection_once(
            logical_time=logical_time,
            trace_id="trace:acceptance:selection",
            correlation_id="correlation:acceptance",
        )
        selection_latency = _latency(selection_started)
        evidence_after_selection = app.export_replay_evidence()
        selection_report = _media_selection_report(
            result=selection,
            role_model=role_model,
            evidence=evidence_after_selection,
            usage=role_model.usage,
            usage_start=usage_start,
        )
        selection_report["base_url"] = _safe_endpoint(settings.deepseek_base_url)
        report["character_selection"] = selection_report
        report["source_refs"] = list(
            dict.fromkeys(
                ref
                for candidate in evidence_after_selection.projection.photo_candidates
                for ref in candidate.source_event_refs
            )
        )
        report["contract_digests"] = selection_report.get("tool_contract")
        report["proposal_binding"] = _proposal_binding(
            evidence_after_selection, selection.proposal_event_ref if selection else None
        )
        report["latency_ms"] = {"selection": selection_latency}

        if selection is None:
            report.update({"status": "selection_unavailable", "failure": "selection worker absent"})
            return report
        if selection.status == "no_op":
            # no_op is a legal character result.  Do not retry it in this
            # scratch world merely to manufacture a rendered artifact.
            report.update(
                {
                    "status": "legal_character_no_op",
                    "qualification_complete": False,
                    "no_op_is_failure": False,
                }
            )
            return report
        if selection.status == "blocked":
            report.update(
                {
                    "status": "character_selection_technical_failure",
                    "failure": selection.reason_code,
                    "qualification_complete": False,
                    "no_op_is_failure": False,
                }
            )
            return report
        if not selection.proposal_event_ref:
            raise RuntimeError("proposed selection has no proposal event ref")

        # Probe invalid grant revisions through the current Acceptance seam.
        # These probes occur before the valid acceptance, so they cannot leave
        # an accepted opportunity behind.
        await _close_app(app)
        app = None
        stale_grant = ProviderMediaGrantBinding(
            grant_id=bundle.deployment.acceptance.grant.grant_id,
            grant_revision=bundle.deployment.acceptance.grant.grant_revision + 1,
        )
        unknown_grant = ProviderMediaGrantBinding(
            grant_id="provider-media-grant:unknown",
            grant_revision=1,
        )
        report["fail_closed"] = {
            "stale_grant": await _probe_grant_rejection(
                database_path=database_path,
                now=now,
                role_model=role_model,
                bundle=bundle,
                proposal_event_ref=selection.proposal_event_ref,
                grant=stale_grant,
                label="stale_grant",
            ),
            "unknown_grant": await _probe_grant_rejection(
                database_path=database_path,
                now=now,
                role_model=role_model,
                bundle=bundle,
                proposal_event_ref=selection.proposal_event_ref,
                grant=unknown_grant,
                label="unknown_grant",
            ),
        }

        app = _build_app(
            database_path=database_path,
            now=now,
            role_model=role_model,
            media_bundle=bundle,
        )
        logical_time = await app.current_logical_time()
        acceptance_started = time.perf_counter()
        accepted = await app.accept_media_selection_once(
            proposal_event_ref=selection.proposal_event_ref,
            logical_time=logical_time,
            trace_id="trace:acceptance:accept",
            correlation_id="correlation:acceptance",
        )
        acceptance_latency = _latency(acceptance_started)
        if accepted is None:
            raise RuntimeError("valid media selection acceptance was not composed")
        report["acceptance"] = {
            "event_ids": list(accepted.event_ids),
            "grant_id": bundle.deployment.acceptance.grant.grant_id,
            "grant_revision": bundle.deployment.acceptance.grant.grant_revision,
            "account_id": bundle.deployment.acceptance.account_id,
            "amount_limit": bundle.deployment.acceptance.amount_limit,
        }
        planning_started = time.perf_counter()
        planning = await app.drain_media_planning_once()
        planning_latency = _latency(planning_started)
        report["planning"] = {
            "status": planning.status,
            "action_id": planning.action_id,
            "provider": "deepseek",
            "model": settings.world_v2_media_planner_model or settings.deepseek_model,
            "base_url": _safe_endpoint(settings.deepseek_base_url),
            "usage": {"available": False, "note": "QQ deployment factory does not expose planner token usage."},
            "cost": {"actual": None, "currency": None, "state": "unknown"},
        }
        report["latency_ms"] = {
            "selection": selection_latency,
            "acceptance": acceptance_latency,
            "planning": planning_latency,
        }
        report["usage_cost"] = _usage_cost_report(phase_report=report, rows=())
        planning_rows = _planning_rows(database_path)
        planning_provider_calls = getattr(bundle.deployment.planner, "provider_call_count", None)
        report["planning"]["durable_result_rows"] = planning_rows
        report["planning"]["provider_call_count"] = planning_provider_calls
        if planning.status != "planned":
            report.update(
                {
                    "status": "planning_not_renderable",
                    "failure": (
                        planning_rows[-1].get("reason_code")
                        if planning_rows and planning_rows[-1].get("reason_code")
                        else planning.status
                    ),
                    "qualification_complete": False,
                    "provider_failure_semantics": {
                        "observed": True,
                        "failure_kind": "planning_terminal_result",
                        "failure_did_not_become_role_no_op": (
                            _provider_failure_did_not_become_role_no_op(
                                report["character_selection"], [planning.status]
                            )
                        ),
                        "failure": (
                            planning_rows[-1].get("reason_code")
                            if planning_rows
                            else planning.status
                        ),
                    },
                }
            )
            return report

        planning_rows_before_restart = planning_rows
        planning_provider_calls_before_restart = planning_provider_calls

        await _drain_provider_stages(
            app=app,
            logical_time=logical_time,
            phase_report=report,
        )
        live_evidence = app.export_replay_evidence()
        if not live_evidence.projection.media_previews:
            failure_events = [
                item
                for item in _event_types(live_evidence)
                if item in {"MediaPreviewFailed", "MediaNotRenderableRecorded"}
            ]
            rows = _dispatch_rows(database_path)
            failure_details = []
            for item in live_evidence.events:
                if item.event.event_type not in {"MediaPreviewFailed", "MediaNotRenderableRecorded"}:
                    continue
                payload = item.event.payload()
                failure_details.append(
                    {
                        "event_ref": item.event.event_id,
                        "event_type": item.event.event_type,
                        "reason_code": payload.get("reason_code")
                        or (payload.get("result") or {}).get("reason_code")
                        if isinstance(payload.get("result"), dict)
                        else payload.get("reason_code"),
                    }
                )
            before_semantic_hash = live_evidence.projection.semantic_hash
            before_inspection_hash = (
                live_evidence.projection.media_inspections[0].inspection_payload_hash
                if live_evidence.projection.media_inspections
                else None
            )
            # A failed real provider call is still a restart/effect-once
            # qualification case.  Rebuild the application and verify that
            # the durable failed receipt is reused rather than re-sent.
            await _close_app(app)
            app = None
            await _close_bundle(bundle)
            bundle = None
            bundle = _build_media_bundle(
                settings=settings,
                phase_root=phase_root,
                artifacts_root=artifacts_root,
            )
            if bundle is None:
                raise RuntimeError("media deployment disappeared during failure restart composition")
            app = _build_app(
                database_path=database_path,
                now=now,
                role_model=role_model,
                media_bundle=bundle,
            )
            cold_before = app.export_replay_evidence()
            await app.drain_media_continuation_once(
                logical_time=cold_before.projection.logical_time or logical_time,
                trace_id="trace:acceptance:failed-cold-repeat:continuation",
                correlation_id="correlation:acceptance",
            )
            await app.drain_media_planning_once()
            await app.drain_media_results_once(
                logical_time=cold_before.projection.logical_time or logical_time
            )
            cold_after = app.export_replay_evidence()
            after_rows = _dispatch_rows(database_path)
            after_planning_rows = _planning_rows(database_path)
            cold_planning_provider_calls = getattr(
                bundle.deployment.planner, "provider_call_count", None
            )
            fingerprint_probe = {"fail_closed": False}
            if rows:
                first = rows[0]
                try:
                    await bundle.transport.lookup(
                        idempotency_key=str(first["idempotency_key"]),
                        request_fingerprint=str(first["request_fingerprint"]) + "-different",
                    )
                except Exception as exc:
                    fingerprint_probe = {
                        "fail_closed": True,
                        "exception": type(exc).__name__,
                        "reason": str(exc)[:300],
                    }
            report.update(
                {
                    "status": "preview_not_generated",
                    "failure_events": failure_events,
                    "failure_details": failure_details,
                    "provider_dispatch": _public_dispatch_rows(rows),
                    "usage_cost": _usage_cost_report(phase_report=report, rows=rows),
                    "provider_failure_semantics": {
                        "observed": bool(failure_events),
                        "failure_did_not_become_role_no_op": (
                            _provider_failure_did_not_become_role_no_op(
                                report["character_selection"], failure_events
                            )
                        ),
                        "failure_events": failure_events,
                    },
                    "ids": _media_ids(live_evidence),
                    "replay_live": {
                        "cursor": live_evidence.cursor.model_dump(mode="json"),
                        "reducer_bundle_version": live_evidence.reducer_bundle_version,
                        "projection_semantic_hash": before_semantic_hash,
                        "replay_semantic_hash": live_evidence.replay.semantic_hash,
                        "projection_matches_replay": live_evidence.projection
                        == live_evidence.replay,
                        "evaluator_passed": ReplayEvaluator().evaluate(evidence=live_evidence).passed,
                    },
                    "fail_closed": {
                        **dict(report.get("fail_closed", {})),
                        "different_fingerprint": fingerprint_probe,
                        "expired_approval": _expired_approval_probe(now),
                    },
                    "restart": {
                        "provider_dispatch_rows_unchanged": rows == after_rows,
                        "provider_receipt_ids_unchanged": [row.get("receipt_id") for row in rows]
                        == [row.get("receipt_id") for row in after_rows],
                        "same_idempotency_not_resent": len(rows) == len(after_rows),
                        "planning_result_rows_unchanged": planning_rows_before_restart
                        == after_planning_rows,
                        "planning_provider_calls_before_restart": planning_provider_calls_before_restart,
                        "planning_provider_calls_after_restart": cold_planning_provider_calls,
                        "planning_same_idempotency_not_resent": (
                            cold_planning_provider_calls == 0
                            if cold_planning_provider_calls is not None
                            else "not_observed"
                        ),
                        "artifact_bytes_unchanged": "not_applicable_no_artifact",
                        "artifact_hash_unchanged": "not_applicable_no_artifact",
                        "inspection_result_unchanged": _inspection_state(live_evidence)
                        == _inspection_state(cold_after),
                        "inspection_hash_unchanged": before_inspection_hash
                        == (
                            cold_after.projection.media_inspections[0].inspection_payload_hash
                            if cold_after.projection.media_inspections
                            else None
                        ),
                        "projection_semantic_hash_unchanged": before_semantic_hash
                        == cold_after.projection.semantic_hash,
                        "live_projection_matches_live_replay": live_evidence.projection
                        == live_evidence.replay,
                        "cold_projection_matches_cold_replay": cold_after.projection
                        == cold_after.replay,
                        "cold_projection_semantic_hash_matches_replay": cold_after.projection.semantic_hash
                        == cold_after.replay.semantic_hash,
                        "cold_replay_semantic_hash": cold_after.replay.semantic_hash,
                        "repeat_drain_preview_count": len(cold_after.projection.media_previews),
                    },
                    "qualification_complete": False,
                }
            )
            return report

        preview_before = _preview_artifact(
            app=app,
            preview_dir=artifacts_root / "previews",
            rows=_dispatch_rows(database_path),
        )
        before_rows = _dispatch_rows(database_path)
        before_semantic_hash = live_evidence.projection.semantic_hash
        before_inspection_hash = (
            live_evidence.projection.media_inspections[0].inspection_payload_hash
            if live_evidence.projection.media_inspections
            else None
        )
        report["ids"] = _media_ids(live_evidence)
        report["artifact"] = preview_before.get("artifact")
        report["preview_queue"] = preview_before.get("queue")
        report["replay_live"] = {
            "cursor": live_evidence.cursor.model_dump(mode="json"),
            "reducer_bundle_version": live_evidence.reducer_bundle_version,
            "projection_semantic_hash": before_semantic_hash,
            "replay_semantic_hash": live_evidence.replay.semantic_hash,
            "projection_matches_replay": live_evidence.projection == live_evidence.replay,
            "evaluator_passed": ReplayEvaluator().evaluate(evidence=live_evidence).passed,
        }
        report["provider_dispatch"] = _public_dispatch_rows(before_rows)
        report["usage_cost"] = _usage_cost_report(phase_report=report, rows=before_rows)

        # Close both application and provider sidecar ownership before
        # composing a fresh deployment bundle against the same SQLite file.
        await _close_app(app)
        app = None
        await _close_bundle(bundle)
        bundle = None
        bundle = _build_media_bundle(
            settings=settings,
            phase_root=phase_root,
            artifacts_root=artifacts_root,
        )
        if bundle is None:
            raise RuntimeError("media deployment disappeared during restart composition")
        app = _build_app(
            database_path=database_path,
            now=now,
            role_model=role_model,
            media_bundle=bundle,
        )
        cold_before = app.export_replay_evidence()
        await app.drain_media_continuation_once(
            logical_time=cold_before.projection.logical_time or logical_time,
            trace_id="trace:acceptance:cold-repeat:continuation",
            correlation_id="correlation:acceptance",
        )
        await app.drain_media_planning_once()
        await app.drain_media_results_once(
            logical_time=cold_before.projection.logical_time or logical_time
        )
        cold_after = app.export_replay_evidence()
        after_preview = _preview_artifact(
            app=app,
            preview_dir=artifacts_root / "previews",
            rows=_dispatch_rows(database_path),
        )
        after_rows = _dispatch_rows(database_path)
        after_planning_rows = _planning_rows(database_path)
        cold_planning_provider_calls = getattr(
            bundle.deployment.planner, "provider_call_count", None
        )
        fingerprint_probe = {"fail_closed": False}
        if before_rows:
            first = before_rows[0]
            try:
                await bundle.transport.lookup(
                    idempotency_key=str(first["idempotency_key"]),
                    request_fingerprint=str(first["request_fingerprint"]) + "-different",
                )
            except Exception as exc:
                fingerprint_probe = {
                    "fail_closed": True,
                    "exception": type(exc).__name__,
                    "reason": str(exc)[:300],
                }
        report["fail_closed"]["different_fingerprint"] = fingerprint_probe
        report["fail_closed"]["expired_approval"] = _expired_approval_probe(now)
        report["restart"] = {
            "provider_dispatch_rows_unchanged": before_rows == after_rows,
            "provider_receipt_ids_unchanged": [row.get("receipt_id") for row in before_rows]
            == [row.get("receipt_id") for row in after_rows],
            "same_idempotency_not_resent": len(before_rows) == len(after_rows),
            "planning_result_rows_unchanged": planning_rows_before_restart
            == after_planning_rows,
            "planning_provider_calls_before_restart": planning_provider_calls_before_restart,
            "planning_provider_calls_after_restart": cold_planning_provider_calls,
            "planning_same_idempotency_not_resent": (
                cold_planning_provider_calls == 0
                if cold_planning_provider_calls is not None
                else "not_observed"
            ),
            "artifact_bytes_unchanged": (
                preview_before.get("artifact", {}).get("bytes")
                == after_preview.get("artifact", {}).get("bytes")
                and preview_before.get("artifact", {}).get("sha256")
                == after_preview.get("artifact", {}).get("sha256")
            ),
            "artifact_hash_unchanged": preview_before.get("artifact", {}).get("sha256")
            == after_preview.get("artifact", {}).get("sha256"),
            "inspection_result_unchanged": _inspection_state(live_evidence)
            == _inspection_state(cold_after),
            "inspection_hash_unchanged": before_inspection_hash
            == (
                cold_after.projection.media_inspections[0].inspection_payload_hash
                if cold_after.projection.media_inspections
                else None
            ),
            "projection_semantic_hash_unchanged": before_semantic_hash
            == cold_after.projection.semantic_hash,
            "live_projection_matches_live_replay": live_evidence.projection
            == live_evidence.replay,
            "cold_projection_matches_cold_replay": cold_after.projection
            == cold_after.replay,
            "cold_projection_semantic_hash_matches_replay": cold_after.projection.semantic_hash
            == cold_after.replay.semantic_hash,
            "cold_replay_semantic_hash": cold_after.replay.semantic_hash,
            "repeat_drain_preview_count": len(cold_after.projection.media_previews),
        }
        failure_events = [
            item
            for item in _event_types(cold_after)
            if item == "MediaPreviewFailed"
        ]
        report["provider_failure_semantics"] = {
            "observed": bool(failure_events),
            "failure_did_not_become_role_no_op": (
                _provider_failure_did_not_become_role_no_op(
                    report["character_selection"], failure_events
                )
                if failure_events
                else "not_observed"
            ),
            "failure_events": failure_events,
        }
        restart = report["restart"]
        fail_closed = report["fail_closed"]
        replay_live = report["replay_live"]
        artifact = report.get("artifact")
        artifact_valid = _artifact_report_is_valid(artifact, scratch_root=phase_root)
        failed_checks: list[str] = []
        if not artifact_valid:
            failed_checks.append("artifact_bytes_hash_and_path")
        if not planning_rows_before_restart:
            failed_checks.append("planning_durable_result")
        if "MediaPreviewGenerated" not in _event_types(live_evidence):
            failed_checks.append("media_preview_generated_event")
        if not isinstance(replay_live, dict) or not (
            replay_live.get("projection_matches_replay") is True
            and replay_live.get("evaluator_passed") is True
        ):
            failed_checks.append("live_replay")
        if not isinstance(restart, dict):
            failed_checks.append("restart_report")
        else:
            for key in (
                "provider_dispatch_rows_unchanged",
                "provider_receipt_ids_unchanged",
                "same_idempotency_not_resent",
                "planning_result_rows_unchanged",
                "planning_same_idempotency_not_resent",
                "artifact_bytes_unchanged",
                "artifact_hash_unchanged",
                "inspection_result_unchanged",
                "inspection_hash_unchanged",
                "projection_semantic_hash_unchanged",
                "cold_projection_matches_cold_replay",
                "cold_projection_semantic_hash_matches_replay",
            ):
                if restart.get(key) is not True:
                    failed_checks.append(f"restart.{key}")
            if restart.get("repeat_drain_preview_count") != 1:
                failed_checks.append("restart.repeat_drain_preview_count")
        if not isinstance(fail_closed, dict) or any(
            not isinstance(value, dict) or value.get("fail_closed") is not True
            for value in fail_closed.values()
        ):
            failed_checks.append("fail_closed_probes")
        if failed_checks:
            report.update(
                {
                    "status": "qualification_verification_failed",
                    "failure": {"checks_failed": failed_checks},
                    "qualification_complete": False,
                }
            )
            return report
        report.update(
            {
                "status": "qualification_complete",
                "qualification_complete": not deterministic_selection_double,
                "character_selection_qualified": not deterministic_selection_double,
                "no_op_is_failure": False,
            }
        )
        return report
    finally:
        await _close_app(app)
        await _close_bundle(bundle)


def _base_report(*, scratch_root: Path, settings: Settings) -> dict[str, object]:
    return {
        "qualification_scope": "not_started",
        "character_selection_qualified": False,
        "deterministic_selection_double": False,
        "scratch_root": str(scratch_root),
        "scratch_dbs": [],
        "report_path": str(scratch_root / "qualification-report.json"),
        "git": _git_state(),
        "contract": {
            "world_v2_reducer_bundle": "runtime-exported-after-run",
            "character_role_tool": "character_role_media_selection_v1",
            "schema_sha256": None,
            "capabilities_sha256": None,
            "contract_sha256": None,
        },
        "providers": {
            "character_selection": {
                "provider": "deepseek",
                "model": settings.deepseek_model,
                "base_url": _safe_endpoint(settings.deepseek_base_url),
            },
            "planning": {
                "provider": "deepseek",
                "model": settings.world_v2_media_planner_model or settings.deepseek_model,
                "base_url": _safe_endpoint(settings.deepseek_base_url),
            },
            "render": {
                "provider": "openai",
                "model": settings.image_model,
                "base_url": _safe_endpoint(settings.openai_base_url),
            },
            "inspection": {
                "provider": "openai",
                "model": settings.world_v2_media_inspection_model,
                "base_url": _safe_endpoint(settings.openai_base_url),
            },
        },
        "exclusions": {
            "real_qq_dispatch": False,
            "production_db": False,
            "shared_output": False,
            "ports": "none",
            "local_comfyui": False,
            "auto_delivery": False,
            "operator_approval": False,
        },
        "status": "started",
    }


async def main() -> int:
    started = time.perf_counter()
    original_cwd = Path.cwd()
    scratch_root = _new_scratch_root()
    exit_code = 0
    report: dict[str, object]
    role_model: _ObservedRoleModel | None = None
    try:
        with _cwd(REPO_ROOT):
            settings = Settings(
                database_path=scratch_root / "preflight.sqlite",
                VISUAL_IDENTITY_PATH=(REPO_ROOT / "configs" / "visual_identity.yaml").resolve(),
                WORLD_V2_MEDIA_PREVIEW_ENABLED=True,
                NAPCAT_ALLOWED_PRIVATE_USER_IDS="acceptance",
                PRIMARY_USER_ID="acceptance",
            )
        report = _base_report(scratch_root=scratch_root, settings=settings)
        missing = []
        if not settings.deepseek_api_key:
            missing.append("DEEPSEEK_API_KEY")
        if not settings.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if missing:
            report.update(
                {
                    "status": "provider_prerequisite_unavailable",
                    "failure": "missing provider credentials",
                    "missing": missing,
                }
            )
            exit_code = 2
        else:
            run_now = datetime.now(UTC).replace(microsecond=0)
            real_usage: list[ModelCallUsage] = []
            role_model = _role_model(settings, real_usage)
            real_phase = await _run_phase(
                phase_root=scratch_root / "real-selection",
                settings_template=settings,
                role_model=role_model,
                now=run_now,
                deterministic_selection_double=False,
            )
            report["real_phase"] = real_phase
            report["character_selection"] = real_phase.get("character_selection")
            report["scratch_dbs"] = [real_phase.get("scratch_db")]
            report["latency_ms"] = real_phase.get("latency_ms", {})
            report["latency_semantics"] = real_phase.get("latency_semantics")
            report["ids"] = real_phase.get("ids")
            report["artifact"] = real_phase.get("artifact")
            report["restart"] = real_phase.get("restart")
            report["fail_closed"] = real_phase.get("fail_closed")
            report["usage_cost"] = real_phase.get("usage_cost")
            report["provider_failure_semantics"] = real_phase.get("provider_failure_semantics")
            if real_phase.get("status") not in {"qualification_complete", "legal_character_no_op"}:
                report["current_failure"] = real_phase.get("failure") or real_phase.get(
                    "failure_details"
                )
            selection = real_phase.get("character_selection") or {}
            tool_contract = selection.get("tool_contract") if isinstance(selection, dict) else None
            if isinstance(tool_contract, dict):
                report["contract"] = {
                    "world_v2_reducer_bundle": real_phase.get("replay_live", {}).get("reducer_bundle_version")
                    if isinstance(real_phase.get("replay_live"), dict)
                    else "runtime-exported-after-run",
                    "character_role_tool": tool_contract.get("tool_name"),
                    "schema_sha256": tool_contract.get("schema_sha256"),
                    "capabilities_sha256": tool_contract.get("capabilities_sha256"),
                    "contract_sha256": tool_contract.get("contract_sha256"),
                }
            if real_phase.get("status") == "qualification_complete":
                report.update(
                    _qualification_fields(
                        status="qualification_complete",
                        deterministic_selection_double=False,
                    )
                )
            elif (
                real_phase.get("status") == "legal_character_no_op"
                and os.environ.get("ACCEPTANCE_DETERMINISTIC_SELECTION") == "1"
            ):
                downstream_role = _DeterministicSelectionDouble(text_delegate=role_model)
                downstream = await _run_phase(
                    phase_root=scratch_root / "downstream-provider-stages",
                    settings_template=settings,
                    role_model=downstream_role,
                    now=run_now,
                    deterministic_selection_double=True,
                )
                report["downstream_phase"] = downstream
                report["scratch_dbs"].append(downstream.get("scratch_db"))
                report.update(
                    {
                        **_qualification_fields(
                            status=downstream.get("status"),
                            deterministic_selection_double=True,
                        ),
                        "downstream_provider_stages": {
                            "status": downstream.get("status"),
                            "planning": downstream.get("planning"),
                        "latency_ms": downstream.get("latency_ms"),
                        "latency_semantics": downstream.get("latency_semantics"),
                            "ids": downstream.get("ids"),
                            "artifact": downstream.get("artifact"),
                        "restart": downstream.get("restart"),
                        "fail_closed": downstream.get("fail_closed"),
                        "usage_cost": downstream.get("usage_cost"),
                            "provider_failure_semantics": downstream.get(
                                "provider_failure_semantics"
                            ),
                        },
                    }
                )
                if downstream.get("status") not in {
                    "qualification_complete",
                    "legal_character_no_op",
                }:
                    exit_code = 5
            elif real_phase.get("status") in {
                "legal_character_no_op",
                "source_candidate_unavailable",
            }:
                report.update(
                    _qualification_fields(
                        status=real_phase.get("status"),
                        deterministic_selection_double=False,
                    )
                )
            else:
                report.update(
                    _qualification_fields(
                        status=real_phase.get("status"),
                        deterministic_selection_double=False,
                    )
                )
                exit_code = 5
    except Exception as exc:
        report = locals().get("report", {})
        report.update(
            {
                "status": "harness_exception",
                "failure": {
                    "type": type(exc).__name__,
                    "reason": str(exc)[:500],
                },
            }
        )
        exit_code = 10
    finally:
        report["duration_ms"] = _latency(started)
        report_path = scratch_root / "qualification-report.json"
        try:
            _write_json(report_path, report)
        except Exception as exc:
            report["report_write_error"] = f"{type(exc).__name__}: {exc}"
        with _cwd(original_cwd):
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
