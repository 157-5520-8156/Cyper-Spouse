import json

from companion_daemon.world_v2.afterthought_author_vertical import (
    AfterthoughtPolicy,
    afterthought_gate_messages,
    parse_afterthought_verdict,
    timing_candidates,
)


def test_random_authority_selects_only_consideration_time() -> None:
    refs, weights = timing_candidates(AfterthoughtPolicy())

    assert refs
    assert set(weights) == set(refs)
    assert all(ref.startswith("delay:") for ref in refs)
    assert "act" not in refs
    assert "hold" not in refs


def test_role_model_owns_impulse_and_text() -> None:
    verdict = parse_afterthought_verdict(
        '{"afterthought":true,"impulse_summary":"忽然想分享自己的感受",'
        '"text":"其实我刚才还想到一件事。"}',
        max_chars=120,
    )

    assert verdict is not None
    assert verdict.impulse_summary == "忽然想分享自己的感受"
    assert verdict.text == "其实我刚才还想到一件事。"
    prompt = afterthought_gate_messages(
        policy=AfterthoughtPolicy(),
        identity_frame=None,
        reply_text="刚刚那部电影我也喜欢。",
        dialogue=(),
        local_time_label="2026-07-26T20:00:00+08:00",
        situation={"relationship": [{"stage": "friend"}], "affect": [{"dimension": "joy"}]},
    )
    assert "自行判断" in prompt[0]["content"]
    assert "act" not in prompt[0]["content"]
    assert "hold" not in prompt[0]["content"]
    supplied = json.loads(prompt[1]["content"])
    assert supplied["current_situation"]["relationship"] == [{"stage": "friend"}]
    assert supplied["current_situation"]["affect"] == [{"dimension": "joy"}]


def test_role_model_may_decline_without_local_hold() -> None:
    assert (
        parse_afterthought_verdict(
            '{"afterthought":false}',
            max_chars=120,
        )
        is None
    )
