import pytest

import companion_daemon.qq_sticker_cli as sticker_cli


@pytest.mark.asyncio
async def test_manual_sticker_cli_is_retired_without_loading_a_runtime(capsys) -> None:
    sent = await sticker_cli.run("geoff", "happy", sandbox=True)

    assert sent is False
    output = capsys.readouterr().out
    assert "not sent" in output
    assert "World authorization" in output


@pytest.mark.asyncio
async def test_manual_sticker_cli_is_retired_even_without_a_known_sticker(capsys) -> None:
    sent = await sticker_cli.run("geoff", "not-a-catalog-entry", sandbox=False)

    assert sent is False
    assert "retired" in capsys.readouterr().out
