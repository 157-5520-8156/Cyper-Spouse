#!/usr/bin/env python3
"""Fail CI if a selected World v2 platform lane reaches legacy authority."""

from __future__ import annotations

from pathlib import Path

from companion_daemon.world_v2.platform_architecture_guard import assert_v2_platform_architecture


def main() -> None:
    assert_v2_platform_architecture(Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    main()
