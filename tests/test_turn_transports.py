import pytest

from companion_daemon.companion_turn import TurnBeat
from companion_daemon.turn_transports import CaptureTurnTransport


@pytest.mark.asyncio
async def test_capture_transport_records_receipted_beats_in_order() -> None:
    transport = CaptureTurnTransport(receipt_namespace="test")
    first = TurnBeat("outgoing:1", 1, "outgoing:1:segment:0", 0, "第一句。", "qq", "geoff")
    second = TurnBeat("outgoing:1", 1, "outgoing:1:segment:1", 1, "第二句。", "qq", "geoff")

    first_result = await transport.dispatch(first)
    second_result = await transport.dispatch(second)

    assert first_result.external_receipt == "test:outgoing:1:outgoing:1:segment:0"
    assert second_result.external_receipt == "test:outgoing:1:outgoing:1:segment:1"
    assert transport.text == "第一句。第二句。"
