from __future__ import annotations

import pytest

from companion_daemon.world_v2.media_selection import MediaSelection


def test_public_life_share_selection_is_only_a_bounded_preview_choice() -> None:
    selection = MediaSelection(candidate_id="photo-candidate:1", family="life_share")
    assert selection.delivery_mode == "preview"
    assert selection.media_privacy_ceiling == "ordinary"


@pytest.mark.parametrize("values", [
    {"family": "life_share", "recipient_ref": "user:1", "private_expression_basis_ref": "basis:1"},
    {"family": "life_share", "expression_charge_ceiling": "subtle"},
    {"family": "character_media", "delivery_mode": "automatic"},
    {"family": "character_media", "recipient_ref": "user:1"},
])
def test_selection_rejects_authority_escalation_or_incomplete_private_binding(values) -> None:
    with pytest.raises(ValueError):
        MediaSelection(candidate_id="photo-candidate:1", **values)
