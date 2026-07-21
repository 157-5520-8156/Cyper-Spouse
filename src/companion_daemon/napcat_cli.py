"""NapCat (OneBot v11) adapter CLI.

The CLI runs the dedicated World v2 C2C lane.  The historical Engine/QQ
coalescer lane was retired; its source lives in git history only.  A
*compatible* deployment is single-recipient private text: groups, multiple
private recipients, and other shapes must be rejected here instead of being
silently narrowed into ambiguous world ownership.  ``--world-v2-c2c`` remains
as an explicit override and ``--archive-qq`` now fails fast with a clear
error instead of resurrecting the removed runtime.
"""

from __future__ import annotations

import argparse
import logging
import os
from typing import TYPE_CHECKING

import uvicorn
from fastapi import FastAPI

from companion_daemon.config import Settings, get_settings
from companion_daemon.process_lock import AlreadyRunningError
from companion_daemon.qq_outbound_owner import (
    QQOutboundOwnerLease,
    qq_outbound_owner_lock_path,
    validate_qq_outbound_configuration,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from companion_daemon.world_v2.platform_action_executor import MediaProviderTransport
    from companion_daemon.world_v2.production_turn_application import MediaPreviewDeployment


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _parse_optional_boolean(value: str | None, *, name: str) -> bool | None:
    """Parse an unset/explicit migration switch without guessing its intent."""

    if value is None or not value.strip():
        return None
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be one of true/false, not {value!r}")


def _qq_c2c_v2_is_compatible(settings: Settings) -> bool:
    """Whether this process can select the narrow V2 C2C adapter by default.

    The V2 adapter deliberately has one relationship and supports private
    text only.  Choosing it for a group-enabled or multi-recipient deployment
    would turn otherwise valid traffic into ambiguous world ownership.  Those
    deployments must be rejected until their own V2 adapter exists.
    """

    return (
        not settings.napcat_allow_group_messages
        and len(_parse_id_list(settings.napcat_allowed_private_user_ids)) == 1
    )


def resolve_cli_world_v2_c2c_selection(
    *, settings: Settings, requested: bool | None
) -> bool:
    """Select exactly one QQ authority for the documented CLI entry point.

    Precedence is intentionally simple: an argument wins; the legacy boolean
    environment override remains supported; otherwise a compatible C2C-only
    deployment defaults to World v2.  ``WORLD_V2_QQ_C2C_MODE=archive`` is kept
    as a recognized value so an operator gets an explicit removal error from
    ``create_app`` instead of a confusing unknown-mode failure, while ``=v2``
    verifies that the current deployment is actually compatible instead of
    silently narrowing it.
    """

    if requested is not None:
        return requested

    legacy_override = _parse_optional_boolean(
        os.getenv("WORLD_V2_QQ_C2C_ENABLED"), name="WORLD_V2_QQ_C2C_ENABLED"
    )
    if legacy_override is not None:
        return legacy_override

    mode = os.getenv("WORLD_V2_QQ_C2C_MODE", "auto").strip().lower() or "auto"
    if mode == "archive":
        return False
    compatible = _qq_c2c_v2_is_compatible(settings)
    if mode == "v2":
        if not compatible:
            raise ValueError(
                "WORLD_V2_QQ_C2C_MODE=v2 requires exactly one "
                "NAPCAT_ALLOWED_PRIVATE_USER_IDS entry and "
                "NAPCAT_ALLOW_GROUP_MESSAGES=false"
            )
        return True
    if mode != "auto":
        raise ValueError("WORLD_V2_QQ_C2C_MODE must be one of auto/v2/archive")
    return compatible


def create_app(
    *,
    adapter: str = "napcat",
    use_fake_model: bool = False,
    world_v2_c2c: bool | None = None,
    media_preview: MediaPreviewDeployment | None = None,
    media_transport: MediaProviderTransport | None = None,
) -> FastAPI:
    settings = get_settings()
    validate_qq_outbound_configuration(
        configured_adapter=settings.qq_adapter,
        launched_adapter=adapter,
    )
    if adapter not in {"napcat", "onebot"}:
        raise ValueError(f"unsupported OneBot adapter: {adapter}")
    if world_v2_c2c is None:
        # Programmatic and CLI construction share one authority-selection
        # contract.  A compatible private, single-recipient deployment is V2
        # by default; anything else must fail explicitly below.
        world_v2_c2c = resolve_cli_world_v2_c2c_selection(
            settings=settings,
            requested=None,
        )
    if world_v2_c2c:
        from companion_daemon.world_v2.qq_c2c_onebot_app import create_qq_c2c_onebot_app

        return create_qq_c2c_onebot_app(
            adapter=adapter,
            settings=settings,
            use_fake_model=use_fake_model,
            scheduler_interval_seconds=settings.qq_c2c_scheduler_interval_seconds,
            media_preview=media_preview,
            media_transport=media_transport,
        )
    if media_preview is not None or media_transport is not None:
        raise ValueError("World v2 media deployment requires the World v2 QQ C2C entry")
    raise RuntimeError(
        "the archived QQ Engine/coalescer lane was removed; this deployment "
        "selected archive mode (unsupported shape, --archive-qq, or "
        "WORLD_V2_QQ_C2C_MODE=archive) but only the World v2 C2C lane exists"
    )


def _parse_id_list(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def main() -> None:
    _run_cli(default_adapter="napcat")


def onebot_main() -> None:
    _run_cli(default_adapter="onebot")


def _run_cli(*, default_adapter: str) -> None:
    parser = argparse.ArgumentParser(description="Run a OneBot v11 companion adapter.")
    parser.add_argument("--adapter", choices=("napcat", "onebot"), default=default_adapter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--fake", action="store_true", help="Use the local fake model.")
    parser.add_argument(
        "--world-v2-c2c",
        action="store_const",
        const=True,
        default=None,
        dest="world_v2_c2c",
        help="Force the World v2 C2C text-only lane (no groups/media/stickers).",
    )
    parser.add_argument(
        "--archive-qq",
        action="store_const",
        const=False,
        dest="world_v2_c2c",
        help="Removed lane; selecting it fails fast with a removal error.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    lock_path = qq_outbound_owner_lock_path(settings.database_path)
    try:
        with QQOutboundOwnerLease(lock_path, adapter=args.adapter):
            uvicorn.run(
                create_app(
                    adapter=args.adapter,
                    use_fake_model=args.fake,
                    world_v2_c2c=resolve_cli_world_v2_c2c_selection(
                        settings=settings, requested=args.world_v2_c2c
                    ),
                ),
                host=args.host,
                port=args.port,
            )
    except AlreadyRunningError as exc:
        raise SystemExit(f"QQ outbound adapter cannot start: {exc}") from exc


if __name__ == "__main__":
    main()
