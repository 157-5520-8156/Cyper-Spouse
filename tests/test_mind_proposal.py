from companion_daemon.mind_proposal import parse_mind_proposal


def test_legacy_world_reply_json_remains_a_valid_mind_proposal() -> None:
    proposal = parse_mind_proposal(
        '{"reply_text":"我在听。","mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
    )

    assert proposal.candidate["reply_text"] == "我在听。"
    assert proposal.expression_beats == ()
    assert proposal.display_strategy is None


def test_mind_proposal_accepts_exact_bounded_expression_beats() -> None:
    proposal = parse_mind_proposal(
        '{"reply_text":"先骂两句。再慢慢说。",'
        '"expression_beats":[{"text":"先骂两句。","delay_ms":0},'
        '{"text":"再慢慢说。","delay_ms":1200}],'
        '"display_strategy":"陪伴后追问",'
        '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
    )

    assert [beat.text for beat in proposal.expression_beats] == ["先骂两句。", "再慢慢说。"]
    assert [beat.delay_ms for beat in proposal.expression_beats] == [0, 1200]
    assert proposal.display_strategy == "陪伴后追问"


def test_mind_proposal_discards_non_composing_or_delayed_first_beat() -> None:
    proposal = parse_mind_proposal(
        '{"reply_text":"我在听。",'
        '"expression_beats":[{"text":"我在听。","delay_ms":500}],'
        '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
    )

    assert proposal.expression_beats == ()


def test_mind_proposal_keeps_only_a_bounded_fallible_private_impression() -> None:
    proposal = parse_mind_proposal(
        '{"reply_text":"我听到了。",'
        '"private_impression":{"kind":"possible_disappointment",'
        '"summary":"我感觉他可能是被刚才的节奏伤到了。","confidence":0.7},'
        '"mentioned_event_ids":[],"proposed_action_ids":[],"claims":[]}'
    )

    assert proposal.private_impression is not None
    assert proposal.private_impression.kind == "possible_disappointment"
    assert proposal.private_impression.confidence == 0.7
