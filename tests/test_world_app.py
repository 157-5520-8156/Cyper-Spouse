from pathlib import Path

from fastapi.testclient import TestClient

import companion_daemon.app as app_module
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world import WorldKernel


def test_world_enablement_and_manual_delivery_reconciliation(tmp_path: Path, monkeypatch) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    kernel = WorldKernel(store)
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    engine = CompanionEngine(store, FakeCompanionModel(), "你是知栀。", world_kernel=kernel, world_id=started.world_id)
    monkeypatch.setattr(app_module, "engine", engine)
    client = TestClient(app_module.app)

    audit = client.get("/world-runtime/enablement")
    assert audit.status_code == 200
    assert audit.json()["enabled"] is True

    delivery_id, _, _ = kernel.queue_outgoing_action(
        canonical_user_id="geoff", platform="qq", text="测试。", kind="reply",
        expires_at=__import__("datetime").datetime.fromisoformat("2026-07-12T09:00:00+08:00"),
        trace={"world_id": started.world_id, "appraisal": "test", "expression_policy": "test", "allowed_facts": [], "short_lived_constraint": None, "observable_reason": "test"},
    )
    kernel.begin_outgoing_action(delivery_id, expected_revision=kernel.revision(started.world_id))
    kernel.mark_outgoing_unknown(delivery_id, reason="restart", expected_revision=kernel.revision(started.world_id))
    reconciled = client.post(f"/world/{started.world_id}/deliveries/reconcile", json={"delivery_id": delivery_id, "delivered": True, "external_receipt": "manual:qq-42"})
    assert reconciled.status_code == 200
    assert kernel.snapshot(started.world_id)["actions"][f"outgoing:{delivery_id}"]["status"] == "delivered"
