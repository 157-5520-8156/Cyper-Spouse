from pathlib import Path

from companion_daemon.world_v2.external_world_perception.registry import (
    load_external_perception_source_registry,
)


_REGISTRY = Path("configs/external-perception-sources.cn.json")


def test_domestic_registry_registers_the_reviewed_social_trend_channels() -> None:
    registry = load_external_perception_source_registry(_REGISTRY)
    registered = {source.source_id: source for source in registry.sources}

    expected_routes = {
        "cn.social.weibo.trends.tophub.v1": "/tophub/KqndgxeLl9",
        "cn.social.douyin.trends.tophub.v1": "/tophub/DpQvNABoNE",
        "cn.social.kuaishou.trends.tophub.v1": "/tophub/MZd7PrPerO",
        "cn.social.wechat.trends.tophub.v1": "/tophub/WnBe01o371",
        "cn.social.toutiao.trends.tophub.v1": "/tophub/x9ozB4KoXb",
        "cn.social.tieba.trends.tophub.v1": "/tophub/Om4ejxvxEN",
        "cn.social.bilibili.popular.v1": "/bilibili/popular/all/1",
        "cn.social.zhihu.hot.v1": "/zhihu/hot",
        "cn.social.baidu.hot.v1": "/baidu/top",
        "cn.social.coolapk.hot.v1": "/coolapk/hot",
    }

    assert {source_id: registered[source_id].route for source_id in expected_routes} == (
        expected_routes
    )


def test_domestic_social_channels_are_bounded_weak_observations() -> None:
    registry = load_external_perception_source_registry(_REGISTRY)
    social = [source for source in registry.sources if source.source_id.startswith("cn.social.")]

    assert len(social) == 10
    assert all(source.adapter_kind == "rsshub" for source in social)
    assert all(not source.enabled for source in social)
    assert all(source.endpoint == "http://127.0.0.1:1200" for source in social)
    assert all(source.signal_kind == "platform_trend_observation" for source in social)
    assert all(source.allow_undated_items for source in social)
    assert all(source.page_limit <= 20 for source in social)
    assert all(source.poll_interval_seconds >= 300 for source in social)
    assert all(source.signal_ttl_seconds <= 3_600 for source in social)
    assert all(source.raw_retention_seconds <= 600 for source in social)
    assert all(not source.policy.may_fetch for source in social)
    assert all(not source.policy.may_cache_raw for source in social)
    assert all(not source.policy.may_expose_to_character_model for source in social)
    assert all(not source.policy.may_freeze_durable_snapshot for source in social)
    assert all(not source.policy.may_store_normalized_summary for source in social)
    assert all(not source.policy.may_embed for source in social)
    assert all(not source.policy.may_quote for source in social)
