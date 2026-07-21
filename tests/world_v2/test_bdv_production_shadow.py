"""Production-ledger-copy shadow proof (owner decision 1, 2026-07-20).

Gated on ``WORLD_V2_BDV_PRODUCTION_COPY`` pointing at a *copy* of
``data/companion.sqlite`` (the live ledger stays read-only and untouched;
everything runs locally).  The proof:

1. copies the provided snapshot twice (one per implementation edition),
2. opens each copy with the real ``SQLiteWorldLedger`` — whose cold-start
   verifier stream-replays and re-validates the entire immutable production
   history (every event, commit, revision and the persisted head) —
3. drives the same scripted conversation (deterministic fixture models, same
   logical clock) through the hand-written pilots on one copy and the
   framework editions on the other, appended on top of the real production
   world state, and
4. compares the appended ledger tails byte-for-byte straight from the
   persisted SQLite envelopes: event ids, idempotency keys, event JSON bytes,
   event hashes, per-commit request hashes and the final head
   ``semantic_hash``.

This module is skipped when no copy is provided: opening a multi-hundred-MB
snapshot twice takes minutes and belongs to the acceptance run, not to the
default suite.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.afterthought_author import (
    AfterthoughtAuthorRuntime,
    AfterthoughtPolicy as HandAfterthoughtPolicy,
)
from companion_daemon.world_v2.afterthought_author_vertical import (
    AfterthoughtPolicy as FrameworkAfterthoughtPolicy,
    AfterthoughtVerticalRuntime,
)
from companion_daemon.world_v2.context_capsule import ContextCapsuleCompiler
from companion_daemon.world_v2.deliberation import Deliberation
from companion_daemon.world_v2.expression_draft import QQ_NAPCAT_EXPRESSION_CAPABILITIES
from companion_daemon.world_v2.expression_plan_acceptance import ExpressionPlanBudgetPolicy
from companion_daemon.world_v2.expression_plan_atomic_recorder import (
    ExpressionPlanAtomicRecorder,
)
from companion_daemon.world_v2.ledger_context_resolver import (
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.minimal_reply_acceptance import ReplyBudgetPolicy
from companion_daemon.world_v2.minimal_reply_atomic_recorder import (
    MinimalReplyAtomicRecorder,
)
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.quick_reaction import (
    QuickReactionPolicy as HandQuickReactionPolicy,
    QuickReactionWorker,
)
from companion_daemon.world_v2.quick_reaction_vertical import (
    QuickReactionPolicy as FrameworkQuickReactionPolicy,
    QuickReactionVerticalWorker,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import ClockObservation, Observation
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger

from bdv_shadow_support import (
    AcceptingExecutor,
    NoRecoveryModel,
    ScriptedAfterthoughtGateModel,
    ScriptedQuickGateModel,
    ScriptedReplyModel,
    ScriptedRouter,
)


_COPY = os.environ.get("WORLD_V2_BDV_PRODUCTION_COPY", "").strip()
_WORLD_ID = os.environ.get(
    "WORLD_V2_BDV_PRODUCTION_WORLD", "world:companion-v2:qq-c2c:geoff"
)

pytestmark = pytest.mark.skipif(
    not _COPY or not Path(_COPY).exists(),
    reason="set WORLD_V2_BDV_PRODUCTION_COPY to a copy of the production ledger",
)


def _tail_rows(path: Path, *, world_id: str, since: int):
    connection = sqlite3.connect(path)
    try:
        events = connection.execute(
            "SELECT ledger_sequence, commit_id, event_id, idempotency_key, "
            "event_json, event_hash FROM world_v2_events "
            "WHERE world_id = ? AND ledger_sequence > ? ORDER BY ledger_sequence",
            (world_id, since),
        ).fetchall()
        commit_ids = {row[1] for row in events}
        commits = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT commit_id, request_hash FROM world_v2_commits WHERE world_id = ?",
                (world_id,),
            )
            if row[0] in commit_ids
        }
        head = connection.execute(
            "SELECT world_revision, deliberation_revision, ledger_sequence, "
            "semantic_hash FROM world_v2_heads WHERE world_id = ?",
            (world_id,),
        ).fetchone()
        return events, commits, head
    finally:
        connection.close()


class _ProductionShadowSide:
    def __init__(self, *, edition: str, path: Path) -> None:
        self.edition = edition
        self.path = path
        self.issuer = AcceptedLedgerBatchIssuer()
        self.ledger = SQLiteWorldLedger(
            path=path, world_id=_WORLD_ID, accepted_batch_issuer=self.issuer
        )
        projection = self.ledger.project()
        assert projection.logical_time is not None
        self.clock = projection.logical_time
        self.fork_sequence = projection.ledger_sequence
        self.fork_semantic_hash = projection.semantic_hash
        reply_action = max(
            (action for action in projection.actions if action.kind == "reply"),
            key=lambda action: action.logical_time,
        )
        self.target = reply_action.target
        self.companion = reply_action.actor
        self.counterpart = max(
            (item for item in projection.message_observations if item.actor != self.companion),
            key=lambda item: item.world_revision,
        ).actor
        chat_policy = ExpressionPlanBudgetPolicy(
            account_id="account:world-v2:chat",
            amount_limit_per_action=2,
            actor=self.companion,
            allowed_targets=(self.target,),
            recovery_policy="effect_once",
        )
        proactive_policy = ExpressionPlanBudgetPolicy(
            account_id="account:world-v2:proactive",
            amount_limit_per_action=2,
            actor=self.companion,
            allowed_targets=(self.target,),
            recovery_policy="effect_once",
            category="proactive",
        )
        recorder = ExpressionPlanAtomicRecorder(batch_issuer=self.issuer)
        # Receipts must carry the world's logical clock, exactly like a live
        # provider settling inside the current moment; otherwise the
        # afterthought window arithmetic never opens on this world.
        self.executor = AcceptingExecutor(now=self.clock)
        quick_gate = ScriptedQuickGateModel(behaviour="always_react")
        afterthought_gate = ScriptedAfterthoughtGateModel(behaviour="author")
        if edition == "hand":
            quick_worker = QuickReactionWorker(
                ledger=self.ledger,
                model=quick_gate,
                capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
                expression_policy=chat_policy,
                expression_recorder=recorder,
                executor=self.executor,
                pump_owner="pump:bdv-shadow:quick-reaction",
                policy=HandQuickReactionPolicy(base_act_bp=8_000),
                actor=self.companion,
            )
            afterthought = AfterthoughtAuthorRuntime(
                ledger=self.ledger,
                model=afterthought_gate,
                policy=proactive_policy,
                batch_issuer=self.issuer,
                owner_id="worker:bdv-shadow:afterthought",
                target=self.target,
                companion_actor_ref=self.companion,
                counterpart_actor_ref=self.counterpart,
                chronology=LocalChronology("Asia/Shanghai"),
                afterthought_policy=HandAfterthoughtPolicy(base_act_bp=4_400),
            )
        else:
            quick_worker = QuickReactionVerticalWorker(
                ledger=self.ledger,
                model=quick_gate,
                capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
                expression_policy=chat_policy,
                expression_recorder=recorder,
                executor=self.executor,
                pump_owner="pump:bdv-shadow:quick-reaction",
                policy=FrameworkQuickReactionPolicy(base_act_bp=8_000),
                actor=self.companion,
            )
            afterthought = AfterthoughtVerticalRuntime(
                ledger=self.ledger,
                model=afterthought_gate,
                policy=proactive_policy,
                batch_issuer=self.issuer,
                owner_id="worker:bdv-shadow:afterthought",
                target=self.target,
                companion_actor_ref=self.companion,
                counterpart_actor_ref=self.counterpart,
                chronology=LocalChronology("Asia/Shanghai"),
                afterthought_policy=FrameworkAfterthoughtPolicy(base_act_bp=4_400),
            )
        capsules: ContextCapsuleCompiler = context_capsule_compiler_from_ledger(
            ledger=self.ledger
        )
        self.runtime = WorldRuntime(
            world_id=_WORLD_ID,
            ledger=self.ledger,
            pinned_turn=PinnedTurnCompiler(
                ledger=self.ledger,
                capsule_compiler=capsules,
                deliberation=Deliberation(
                    router=ScriptedRouter(),
                    main_model=ScriptedReplyModel(),
                    quick_recovery=NoRecoveryModel(),
                ),
                companion_actor_ref=self.companion,
            ),
            reply_policy=ReplyBudgetPolicy(
                account_id="account:world-v2:chat",
                amount_limit=2,
                actor=self.companion,
                target=self.target,
                recovery_policy="effect_once",
            ),
            reply_recorder=MinimalReplyAtomicRecorder(batch_issuer=self.issuer),
            expression_policy=chat_policy,
            expression_recorder=recorder,
            action_executor=self.executor,
            action_pump_owner="pump:bdv-shadow",
            quick_reaction_worker=quick_worker,
            afterthought_author=afterthought,
        )
        self.tick_serial = 0
        self.statuses: list[str] = []

    def observation(self, *, suffix: str, text: str) -> Observation:
        return Observation(
            schema_version="world-v2.1",
            observation_id=f"observation:bdv-shadow:{suffix}",
            world_id=_WORLD_ID,
            logical_time=self.clock,
            created_at=self.clock,
            trace_id=f"trace:bdv-shadow:{suffix}",
            causation_id=f"inbound:bdv-shadow:{suffix}",
            correlation_id="conversation:bdv-shadow",
            source="bdv-shadow",
            source_event_id=f"message:bdv-shadow:{suffix}",
            actor=self.counterpart,
            channel="bdv-shadow",
            payload_ref=f"payload:bdv-shadow:{suffix}",
            payload_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            text=text,
            received_at=self.clock,
            reply_context={
                "target": self.target,
                "platform_message_id": f"bdv-shadow-{suffix}",
            },
        )

    async def advance(self, *, seconds: int) -> None:
        self.tick_serial += 1
        self.executor.now = self.clock + timedelta(seconds=seconds)
        new_time = self.clock + timedelta(seconds=seconds)
        outcome = await self.runtime.advance(
            ClockObservation(
                schema_version="world-v2.1",
                tick_id=f"tick:bdv-shadow:{self.tick_serial}",
                world_id=_WORLD_ID,
                logical_time=new_time,
                created_at=new_time,
                trace_id=f"trace:bdv-shadow:tick:{self.tick_serial}",
                causation_id="scheduler:bdv-shadow",
                correlation_id="conversation:bdv-shadow",
                logical_time_from=self.clock,
                logical_time_to=new_time,
                reason="bdv_shadow_window",
            )
        )
        assert outcome.status in {"advanced", "observed_only", "noop"}
        self.clock = new_time
        self.statuses.append(f"clock:{outcome.status}")

    async def drive(self) -> None:
        turns = (
            ("p1", "今天路过一家小店，突然想起你。"),
            ("p2", "刚才的晚霞好漂亮，想给你看。"),
        )
        # Move off the recorded head instant so the ingress clock is our own.
        await self.advance(seconds=30)
        for index, (suffix, text) in enumerate(turns):
            outcome = await self.runtime.ingest(self.observation(suffix=suffix, text=text))
            self.statuses.append(f"ingest[{suffix}]:{outcome.status}")
            pumped = await self.runtime.drain_actions_once()
            self.statuses.append(
                f"pump[{suffix}]:{pumped.status if pumped is not None else 'none'}"
            )
            await self.advance(seconds=20)
            for round_index in range(3):
                result = await self.runtime.drain_background_once()
                self.statuses.append(
                    f"background[{suffix}.{round_index}]:"
                    f"{result.status if result is not None else 'none'}"
                )
            if index == 0:
                # Let an authorized tail reach its due window and dispatch.
                await self.advance(seconds=240)
                pumped = await self.runtime.drain_actions_once()
                self.statuses.append(
                    f"due[{suffix}]:{pumped.status if pumped is not None else 'none'}"
                )

    def close(self) -> None:
        self.ledger.close()


@pytest.mark.asyncio
async def test_production_copy_shadow_replay_is_byte_identical(tmp_path) -> None:
    source = Path(_COPY)
    sides: dict[str, _ProductionShadowSide] = {}
    try:
        for edition in ("hand", "framework"):
            replica = tmp_path / f"{edition}.sqlite"
            shutil.copyfile(source, replica)
            side = _ProductionShadowSide(edition=edition, path=replica)
            sides[edition] = side
        forks = {side.fork_sequence for side in sides.values()}
        assert len(forks) == 1, "both replicas must fork at the same head"
        fork = forks.pop()
        assert (
            sides["hand"].fork_semantic_hash == sides["framework"].fork_semantic_hash
        ), "replicas disagree before any shadow input"

        for side in sides.values():
            await side.drive()

        assert sides["hand"].statuses == sides["framework"].statuses, (
            "run statuses diverged\n hand:      "
            f"{sides['hand'].statuses}\n framework: {sides['framework'].statuses}"
        )
    finally:
        for side in sides.values():
            side.close()
        await asyncio.sleep(0)

    hand_events, hand_commits, hand_head = _tail_rows(
        tmp_path / "hand.sqlite", world_id=_WORLD_ID, since=fork
    )
    framework_events, framework_commits, framework_head = _tail_rows(
        tmp_path / "framework.sqlite", world_id=_WORLD_ID, since=fork
    )
    assert len(hand_events) == len(framework_events) and hand_events, (
        f"tail lengths diverged: {len(hand_events)} vs {len(framework_events)}"
    )
    for left, right in zip(hand_events, framework_events, strict=True):
        assert left == right, f"tail event differs:\n hand:      {left}\n framework: {right}"
    assert hand_commits == framework_commits, "commit request hashes diverged"
    assert hand_head == framework_head, "final heads diverged"

    print(
        "production shadow ok: fork_sequence="
        f"{fork} tail_events={len(hand_events)} tail_commits={len(hand_commits)} "
        f"final_head={hand_head}"
    )
    print("production shadow lane trace:", sides["hand"].statuses)
