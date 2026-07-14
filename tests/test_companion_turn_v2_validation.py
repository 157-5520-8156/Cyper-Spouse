import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_companion_turn_v2", ROOT / "scripts" / "validate_companion_turn_v2.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
build_commands = MODULE.build_commands


def test_validation_commands_keep_live_qq_gates_opt_in() -> None:
    commands = build_commands(
        python="python",
        observation_jsonl=Path("data/private/qq-turns.jsonl"),
        report_path=Path("var/evaluation/smoke.json"),
        include_live_qq_gates=False,
    )
    rendered = [" ".join(command) for command in commands]

    assert any("pytest" in command and "test_companion_turn.py" in command for command in rendered)
    assert any("dialogue_eval --baseline" in command for command in rendered)
    assert any("qq_latency_eval --synthetic" in command for command in rendered)
    assert any("qq_latency_eval --observation-jsonl" in command for command in rendered)
    assert not any("--assert-live-evidence" in command for command in rendered)
    assert not any("--assert-experience-evidence" in command for command in rendered)


def test_validation_commands_can_include_live_qq_evidence_gates() -> None:
    commands = build_commands(
        python="python",
        observation_jsonl=Path("data/private/qq-turns.jsonl"),
        report_path=Path("var/evaluation/smoke.json"),
        include_live_qq_gates=True,
    )
    rendered = [" ".join(command) for command in commands]

    assert any(
        "--assert-live-evidence" in command
        and "--assert-experience-evidence" in command
        for command in rendered
    )
