#!/usr/bin/env python3
"""Verify delayed-trigger static declarations; never claim host qualification."""

from __future__ import annotations

from pathlib import Path

import yaml

from companion_daemon.delayed_trigger_catalog import (
    load_delayed_trigger_catalog,
    verify_delayed_trigger_catalog,
)
from companion_daemon.world_v2.vertical_registry import VERTICAL_REGISTRY


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    catalog = load_delayed_trigger_catalog(
        root / "configs/delayed_trigger_qualification.v1.yaml"
    )
    closure = yaml.safe_load(
        (root / "configs/mechanism_closure.yaml").read_text(encoding="utf-8")
    )
    verify_delayed_trigger_catalog(
        catalog,
        vertical_registry=VERTICAL_REGISTRY,
        mechanism_rows=tuple(closure["mechanisms"]),
    )
    print(
        "static declarations verified: "
        f"{len(catalog.mechanisms)} delayed trigger mechanisms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
