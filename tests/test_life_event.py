from companion_daemon.life_event import parse_life_event


def test_parse_life_event_json() -> None:
    event = parse_life_event(
        '{"topic":"食堂","messages":["刚刚吃到一个好甜的南瓜。","我认真怀疑阿姨今天心情很好。"],"sticker_category":"happy"}'
    )

    assert event.topic == "食堂"
    assert len(event.messages) == 2
    assert event.sticker_category == "happy"
