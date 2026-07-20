"""Production factory for the QQ World v2 perception (vision) deployment.

This is the one place that turns deployment ``Settings`` into the complete,
explicit perception injection set for ``build_qq_c2c_host``:

- the decision model mirrors the chat lane's Flash provider route
  (DeepSeek primary, ``WORLD_V2_FALLBACK_MODEL`` through the OpenAI proxy)
  wrapped in :class:`QQPerceptionDecisionModel`, which owns the restrained
  trigger semantics (archived-image gate, exact-bytes dedupe, durable daily
  cap, one bounded look/skip confirmation);
- the durable input source is the URL-free :class:`QQAttachmentArchive`
  under ``ATTACHMENT_CACHE_PATH``, fed at the adapter boundary by
  :class:`QQOneBotAttachmentArchiver`;
- the transport is :class:`SQLiteDurableVisionPerceptionTransport` against
  the world database file, so restart recovery replays exact stored text.

Missing prerequisites (budget limit 0, absent OpenAI/DeepSeek credentials, or
an unprovisioned perception enforcement chain) disable the whole lane with
exactly one log line; ingress and replies are never affected.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3

from companion_daemon.config import Settings

from .perception_authority_provisioning import (
    PERCEPTION_CONSENT_ID,
    PERCEPTION_PRIVACY_POLICY_ID,
    PERCEPTION_VISION_CAPABILITY_ID,
)
from .perception_decision_adapter import QQPerceptionDecisionModel
from .perception_vision_transport import SQLiteDurableVisionPerceptionTransport
from .qq_attachment_archive import QQAttachmentArchive, QQOneBotAttachmentArchiver


_LOG = logging.getLogger(__name__)

# Must match ``WorldV2TurnApplicationConfig.perception_account_id`` — the
# compiler rejects any proposal whose payload names a different account.
PERCEPTION_ACCOUNT_ID = "account:world-v2:perception"

_REQUIRED_AUTHORITY = (
    ("CapabilityGranted", PERCEPTION_VISION_CAPABILITY_ID),
    ("ConsentGranted", PERCEPTION_CONSENT_ID),
    ("PrivacyPolicyRevised", PERCEPTION_PRIVACY_POLICY_ID),
)


@dataclass(frozen=True, slots=True)
class QQPerceptionDeploymentBundle:
    """Everything ``create_qq_c2c_onebot_app`` needs to install perception."""

    model: QQPerceptionDecisionModel
    input_source: QQAttachmentArchive
    transport: SQLiteDurableVisionPerceptionTransport
    budget_limit: int
    archiver: QQOneBotAttachmentArchiver

    def close(self) -> None:
        self.transport.close()


def _provisioned_authority_present(*, database_path: Path, world_id: str) -> bool:
    """Cheap preflight: is the perception enforcement chain recorded?

    This only gates lane composition; the authorization resolver, reducers
    and ActionPump remain the enforcement authority for every dispatch.
    """

    if not database_path.exists():
        return False
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        for event_type, entity_id in _REQUIRED_AUTHORITY:
            row = connection.execute(
                "SELECT COUNT(*) FROM world_v2_events WHERE world_id = ? "
                "AND json_extract(event_json, '$.event_type') = ? "
                "AND event_json LIKE ?",
                (world_id, event_type, f"%{entity_id}%"),
            ).fetchone()
            if row is None or int(row[0]) < 1:
                return False
        return True
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def build_qq_perception_deployment(
    *,
    settings: Settings,
    world_id: str,
    api_url: str,
    access_token: str | None = None,
) -> QQPerceptionDeploymentBundle | None:
    """Assemble the perception lane, or disable it with one log line."""

    missing: list[str] = []
    if settings.world_v2_perception_budget_limit <= 0:
        missing.append("PERCEPTION_BUDGET_LIMIT (0 disables the lane)")
    if not settings.openai_api_key:
        missing.append("OPENAI_API_KEY")
    if not settings.deepseek_api_key:
        missing.append("DEEPSEEK_API_KEY")
    database_path = Path(settings.database_path)
    if not missing and not _provisioned_authority_present(
        database_path=database_path, world_id=world_id
    ):
        missing.append(
            "perception enforcement authority "
            "(scripts/provision_world_v2_perception_authority.py)"
        )
    if missing:
        _LOG.warning(
            "world v2 perception lane disabled for %s; missing: %s",
            world_id,
            ", ".join(missing),
        )
        return None

    from companion_daemon.llm import (
        DeepSeekChatModel,
        FailoverChatModel,
        OpenAICompatibleChatModel,
    )

    decision_model = FailoverChatModel(
        primary=DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            thinking_enabled=False,
        ),
        fallback=OpenAICompatibleChatModel(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.world_v2_fallback_model,
            reasoning_effort="none",
            max_completion_tokens=200,
            proxy_url=settings.openai_proxy_url,
        ),
    )
    archive = QQAttachmentArchive(Path(settings.attachment_cache_path) / "qq-c2c-v2")
    transport = SQLiteDurableVisionPerceptionTransport(
        database_path,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.vision_model,
        proxy_url=settings.openai_proxy_url,
    )
    model = QQPerceptionDecisionModel(
        model=decision_model,
        input_source=archive,
        dispatch_evidence=transport,
        budget_account_id=PERCEPTION_ACCOUNT_ID,
        budget_limit=settings.world_v2_perception_budget_limit,
        daily_limit=settings.world_v2_perception_budget_limit,
        local_timezone=settings.local_timezone,
    )
    archiver = QQOneBotAttachmentArchiver(
        archive=archive, api_url=api_url, access_token=access_token
    )
    _LOG.warning(
        "world v2 perception lane enabled for %s (vision=%s, daily_limit=%d)",
        world_id,
        settings.vision_model,
        settings.world_v2_perception_budget_limit,
    )
    return QQPerceptionDeploymentBundle(
        model=model,
        input_source=archive,
        transport=transport,
        budget_limit=settings.world_v2_perception_budget_limit,
        archiver=archiver,
    )


__all__ = [
    "PERCEPTION_ACCOUNT_ID",
    "QQPerceptionDeploymentBundle",
    "build_qq_perception_deployment",
]
