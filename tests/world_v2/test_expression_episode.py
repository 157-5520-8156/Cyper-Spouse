"""Guards for the retired provisional/full expression coordinator."""

import pytest

import companion_daemon.world_v2 as world_v2
from companion_daemon.world_v2 import expression_episode


def test_two_author_expression_episode_has_no_callable_public_harness() -> None:
    assert not hasattr(world_v2, "ExpressionEpisode")
    assert not hasattr(expression_episode, "ExpressionEpisode")
    assert not hasattr(expression_episode, "EpisodePolicy")


def test_expression_episode_diagnostics_rejects_retired_on_mode() -> None:
    with pytest.raises(
        ValueError,
        match="expression episode mode must be off, shadow, or stream",
    ):
        expression_episode.ExpressionEpisodeDiagnostics(mode="on")  # type: ignore[arg-type]
