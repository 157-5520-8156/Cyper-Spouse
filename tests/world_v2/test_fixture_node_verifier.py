from __future__ import annotations

import importlib.util
from pathlib import Path

from companion_daemon.world_v2.fixture_acceptance_manifest import (
    FIXTURE_ACCEPTANCE_MANIFEST,
)


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify_world_v2_fixture_nodes.py"


def _module():
    spec = importlib.util.spec_from_file_location("verify_world_v2_fixture_nodes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verifier_executes_every_unique_manifest_node_in_stable_order() -> None:
    module = _module()
    expected = tuple(
        dict.fromkeys(
            node
            for fixture in FIXTURE_ACCEPTANCE_MANIFEST
            for node in fixture.test_nodes
        )
    )

    assert module.manifest_test_nodes() == expected
    assert len(expected) >= len(FIXTURE_ACCEPTANCE_MANIFEST)


def test_verifier_command_runs_pytest_nodes_instead_of_only_scanning_sources() -> None:
    module = _module()
    nodes = module.manifest_test_nodes()

    command = module.build_command(
        python="/python",
        collect_only=False,
        maxfail=3,
    )

    assert command[:3] == ("/python", "-m", "pytest")
    assert command[3 : 3 + len(nodes)] == nodes
    assert command[-2:] == ("-q", "--maxfail=3")
