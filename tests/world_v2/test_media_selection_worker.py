from __future__ import annotations

import pytest
from types import SimpleNamespace
from datetime import UTC, datetime

from companion_daemon.world_v2.media_selection_draft import MediaSelectionDraftAdapter
from companion_daemon.world_v2.media_selection_worker import MediaSelectionWorker

NOW = datetime(2026, 7, 16, tzinfo=UTC)

class _Model:
    model = "test"
    async def complete(self, _messages, *, temperature=0.2):  # type: ignore[no-untyped-def]
        return '{"decision":"no_op"}'

class _Ledger:
    def project(self):
        return SimpleNamespace(logical_time=NOW, photo_candidates=())

class _Recorder: pass

@pytest.mark.asyncio
async def test_worker_does_not_call_the_model_or_write_when_no_candidate_exists() -> None:
    worker = MediaSelectionWorker(
        ledger=_Ledger(), draft_adapter=MediaSelectionDraftAdapter(model=_Model()),
        proposal_recorder=_Recorder(), catalog_version="test.1",
    )
    result = await worker.select_once(logical_time=NOW, actor="worker", trace_id="trace", correlation_id="correlation")
    assert result.status == "no_op"
    assert result.reason_code == "media_selection.no_available_candidates"
