"""Compile the editable Zhizhi room into browser runtime assets."""

from pathlib import Path

from companion_daemon.room_compiler import compile_room


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "assets/dashboard/rooms/zhizhi-home/room.json"
OUTPUT = ROOT / "assets/dashboard/rooms/zhizhi-home/runtime"


def main() -> None:
    report = compile_room(MANIFEST, OUTPUT)
    print(f"compiled {MANIFEST.name} -> {report.bundle_path}")
    print(f"generated {len(report.generated_assets)} room assets")


if __name__ == "__main__":
    main()
