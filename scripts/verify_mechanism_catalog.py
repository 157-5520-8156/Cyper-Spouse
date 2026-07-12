#!/usr/bin/env python3
"""CI entrypoint for the world mechanism closure catalog."""

from __future__ import annotations

import argparse
from pathlib import Path

from companion_daemon.mechanism_catalog import verify_mechanism_catalog


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify mechanism closure evidence.")
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("configs/mechanism_closure.yaml"),
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args()

    report = verify_mechanism_catalog(args.catalog, repo_root=args.repo_root)
    print(
        f"mechanism catalog verified: schema={report.schema_version} "
        f"mechanisms={report.mechanism_count}"
    )


if __name__ == "__main__":
    main()
