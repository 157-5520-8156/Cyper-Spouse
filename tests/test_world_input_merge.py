from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.models import IncomingMessage
from companion_daemon.turn_taking import TurnInput, TurnTakingPolicy
from companion_daemon.world import WorldKernel


def test_pending_input_merge_is_auditable_and_recovers_after_engine_restart(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store, FakeCompanionModel(), "你是知栀。", world_kernel=world, world_id=world_id
    )
    policy = TurnTakingPolicy(short_wait_seconds=2, long_wait_seconds=5)
    first = IncomingMessage(
        platform="qq", platform_user_id="geoff", message_id="merge-1", text="我跟你说"
    )
    second = IncomingMessage(
        platform="qq", platform_user_id="geoff", message_id="merge-2", text="今天有件事，"
    )
    first_decision = policy.decide(TurnInput(1, first.text, first.text))
    engine.record_input_merge_candidate("c2c:geoff", first, first_decision, pending_count=1)
    second_decision = policy.decide(
        TurnInput(2, second.text, f"{first.text}\n{second.text}")
    )
    engine.record_input_merge_candidate("c2c:geoff", second, second_decision, pending_count=2)

    restarted = CompanionEngine(
        store,
        FakeCompanionModel(),
        "你是知栀。",
        world_kernel=WorldKernel(store),
        world_id=world_id,
    )
    recovered = restarted.recover_input_merge("c2c:geoff")

    assert [message.message_id for message in recovered] == ["merge-1", "merge-2"]
    projection = restarted.world_kernel.snapshot(world_id)["input_merges"]["c2c:geoff"]
    assert projection["status"] == "pending"
    assert projection["pending_count"] == 2
    assert projection["max_batch"] == 6
    assert projection["reason"] == second_decision.reason

    restarted.settle_input_merge("c2c:geoff", recovered)

    assert restarted.recover_input_merge("c2c:geoff") == ()
    assert restarted.world_kernel.snapshot(world_id)["input_merges"]["c2c:geoff"][
        "terminal_state"
    ] == "settled"
    assert restarted.world_kernel.rebuild_projection(
        world_id, "world_current_state"
    ).matches_live is True


def test_new_local_input_batch_replaces_stale_pending_merge(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store, FakeCompanionModel(), "你是知栀。", world_kernel=world, world_id=world_id
    )
    policy = TurnTakingPolicy(short_wait_seconds=2, long_wait_seconds=5)
    old = IncomingMessage(
        platform="qq", platform_user_id="geoff", message_id="old-pending", text="旧消息"
    )
    fresh = IncomingMessage(
        platform="qq", platform_user_id="geoff", message_id="fresh-first", text="今天重新聊"
    )

    engine.record_input_merge_candidate(
        "c2c:geoff",
        old,
        policy.decide(TurnInput(1, old.text, old.text)),
        pending_count=1,
    )
    engine.record_input_merge_candidate(
        "c2c:geoff",
        fresh,
        policy.decide(TurnInput(1, fresh.text, fresh.text)),
        pending_count=1,
    )

    projection = world.snapshot(world_id)["input_merges"]["c2c:geoff"]
    assert [message["message_id"] for message in projection["messages"]] == ["fresh-first"]
    engine.settle_input_merge("c2c:geoff", (fresh,))
    assert world.snapshot(world_id)["input_merges"]["c2c:geoff"]["status"] == "settled"


def test_repeated_input_merge_settlement_is_revision_stable(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    seed_user(store)
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    engine = CompanionEngine(
        store, FakeCompanionModel(), "你是知栀。", world_kernel=world, world_id=world_id
    )
    message = IncomingMessage(
        platform="qq", platform_user_id="geoff", message_id="merge-once", text="早"
    )
    policy = TurnTakingPolicy(short_wait_seconds=2, long_wait_seconds=5)
    engine.record_input_merge_candidate(
        "c2c:geoff",
        message,
        policy.decide(TurnInput(1, message.text, message.text)),
        pending_count=1,
    )
    engine.settle_input_merge("c2c:geoff", (message,))
    revision = world.revision(world_id)

    engine.settle_input_merge("c2c:geoff", (message,))

    assert world.revision(world_id) == revision
