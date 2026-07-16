from __future__ import annotations

import pytest

from companion_daemon.world_v2.external_capability_catalog import (
    EXTERNAL_CAPABILITIES,
    assert_external_capability_catalog_coverage,
    external_capability,
    production_expression_capabilities,
)
from companion_daemon.world_v2.production_proposal_grammar import (
    production_proposal_grammar,
)


def test_remaining_capability_verticals_are_explicit_and_fail_closed() -> None:
    assert_external_capability_catalog_coverage()
    assert production_expression_capabilities() == {"reply", "followup", "proactive_message"}

    for action_kind in ("reaction", "typing", "sticker"):
        capability = external_capability(action_kind)
        assert capability.availability == "adapter_only"
        assert "concrete_transport" in capability.missing_closure

    for action_kind in ("vision", "transcription"):
        capability = external_capability(action_kind)
        assert capability.availability == "adapter_only"
        assert "enforcement_authorization" in capability.installed_closure
        assert "default_provider_composition" in capability.missing_closure
    capability = external_capability("creative_media_request")
    assert capability.availability == "planned"
    assert "source_bound_request" in capability.missing_closure
    assert external_capability("read_only_tool").availability == "adapter_only"
    assert (
        "production_request_deliberation" in external_capability("read_only_tool").missing_closure
    )
    assert "enforcement_authorization" in external_capability("read_only_tool").installed_closure


def test_catalogue_has_no_implicit_or_duplicate_external_capabilities() -> None:
    assert len({item.capability_id for item in EXTERNAL_CAPABILITIES}) == len(EXTERNAL_CAPABILITIES)
    assert len({item.action_kind for item in EXTERNAL_CAPABILITIES}) == len(EXTERNAL_CAPABILITIES)
    with pytest.raises(ValueError, match="unknown World v2 external capability"):
        external_capability("file_write")


def test_production_chat_grammar_only_exposes_catalogued_production_expression_actions() -> None:
    grammar = production_proposal_grammar("chat_reply")
    action_kinds = frozenset().union(
        *(
            capability.action_kinds
            for capability in grammar.capabilities
            if capability.allows_actions
        )
    )
    assert action_kinds == production_expression_capabilities()
