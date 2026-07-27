from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parents[2] / "scripts" / "run_memory_recall_eval.py"
_SPEC = importlib.util.spec_from_file_location("memory_recall_eval", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
assert_safe_eval_paths = _MODULE.assert_safe_eval_paths
_memory_slice_text = _MODULE._memory_slice_text
_score_negative_probe = _MODULE._score_negative_probe
_score_probe = _MODULE._score_probe


def test_eval_refuses_configured_production_database(tmp_path: Path) -> None:
    production = tmp_path / "production.sqlite"

    with pytest.raises(ValueError, match="configured production"):
        assert_safe_eval_paths(
            database=production,
            output=tmp_path / "results.jsonl",
            production_database=production,
        )


def test_eval_refuses_output_aliasing_scratch_database(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch.sqlite"

    with pytest.raises(ValueError, match="output must not overwrite"):
        assert_safe_eval_paths(
            database=scratch,
            output=scratch,
            production_database=tmp_path / "production.sqlite",
        )


def test_eval_refuses_existing_or_non_scratch_database(tmp_path: Path) -> None:
    existing = tmp_path / "existing.sqlite"
    existing.touch()

    with pytest.raises(ValueError, match="new scratch path"):
        assert_safe_eval_paths(
            database=existing,
            output=tmp_path / "results.jsonl",
            production_database=tmp_path / "production.sqlite",
        )
    with pytest.raises(ValueError, match="temporary root"):
        assert_safe_eval_paths(
            database=Path("/opt/not-a-scratch-ledger.sqlite"),
            output=tmp_path / "results.jsonl",
            production_database=tmp_path / "production.sqlite",
        )


def test_active_memory_candidate_counts_as_retrieval_not_dialogue_tail() -> None:
    model_context = {
        "slices": {
            "relevant_facts": {"items": []},
            "active_memory_candidates": {
                "items": [{"source_excerpts": [{"text": "最爱桂花乌龙"}]}]
            },
            "recent_dialogue": {"items": [{"text": "想喝点热的"}]},
        }
    }
    raw = json.dumps(model_context, ensure_ascii=False)
    memory = _memory_slice_text(raw)

    scored = _score_probe(
        {"expect_any": ["桂花乌龙"], "context_any": ["桂花乌龙"]},
        "那就泡桂花乌龙吧",
        raw,
        memory,
    )

    assert scored["verdict"] == "recalled"
    assert scored["in_memory"] is True


def test_negative_probe_separates_wrong_injection_from_visible_expression() -> None:
    scored = _score_negative_probe(
        {"forbid_any": ["林星", "美术社"]},
        "窗外有点阴。",
        "旧候选：林星最近高三。",
    )

    assert scored["wrong_memory_injected"] is True
    assert scored["unsupported_reply"] is False
    assert scored["pass"] is False
