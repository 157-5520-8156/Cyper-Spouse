#!/usr/bin/env python
"""Provision the World V2 public-information read capability.

The root signing seed is read only from ``WORLD_V2_ROOT_SIGNING_KEY_HEX``.
The command is idempotent and writes through the normal CAS ledger boundary.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion_daemon.world_v2.public_information_authority_provisioning import (  # noqa: E402
    PublicInformationAuthorityProvisioner,
)
from companion_daemon.world_v2.external_world_perception.registry import (  # noqa: E402
    load_external_perception_source_registry,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--world-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--operator", default="operator:girl-agent")
    args = parser.parse_args()

    signing_key = os.environ.get("WORLD_V2_ROOT_SIGNING_KEY_HEX", "").strip()
    if not signing_key:
        print("WORLD_V2_ROOT_SIGNING_KEY_HEX is required", file=sys.stderr)
        return 2
    ledger = SQLiteWorldLedger(path=Path(args.database), world_id=args.world_id)
    try:
        registry = load_external_perception_source_registry(Path(args.registry))
        result = PublicInformationAuthorityProvisioner(
            ledger=ledger,
            signing_key_hex=signing_key,
            companion_actor_ref=args.actor,
            registry_content_hash=registry.content_hash,
            operator_ref=args.operator,
        ).ensure()
    finally:
        ledger.close()
    for event_id in result.committed_event_ids:
        print(f"committed {event_id}")
    for entity in result.already_present:
        print(f"already present {entity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
