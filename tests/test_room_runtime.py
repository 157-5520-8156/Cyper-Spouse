from pathlib import Path
import subprocess


def test_room_runtime_javascript_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "--test", "tests/js/room_runtime.test.js"],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
