from companion_daemon.world_v2.delayed_trigger_policies import (
    TECHNICAL_RETRY_BACKOFF_SECONDS,
)
from companion_daemon.world_v2.proactive_action import ProactiveActionRuntime
from companion_daemon.world_v2.external_world_perception.attention import (
    _FAILURE_BACKOFF_SECONDS as PERCEPTION_ATTENTION_BACKOFF_SECONDS,
)
from companion_daemon.world_v2.external_world_perception.hub import (
    _FAILURE_BACKOFF_SECONDS as PERCEPTION_HUB_BACKOFF_SECONDS,
)


def test_proactive_runtime_and_qualification_share_one_retry_policy_object() -> None:
    assert ProactiveActionRuntime.FAILURE_BACKOFF_SECONDS is TECHNICAL_RETRY_BACKOFF_SECONDS
    assert PERCEPTION_ATTENTION_BACKOFF_SECONDS is TECHNICAL_RETRY_BACKOFF_SECONDS
    assert PERCEPTION_HUB_BACKOFF_SECONDS is TECHNICAL_RETRY_BACKOFF_SECONDS
