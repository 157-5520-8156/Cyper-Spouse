"""External-world signal acquisition without World-event authority.

This package is deliberately separate from ``world_v2.perception_*``, which
describes provider analysis of user-supplied media.  Phase 1 stores only
source-reported External Signals in a disposable sidecar; it cannot write the
World ledger or decide that the companion noticed anything.
"""

from .contracts import (
    ExternalSignalEmbedding,
    ExternalSignalPlace,
    ExternalSignalSourceFailure,
    ExternalSignalSourceItem,
    ExternalSignalSourcePage,
    PerceptionAdvanceResult,
    PerceptionHealthSnapshot,
    RecordedSignalSourceAdapter,
    SourceCursor,
    SourceHealthSnapshot,
    SourcePolicyRevision,
    SourceProfile,
    WorldPerceptionHub,
)
from .hub import SQLiteWorldPerceptionHub
from .rss import RssAtomSourceAdapter, RssHubPullAdapter

__all__ = [
    "ExternalSignalSourceFailure",
    "ExternalSignalEmbedding",
    "ExternalSignalPlace",
    "ExternalSignalSourceItem",
    "ExternalSignalSourcePage",
    "PerceptionAdvanceResult",
    "PerceptionHealthSnapshot",
    "RecordedSignalSourceAdapter",
    "RssAtomSourceAdapter",
    "RssHubPullAdapter",
    "SQLiteWorldPerceptionHub",
    "SourceCursor",
    "SourceHealthSnapshot",
    "SourcePolicyRevision",
    "SourceProfile",
    "WorldPerceptionHub",
]
