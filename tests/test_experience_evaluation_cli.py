from __future__ import annotations

import json

from companion_daemon.experience_evaluation import ExperienceTurn, VariantRun
from companion_daemon.experience_evaluation_cli import main


def _run(variant_id: str, *, reply_prefix: str) -> VariantRun:
    return VariantRun(
        variant_id=variant_id,
        turns=tuple(
            ExperienceTurn(
                turn_id=f"turn-{index}",
                reply=f"{reply_prefix}-{index}",
                speech_act="respond_to_vulnerability",
                stance="care_despite_hurt",
                empathy=4,
                persona_continuity=5,
                grounding=5,
                agency=4,
                action_consequence="delivered",
                manual_review_note=f"人工复核 {index}",
                factual_invariants=("character:name=知栀", "user:city=上海"),
            )
            for index in range(5)
        ),
    )


def test_cli_records_two_five_turn_variants_then_writes_comparison_report(
    tmp_path, capsys
) -> None:
    ledger = tmp_path / "experience-runs.jsonl"
    for run in (_run("baseline", reply_prefix="相同"), _run("candidate", reply_prefix="变化")):
        source = tmp_path / f"{run.variant_id}.json"
        source.write_text(json.dumps(run.to_record(), ensure_ascii=False), encoding="utf-8")

        assert main(["record", str(source), "--ledger", str(ledger)]) == 0

    report_path = tmp_path / "report.json"
    assert main(["compare", str(ledger), "--report", str(report_path)]) == 0

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["warning"] == "diagnostics_do_not_replace_human_experience_review"
    assert set(report["variants"]) == {"baseline", "candidate"}
    assert report["variants"]["candidate"]["human_review_complete"] is True
    assert report["variants"]["candidate"]["human_like"] is None
    assert "candidate" in capsys.readouterr().out
