"""Archive-only replay seam for pre-social-action reply-later fixtures.

Production composition must not import this module.  It exists solely to
replay old ledger fixtures while ``DeferredReplyRuntime`` retains settlement
compatibility for commitments already written by those versions.
"""

from __future__ import annotations

from .deferred_reply_runtime import (
    DeferredReplyRuntime,
    ReplyLaterCommand,
    _LEGACY_REPLY_LATER_AUTHORITY,
)


def replay_legacy_reply_later(runtime: DeferredReplyRuntime, command: ReplyLaterCommand, **kwargs):
    if type(runtime) is not DeferredReplyRuntime:
        raise TypeError("legacy reply-later replay requires the exact migration runtime")
    return runtime._defer_legacy(
        command, authority=_LEGACY_REPLY_LATER_AUTHORITY, **kwargs
    )


__all__ = ["replay_legacy_reply_later"]
