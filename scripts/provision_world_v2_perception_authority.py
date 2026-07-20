#!/usr/bin/env python
"""Provision the World v2 perception enforcement chain for one world.

Usage (run from the repository root, daemon may stay up — commits are CAS'd):

    WORLD_V2_ROOT_SIGNING_KEY_HEX=<ed25519 seed hex> \
    .venv/bin/python scripts/provision_world_v2_perception_authority.py \
        --database data/companion.sqlite \
        --world-id world:companion-v2:qq-c2c:geoff \
        --subject user:geoff

The signing key must correspond to a deployment root already pinned in
``actor_authority_events.ROOT_PUBLIC_KEYS``.  Using the committed test root
(``test-only:development-root-1``, seed ``11`` × 32) additionally requires
``WORLD_V2_ENABLE_INSECURE_TEST_ROOT=1`` in the environment of *both* this
script and every process that later replays the ledger.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion_daemon.world_v2.perception_authority_provisioning import (  # noqa: E402
    PerceptionAuthorityProvisioner,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="path to the world SQLite file")
    parser.add_argument("--world-id", required=True, help="e.g. world:companion-v2:qq-c2c:geoff")
    parser.add_argument("--subject", required=True, help="user consent principal, e.g. user:geoff")
    parser.add_argument("--companion-actor", default="agent:companion")
    parser.add_argument("--operator", default="operator:girl-agent")
    args = parser.parse_args()

    signing_key = os.environ.get("WORLD_V2_ROOT_SIGNING_KEY_HEX", "").strip()
    if not signing_key:
        print("WORLD_V2_ROOT_SIGNING_KEY_HEX is required", file=sys.stderr)
        return 2

    ledger = SQLiteWorldLedger(path=Path(args.database), world_id=args.world_id)
    try:
        result = PerceptionAuthorityProvisioner(
            ledger=ledger,
            signing_key_hex=signing_key,
            subject_ref=args.subject,
            companion_actor_ref=args.companion_actor,
            operator_ref=args.operator,
        ).ensure()
    finally:
        ledger.close()

    for event_id in result.committed_event_ids:
        print(f"committed {event_id}")
    for entity in result.already_present:
        print(f"already present {entity}")
    if not result.committed_event_ids and not result.already_present:
        print("nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
