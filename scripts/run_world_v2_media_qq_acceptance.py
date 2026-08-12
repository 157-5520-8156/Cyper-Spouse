#!/usr/bin/env python
"""Run one isolated real-media -> real-QQ terminal-receipt acceptance.

This harness has an intentional external side effect: after a real
CharacterInterior ``select`` and real provider generation/inspection, it may
send exactly one ordinary, shareable image to one private QQ recipient through
NapCat/OneBot.  It is default-deny and requires all of these independent gates:

* ``WORLD_V2_REAL_MEDIA_QQ_ACCEPTANCE=1``;
* ``--recipient`` with one numeric QQ id;
* ``--consent-recipient`` repeating the same id to attest one-image consent;
* ``--confirm-send SEND_ONE_MEDIA_TO_QQ_<id>``;
* the deployment allowlist containing exactly that one id;
* real DeepSeek/OpenAI credentials and a NapCat/OneBot adapter.

All SQLite files, artifacts, the QQ audit outbox and the sanitized report live
under a fresh private ``/tmp`` root.  The script never opens the configured
production database.  A legal role ``no_op`` ends without sending.  A provider
ack is not success: the Action is restarted after its lease and must be upgraded
by a positive OneBot ``get_msg`` to a delivered ``ExecutionReceiptRecorded`` and
one ``MediaDeliveryShared``.  No result from this one-shot harness is reported
as production qualification.

Example (the recipient must already be the sole configured private allowlist):

    WORLD_V2_REAL_MEDIA_QQ_ACCEPTANCE=1 \
      .venv/bin/python scripts/run_world_v2_media_qq_acceptance.py \
      --recipient 123456789 \
      --consent-recipient 123456789 \
      --confirm-send SEND_ONE_MEDIA_TO_QQ_123456789
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))

_ENABLE_ENV = "WORLD_V2_REAL_MEDIA_QQ_ACCEPTANCE"
_QQ_ID = re.compile(r"^[1-9][0-9]{4,19}$")
_SCRATCH_PREFIX = "girl-agent-media-qq."
_REPORT_NAME = "qualification-report.json"


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    authorized: bool
    reason_codes: tuple[str, ...]


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send at most one isolated, generated World v2 image to QQ."
    )
    parser.add_argument("--recipient", help="Exact numeric private QQ recipient id.")
    parser.add_argument(
        "--consent-recipient",
        help="Repeat the recipient id to attest consent for this one ordinary image.",
    )
    parser.add_argument(
        "--confirm-send",
        help="Must equal SEND_ONE_MEDIA_TO_QQ_<recipient>.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _authorize_cli(
    args: argparse.Namespace, *, environ: Mapping[str, str]
) -> AuthorizationDecision:
    reasons: list[str] = []
    # These exact bytes become World target coordinates and the durable report
    # identity.  Reject whitespace variants rather than authorizing a trimmed
    # value and later executing with the raw CLI value.
    recipient = str(args.recipient or "")
    consent_recipient = str(args.consent_recipient or "")
    confirmation = str(args.confirm_send or "")
    if environ.get(_ENABLE_ENV) != "1":
        reasons.append("real_qq_acceptance_switch_disabled")
    if not _QQ_ID.fullmatch(recipient):
        reasons.append("recipient_not_one_numeric_private_qq_id")
    if not recipient or consent_recipient != recipient:
        reasons.append("recipient_consent_mismatch")
    if not recipient or confirmation != f"SEND_ONE_MEDIA_TO_QQ_{recipient}":
        reasons.append("single_send_confirmation_mismatch")
    return AuthorizationDecision(authorized=not reasons, reason_codes=tuple(reasons))


def _authorize_settings(
    args: argparse.Namespace, *, settings: object
) -> AuthorizationDecision:
    reasons: list[str] = []
    recipient = str(args.recipient or "")
    adapter = str(getattr(settings, "qq_adapter", "") or "").strip().lower()
    allowed = tuple(
        item.strip()
        for item in str(
            getattr(settings, "napcat_allowed_private_user_ids", "") or ""
        ).split(",")
        if item.strip()
    )
    if adapter not in {"napcat", "onebot"}:
        reasons.append("qq_adapter_not_receipt_capable")
    if allowed != (recipient,):
        reasons.append("configured_recipient_scope_mismatch")
    missing_providers = tuple(
        name
        for name, value in (
            ("DEEPSEEK_API_KEY", getattr(settings, "deepseek_api_key", None)),
            ("OPENAI_API_KEY", getattr(settings, "openai_api_key", None)),
        )
        if not value
    )
    if missing_providers:
        reasons.append("provider_credentials_missing")
    return AuthorizationDecision(authorized=not reasons, reason_codes=tuple(reasons))


def _new_scratch_root(*, base_dir: Path = Path("/tmp")) -> Path:
    base = Path(base_dir).resolve()
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=base)).resolve()
    if root.parent != base:
        raise RuntimeError("media QQ acceptance scratch root escaped its base directory")
    root.chmod(0o700)
    return root


def _require_scratch_path(path: Path, scratch_root: Path) -> Path:
    resolved = Path(path).resolve()
    root = Path(scratch_root).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("path is outside the isolated media QQ scratch root") from exc
    return resolved


def _scratch_path(scratch_root: Path, relative: str) -> Path:
    candidate = _require_scratch_path(Path(scratch_root) / relative, scratch_root)
    candidate.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    candidate.parent.chmod(0o700)
    return candidate


@contextmanager
def _cwd(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _base_report(*, scratch_root: Path, recipient_id: str) -> dict[str, object]:
    return {
        "status": "started",
        "qualification_scope": "one_shot_isolated_real_media_to_qq",
        "bounded_acceptance_complete": False,
        "production_qualified": False,
        "qualification_complete": False,
        "qualification_incomplete": True,
        "manual_only": True,
        "scratch_root": str(Path(scratch_root).resolve()),
        "report_path": str((Path(scratch_root) / _REPORT_NAME).resolve()),
        "recipient": {
            "id_sha256": "sha256:" + _sha256_text(recipient_id),
            "raw_id_persisted_in_report": False,
        },
        "consent": {
            "explicit_recipient_match": True,
            "attestation_kind": "cli_operator_attestation",
            "durable_world_consent_event_for_delivery": False,
            "scope": "one ordinary shareable image to the exact allowlisted private QQ target",
            "intimate_media_authorized": False,
        },
        "send_policy": {
            "max_image_sends": 1,
            "non_media_actions_allowed": False,
            "retry_after_external_attempt_allowed": False,
        },
        "resource_isolation": {
            "production_db": False,
            "shared_output": False,
            "scratch_private_mode": "0700",
            "qq_outbox_below_scratch": True,
        },
        "upstream_scope": {
            "candidate_source": "isolated_source_bound_ordinary_life_evidence",
            "normal_conversation_candidate_generation_qualified": False,
        },
        "human_receipt_confirmation": False,
    }


def _write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    path.chmod(0o600)


class _SingleImageDelivery:
    """Hard stop around QQDelivery: exact recipient, image only, one attempt."""

    def __init__(
        self,
        *,
        delegate: object,
        recipient_id: str,
        scratch_root: Path | None = None,
    ) -> None:
        self._delegate = delegate
        self._recipient_id = recipient_id
        self._scratch_root = Path(scratch_root).resolve() if scratch_root else None
        self.image_send_attempts = 0
        self.lookup_attempts = 0

    def _require_recipient(self, recipient_id: str) -> None:
        if recipient_id != self._recipient_id:
            raise RuntimeError("media QQ acceptance recipient does not match authorization")

    @staticmethod
    def _reject_non_media() -> None:
        raise RuntimeError("media QQ acceptance rejects every non-media outbound action")

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        del recipient_id, text
        self._reject_non_media()

    async def send_reaction(
        self, recipient_id: str, *, message_id: str, reaction_id: str
    ) -> dict[str, object]:
        del recipient_id, message_id, reaction_id
        self._reject_non_media()

    async def send_sticker(
        self, recipient_id: str, *, sticker_id: str
    ) -> dict[str, object]:
        del recipient_id, sticker_id
        self._reject_non_media()

    async def send_typing(
        self, recipient_id: str, *, state: str
    ) -> dict[str, object]:
        del recipient_id, state
        self._reject_non_media()

    async def send_image_message(
        self, recipient_id: str, *, image_path: Path
    ) -> dict[str, object]:
        self._require_recipient(recipient_id)
        if self.image_send_attempts >= 1:
            raise RuntimeError("media QQ acceptance single-send limit already consumed")
        resolved = Path(image_path).resolve()
        if self._scratch_root is not None:
            _require_scratch_path(resolved, self._scratch_root)
            if stat.S_IMODE(resolved.stat().st_mode) != 0o600:
                raise RuntimeError("media QQ acceptance audit image is not private")
        # Consume the only slot before crossing the external boundary.  A
        # timeout/exception is uncertain and must never authorize a retry.
        self.image_send_attempts += 1
        method = getattr(self._delegate, "send_image_message", None)
        if not callable(method):
            raise RuntimeError("configured QQ adapter has no image-message capability")
        return await method(recipient_id, image_path=resolved)

    async def get_message(
        self, recipient_id: str, *, message_id: str
    ) -> dict[str, object]:
        self._require_recipient(recipient_id)
        if self.lookup_attempts >= 1:
            raise RuntimeError("media QQ acceptance terminal lookup already attempted")
        self.lookup_attempts += 1
        method = getattr(self._delegate, "get_message", None)
        if not callable(method):
            raise RuntimeError("configured QQ adapter has no get_msg capability")
        return await method(recipient_id, message_id=message_id)


class _QQIdentities:
    def __init__(self, *, recipient_id: str, target: str) -> None:
        self._recipient_id = recipient_id
        self._target = target

    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        del platform, platform_user_id
        return (f"user:{self._recipient_id}", self._target)


def _load_preview_harness():
    # The preview harness contains the already-qualified real provider setup.
    # Import it only after all external-send authorization gates pass.
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    return importlib.import_module("run_world_v2_media_preview_acceptance")


def _event_count_for_action(evidence: object, event_type: str, action_id: str) -> int:
    count = 0
    for committed in getattr(evidence, "events", ()):
        event = committed.event
        if event.event_type != event_type:
            continue
        payload = event.payload()
        event_action_id = payload.get("action_id")
        receipt = payload.get("receipt")
        delivery = payload.get("delivery")
        if isinstance(receipt, dict):
            event_action_id = receipt.get("action_id")
        if isinstance(delivery, dict):
            event_action_id = delivery.get("action_id")
        if event_action_id == action_id:
            count += 1
    return count


def _outbox_report(*, phase_root: Path) -> dict[str, object]:
    outbox = _require_scratch_path(phase_root / "output" / "media-delivered", phase_root)
    files = tuple(sorted(outbox.glob("*.png"))) if outbox.exists() else ()
    return {
        "path": str(outbox),
        "directory_mode": (
            format(stat.S_IMODE(outbox.stat().st_mode), "04o") if outbox.exists() else None
        ),
        "png_count": len(files),
        "file_modes": [format(stat.S_IMODE(item.stat().st_mode), "04o") for item in files],
        "artifact_sha256": [
            "sha256:" + hashlib.sha256(item.read_bytes()).hexdigest() for item in files
        ],
    }


def _phase_settings(
    *, settings: object, database_path: Path, recipient_id: str
):
    return settings.model_copy(
        update={
            "database_path": database_path,
            "visual_identity_path": (
                REPO_ROOT / "configs" / "visual_identity.yaml"
            ).resolve(),
            "world_v2_media_preview_enabled": True,
            "napcat_allowed_private_user_ids": recipient_id,
            "primary_user_id": recipient_id,
        }
    )


def _build_delivery_app(
    *,
    preview: object,
    bundle: object,
    settings: object,
    database_path: Path,
    recipient_id: str,
    role_model: object,
    delivery: _SingleImageDelivery,
    now: datetime,
):
    from companion_daemon.world_v2.qq_c2c_host import qq_c2c_target
    from companion_daemon.world_v2.qq_c2c_transport import QQC2CPlatformTransport

    target = qq_c2c_target(recipient_id)
    deployment = bundle.deployment
    if deployment.auto_delivery is None:
        raise RuntimeError("real QQ acceptance deployment has no media auto-delivery seam")
    config = replace(
        preview._config(media_bundle=bundle),
        reply_target=target,
        action_pump_owner="pump:media-qq-acceptance",
        media_auto_delivery=deployment.auto_delivery,
    )
    return preview.build_sqlite_world_v2_turn_application(
        path=database_path,
        config=config,
        identities=_QQIdentities(recipient_id=recipient_id, target=target),
        router=preview._Router(),
        character_interior=preview._new_character_interior(role_model),
        transport=QQC2CPlatformTransport(
            delivery=delivery,
            recipients_by_target={target: recipient_id},
            now=lambda: datetime.now(UTC),
        ),
        media_transport=bundle.transport,
        media_planner=deployment.planner,
        now=now,
    )


async def _deliver_preview_once(
    *,
    preview: object,
    phase_root: Path,
    database_path: Path,
    artifacts_root: Path,
    settings: object,
    recipient_id: str,
    role_model: object,
    now: datetime,
    report: dict[str, object],
) -> bool:
    from companion_daemon.qq_delivery import QQDelivery

    phase_settings = _phase_settings(
        settings=settings,
        database_path=database_path,
        recipient_id=recipient_id,
    )
    delivery = _SingleImageDelivery(
        delegate=QQDelivery(phase_settings),
        recipient_id=recipient_id,
        scratch_root=phase_root,
    )
    report.update(
        {
            "status": "delivery_not_started",
            "image_send_attempts": 0,
            "get_msg_attempts": 0,
            "restart_after_provider_accepted": False,
        }
    )
    app: object | None = None
    bundle: object | None = None
    try:
        bundle = preview._build_media_bundle(
            settings=phase_settings,
            phase_root=phase_root,
            artifacts_root=artifacts_root,
        )
        if bundle is None:
            raise RuntimeError("real QQ acceptance media deployment is unavailable")
        with _cwd(phase_root):
            app = _build_delivery_app(
                preview=preview,
                bundle=bundle,
                settings=phase_settings,
                database_path=database_path,
                recipient_id=recipient_id,
                role_model=role_model,
                delivery=delivery,
                now=now,
            )
            before = app.export_replay_evidence()
            if len(before.projection.media_previews) != 1:
                raise RuntimeError("real QQ acceptance requires exactly one inspected preview")
            if before.projection.media_deliveries:
                raise RuntimeError("real QQ acceptance scratch world is already delivered")
            attempted = await app.drain_media_auto_delivery_once(
                trace_id="trace:media-qq-acceptance:dispatch",
                correlation_id="correlation:media-qq-acceptance",
            )
            action_id = getattr(attempted, "action_id", None)
            if getattr(attempted, "status", None) != "delivered_attempted" or not action_id:
                raise RuntimeError("world-owned media delivery did not authorize one Action")
            accepted = app.export_replay_evidence()
            action = next(
                (item for item in accepted.projection.actions if item.action_id == action_id),
                None,
            )
            if action is None or action.state != "provider_accepted":
                raise RuntimeError("QQ synchronous send did not produce provider_accepted")
            if action.claim_lease is None:
                raise RuntimeError("provider_accepted media Action lost its recovery lease")
            report.update(
                {
                    "status": "provider_accepted",
                    "action_id": action_id,
                    "image_send_attempts": delivery.image_send_attempts,
                    "provider_accepted_execution_receipt": any(
                        item.action_id == action_id
                        and item.observed_state == "provider_accepted"
                        for item in accepted.projection.execution_receipts
                    ),
                }
            )
            lease_expires_at = action.claim_lease.expires_at
            logical_time = accepted.projection.logical_time
            if logical_time is None:
                raise RuntimeError("provider receipt recovery has no logical clock")
        await preview._close_app(app)
        app = None
        await preview._close_bundle(bundle)
        bundle = None

        # Cold restart is required here.  The new platform transport has no
        # process-local send receipt cache and can only use the ledger's ack plus
        # read-only get_msg.  Move World time beyond the dispatch lease; never
        # sleep or re-send.
        bundle = preview._build_media_bundle(
            settings=phase_settings,
            phase_root=phase_root,
            artifacts_root=artifacts_root,
        )
        if bundle is None:
            raise RuntimeError("media deployment disappeared before QQ receipt recovery")
        with _cwd(phase_root):
            app = _build_delivery_app(
                preview=preview,
                bundle=bundle,
                settings=phase_settings,
                database_path=database_path,
                recipient_id=recipient_id,
                role_model=role_model,
                delivery=delivery,
                now=now,
            )
            recovery_time = lease_expires_at + timedelta(seconds=1)
            await app.tick(
                tick_id="media-qq-acceptance:receipt-recovery-clock",
                logical_time_from=logical_time,
                logical_time_to=recovery_time,
                observed_at=recovery_time,
                trace_id="trace:media-qq-acceptance:receipt-recovery-clock",
                causation_id=action_id,
                correlation_id="correlation:media-qq-acceptance",
                reason="read-only QQ terminal receipt recovery",
            )
            recovered = await app.drain_action(action_id)
            repeated_action = await app.drain_action(action_id)
            repeated_delivery = await app.drain_media_auto_delivery_once(
                trace_id="trace:media-qq-acceptance:effect-once",
                correlation_id="correlation:media-qq-acceptance",
            )
            evidence = app.export_replay_evidence()

        receipts = tuple(
            item for item in evidence.projection.execution_receipts if item.action_id == action_id
        )
        receipt_states = tuple(item.observed_state for item in receipts)
        provider_ref_hashes = tuple(
            "sha256:" + _sha256_text(item.provider_ref) for item in receipts
        )
        event_counts = {
            event_type: _event_count_for_action(evidence, event_type, action_id)
            for event_type in (
                "ActionDispatchStarted",
                "ActionProviderAccepted",
                "ExecutionReceiptRecorded",
                "MediaDeliveryShared",
            )
        }
        outbox = _outbox_report(phase_root=phase_root)
        checks = {
            "recovered_delivered": (
                getattr(recovered, "status", None) == "settled"
                and getattr(recovered, "provider_status", None) == "delivered"
            ),
            "one_external_image_attempt": delivery.image_send_attempts == 1,
            "one_read_only_get_msg": delivery.lookup_attempts == 1,
            "receipt_states_provider_accepted_then_delivered": receipt_states
            == ("provider_accepted", "delivered"),
            "one_media_delivery_shared": len(evidence.projection.media_deliveries) == 1,
            "targeted_action_replay_idle": getattr(repeated_action, "status", None) == "idle",
            "auto_delivery_replay_idle": getattr(repeated_delivery, "status", None) == "idle",
            "projection_matches_cold_replay": evidence.projection == evidence.replay,
            "one_dispatch_started": event_counts["ActionDispatchStarted"] == 1,
            "one_provider_accepted_event": event_counts["ActionProviderAccepted"] == 1,
            "two_execution_receipts": event_counts["ExecutionReceiptRecorded"] == 2,
            "one_media_delivery_event": event_counts["MediaDeliveryShared"] == 1,
            "private_single_png_outbox": (
                outbox["directory_mode"] == "0700"
                and outbox["png_count"] == 1
                and outbox["file_modes"] == ["0600"]
            ),
        }
        failed = tuple(name for name, passed in checks.items() if not passed)
        report.update(
            {
                "status": (
                    "real_media_qq_bounded_acceptance_complete"
                    if not failed
                    else "terminal_receipt_verification_failed"
                ),
                "restart_after_provider_accepted": True,
                "image_send_attempts": delivery.image_send_attempts,
                "get_msg_attempts": delivery.lookup_attempts,
                "receipt_observed_states": list(receipt_states),
                "provider_ref_hashes": list(provider_ref_hashes),
                "event_counts": event_counts,
                "effect_once_checks": checks,
                "failed_checks": list(failed),
                "outbox": outbox,
                "human_receipt_confirmation": False,
            }
        )
        return not failed
    finally:
        report["image_send_attempts"] = delivery.image_send_attempts
        report["get_msg_attempts"] = delivery.lookup_attempts
        await preview._close_app(app)
        await preview._close_bundle(bundle)


async def _run_authorized(
    *,
    args: argparse.Namespace,
    settings: object,
    scratch_root: Path,
    report: dict[str, object],
) -> int:
    # This switch is process-local and the only database passed below is inside
    # scratch_root.  It never provisions the deployment/production ledger.
    os.environ.setdefault("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    preview = _load_preview_harness()
    recipient_id = str(args.recipient)
    phase_root = _scratch_path(scratch_root, "real-media-qq")
    phase_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    phase_root.chmod(0o700)
    artifacts_root = _scratch_path(phase_root, "artifacts")
    artifacts_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifacts_root.chmod(0o700)
    run_now = datetime.now(UTC).replace(microsecond=0)
    phase_database = _scratch_path(phase_root, "media.sqlite")
    phase_settings = _phase_settings(
        settings=settings,
        database_path=phase_database,
        recipient_id=recipient_id,
    )
    usage: list[object] = []
    role_model = preview._role_model(phase_settings, usage)
    generation = await preview._run_phase(
        phase_root=phase_root,
        settings_template=phase_settings,
        role_model=role_model,
        now=run_now,
        deterministic_selection_double=False,
    )
    report["generation"] = generation
    report["scratch_db"] = str(phase_database)
    report["character_selection"] = generation.get("character_selection")
    report["artifact"] = generation.get("artifact")
    report["generation_status"] = generation.get("status")
    if generation.get("status") == "legal_character_no_op":
        report.update(
            {
                "status": "legal_character_no_op",
                "no_op_is_failure": False,
                "delivery": {
                    "status": "not_attempted_role_no_op",
                    "image_send_attempts": 0,
                    "get_msg_attempts": 0,
                },
            }
        )
        return 0
    if generation.get("status") != "isolated_acceptance_complete":
        report.update(
            {
                "status": "real_media_generation_not_qualified",
                "delivery": {
                    "status": "not_attempted_generation_incomplete",
                    "image_send_attempts": 0,
                    "get_msg_attempts": 0,
                },
            }
        )
        return 5

    delivery_report: dict[str, object] = {}
    report["delivery"] = delivery_report
    complete = await _deliver_preview_once(
        preview=preview,
        phase_root=phase_root,
        database_path=phase_database,
        artifacts_root=artifacts_root,
        settings=phase_settings,
        recipient_id=recipient_id,
        role_model=role_model,
        now=run_now,
        report=delivery_report,
    )
    if complete:
        report.update(
            {
                "status": "real_media_qq_bounded_acceptance_complete",
                "bounded_acceptance_complete": True,
                # One sample is deliberately never a release/production claim.
                "production_qualified": False,
                "qualification_complete": False,
                "qualification_incomplete": True,
                "manual_only": True,
            }
        )
        return 0
    report["status"] = "real_media_qq_terminal_receipt_incomplete"
    return 6


async def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    scratch_factory: Callable[[], Path] | None = None,
) -> int:
    args = _parse_args(argv)
    selected_environ = os.environ if environ is None else environ
    cli_authorization = _authorize_cli(args, environ=selected_environ)
    if not cli_authorization.authorized:
        print(
            json.dumps(
                {
                    "status": "refused_before_scratch",
                    "reason_codes": list(cli_authorization.reason_codes),
                    "scratch_allocated": False,
                    "external_send_attempted": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2

    from companion_daemon.config import Settings

    # The placeholder path is configuration-only.  No SQLite connection occurs
    # until after settings and recipient authorization and scratch allocation.
    settings = Settings(
        database_path=Path("/tmp") / "girl-agent-media-qq-not-opened.sqlite",
        VISUAL_IDENTITY_PATH=(REPO_ROOT / "configs" / "visual_identity.yaml").resolve(),
        WORLD_V2_MEDIA_PREVIEW_ENABLED=True,
        PRIMARY_USER_ID=str(args.recipient),
    )
    settings_authorization = _authorize_settings(args, settings=settings)
    if not settings_authorization.authorized:
        print(
            json.dumps(
                {
                    "status": "refused_before_scratch",
                    "reason_codes": list(settings_authorization.reason_codes),
                    "scratch_allocated": False,
                    "external_send_attempted": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 3

    factory = scratch_factory or _new_scratch_root
    scratch_root = Path(factory()).resolve()
    scratch_root.chmod(0o700)
    report = _base_report(
        scratch_root=scratch_root,
        recipient_id=str(args.recipient),
    )
    report["authorization"] = {
        "environment_switch": True,
        "recipient_consent_match": True,
        "single_send_phrase_match": True,
        "configured_allowlist_exact_match": True,
        "qq_adapter": str(settings.qq_adapter),
    }
    report_path = _require_scratch_path(scratch_root / _REPORT_NAME, scratch_root)
    exit_code = 10
    try:
        exit_code = await _run_authorized(
            args=args,
            settings=settings,
            scratch_root=scratch_root,
            report=report,
        )
    except Exception as exc:
        # Exception messages may include provider bodies or local endpoints; the
        # durable private DB retains detailed evidence, while the report exposes
        # only a sanitized class/reason code.
        report.update(
            {
                "status": "harness_exception",
                "failure": {
                    "type": type(exc).__name__,
                    "reason_code": "unhandled_media_qq_acceptance_exception",
                },
                "production_qualified": False,
                "qualification_complete": False,
            }
        )
        exit_code = 10
    finally:
        _write_private_json(report_path, report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
