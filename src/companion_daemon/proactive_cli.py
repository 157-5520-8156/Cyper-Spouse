import argparse
import asyncio

from companion_daemon.companion_turn import CompanionTurn, DispatchAcceptance, TurnBeat
from companion_daemon.config import get_settings
from companion_daemon.qq_delivery import QQDelivery
from companion_daemon.runtime import build_companion_engine
from companion_daemon.time import utc_now


# Keep the receipt extractor fixed to the production adapter.  Tests can
# replace QQDelivery without accidentally changing what counts as evidence.
_QQ_RECEIPT_CANDIDATE = QQDelivery.receipt_candidate


class _QQProactiveTurnTransport:
    """Transport adapter for an already-authorized World proactive Action."""

    def __init__(self, delivery: QQDelivery, recipient_id: str) -> None:
        self.delivery = delivery
        self.recipient_id = recipient_id

    async def dispatch(self, beat: TurnBeat) -> DispatchAcceptance:
        response = await self.delivery.send_text(self.recipient_id, beat.text)
        receipt = _QQ_RECEIPT_CANDIDATE(response)
        if receipt:
            return DispatchAcceptance(status="delivered", external_receipt=receipt)
        return DispatchAcceptance(
            status="unknown",
            reason="qq_proactive_send_returned_without_durable_receipt",
        )


async def _dispatch_world_proactive(
    engine,
    *,
    delivery: QQDelivery,
    recipient_id: str,
    decision,
):
    """Deliver a World proactive decision through its normal receipt seam."""
    if not decision.world_action_id or decision.delivery_id is None:
        raise ValueError("World proactive decision is missing its outgoing Action")
    return await CompanionTurn(
        engine,
        _QQProactiveTurnTransport(delivery, recipient_id),
    ).dispatch_scheduled(
        action_id=decision.world_action_id,
        delivery_id=decision.delivery_id,
        observed_at=utc_now(),
        idempotency_key=f"proactive-cli:{decision.world_action_id}",
    )


async def run(user_id: str, *, send: bool, sandbox: bool) -> None:
    engine = build_companion_engine()
    try:
        await _run_with_engine(engine, user_id=user_id, send=send, sandbox=sandbox)
    finally:
        close = getattr(engine, "aclose", None)
        if callable(close):
            await close()


async def _run_with_engine(engine, *, user_id: str, send: bool, sandbox: bool) -> None:
    decision = await engine.proactive_tick(user_id)
    print(f"private: {decision.private_thought}")
    print(f"should_send: {decision.should_send}")
    print(f"platform: {decision.platform}")
    print(f"message_type: {decision.message_type}")
    print(f"message: {decision.message or ''}")
    if decision.sticker_path:
        print(f"sticker: {decision.sticker_path}")
    if decision.image_path:
        print(f"image: {decision.image_path}")

    if not send:
        return
    if not decision.should_send or decision.platform != "qq":
        print("not sent: decision did not produce a QQ message")
        return

    if not (getattr(engine, "world_kernel", None) is not None and decision.world_action_id):
        # The legacy store has no unknown receipt state nor an immutable
        # Action to receive an ExternalObservation.  Its old CLI path treated
        # a successful send coroutine as user-visible delivery and immediately
        # mutated relationship/life state.  Retire that unsafe sender instead
        # of preserving an incompatible second confirmation protocol.
        print("not sent: legacy proactive delivery is retired; migrate to a World-backed engine")
        return

    settings = get_settings()
    delivery = QQDelivery(settings, sandbox=sandbox)
    recipient_id = delivery.proactive_recipient_id() or engine.store.platform_user_id(user_id, "qq")
    if not recipient_id:
        print("not sent: no outbound QQ recipient configured for this user")
        return
    # World media is an independent Action scheduled by the media worker.
    # Sending a decision.image_path here would create an untracked second side
    # effect, so this CLI is intentionally responsible only for the
    # authoritative text Action.
    outcome = await _dispatch_world_proactive(
        engine,
        delivery=delivery,
        recipient_id=recipient_id,
        decision=decision,
    )
    if outcome.terminal_state == "delivered":
        print("sent: QQ proactive wakeup message")
    else:
        print(f"proactive message not confirmed: {outcome.terminal_state or 'accepted'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one proactive companion decision.")
    parser.add_argument("--user", default="geoff", help="Canonical user id.")
    parser.add_argument("--send", action="store_true", help="Actually send if the decision allows it.")
    parser.add_argument("--sandbox", action="store_true", help="Use QQ sandbox API for sending.")
    args = parser.parse_args()
    asyncio.run(run(args.user, send=args.send, sandbox=args.sandbox))
