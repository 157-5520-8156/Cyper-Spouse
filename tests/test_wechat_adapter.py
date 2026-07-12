import pytest
from pydantic import ValidationError

from companion_daemon.config import Settings
from companion_daemon.platform_adapter import (
    ACTION_TERMINAL_STATES,
    OutboundEnvelope,
    OutboundPlatformAdapter,
)
from companion_daemon.wechat_adapter import FakeWeChatAdapter, build_wechat_adapter


def test_wechat_configuration_only_allows_disabled_or_fake() -> None:
    assert Settings().wechat_adapter == "disabled"
    assert Settings(WECHAT_ADAPTER="fake").wechat_adapter == "fake"
    with pytest.raises(ValidationError, match="WECHAT_ADAPTER"):
        Settings(WECHAT_ADAPTER="official")


def test_disabled_wechat_configuration_builds_no_adapter() -> None:
    assert build_wechat_adapter(Settings(WECHAT_ADAPTER="disabled")) is None


@pytest.mark.asyncio
async def test_fake_wechat_adapter_satisfies_contract_without_a_live_account() -> None:
    adapter = build_wechat_adapter(Settings(WECHAT_ADAPTER="fake"))

    assert isinstance(adapter, FakeWeChatAdapter)
    assert isinstance(adapter, OutboundPlatformAdapter)
    assert adapter.platform == "wechat"
    assert adapter.is_fake is True
    assert adapter.live_account_connected is False

    envelope = OutboundEnvelope(
        action_id="outgoing:42",
        recipient_id="geoff",
        kind="reply",
        text="测试微信 seam。",
    )
    first = await adapter.dispatch(envelope)
    duplicate = await adapter.dispatch(envelope)

    assert duplicate == first
    assert len(adapter.dispatched) == 1
    assert first.accepted is True
    assert first.platform_message_id == "fake-wechat:outgoing:42"
    assert first.receipt_query_token == "fake-wechat-receipt:outgoing:42"
    assert (await adapter.lookup_delivery(first.receipt_query_token)).status == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(ACTION_TERMINAL_STATES))
async def test_fake_wechat_receipt_exposes_every_action_terminal_state(status: str) -> None:
    adapter = FakeWeChatAdapter()
    envelope = OutboundEnvelope(
        action_id=f"outgoing:{status}",
        recipient_id="geoff",
        kind="reply",
        text="测试。",
    )
    accepted = await adapter.dispatch(envelope)

    adapter.settle(envelope.action_id, status=status, external_receipt=f"fake:{status}")
    receipt = await adapter.lookup_delivery(accepted.receipt_query_token)

    assert receipt.action_id == envelope.action_id
    assert receipt.status == status
    assert receipt.external_receipt == f"fake:{status}"
