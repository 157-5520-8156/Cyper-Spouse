#!/usr/bin/env bash
set -euo pipefail

DOCS_DIR="$HOME/Library/Containers/com.tencent.qq/Data/Documents"
LOAD_NAPCAT="$DOCS_DIR/loadNapCat.js"
BASE_PACKAGE="/Applications/QQ.app/Contents/Resources/app/package.json"
VERSIONS_DIR="$HOME/Library/Containers/com.tencent.qq/Data/Library/Application Support/QQ/versions"
CONFIG_JSON="$VERSIONS_DIR/config.json"

if [[ ! -f "$LOAD_NAPCAT" ]]; then
  echo "NapCat loader not found: $LOAD_NAPCAT" >&2
  echo "Run NapCatInstaller first, or grant it App Management / Full Disk Access and install again." >&2
  exit 1
fi

if ! touch "$DOCS_DIR/.girl-agent-write-test" 2>/dev/null; then
  echo "Cannot write QQ container: $DOCS_DIR" >&2
  echo "Grant Full Disk Access to the terminal/Codex app running this script, then run it again." >&2
  exit 1
fi
rm -f "$DOCS_DIR/.girl-agent-write-test"

python3 - "$BASE_PACKAGE" "$CONFIG_JSON" "$VERSIONS_DIR" "$LOAD_NAPCAT" <<'PY'
import json
import os
import shutil
import sys
from pathlib import Path

base_package = Path(sys.argv[1])
config_json = Path(sys.argv[2])
versions_dir = Path(sys.argv[3])
load_napcat = Path(sys.argv[4])

targets = [base_package]

if config_json.exists():
    with config_json.open("r", encoding="utf-8") as fh:
        config = json.load(fh)
    current = config.get("curVersion") or config.get("currentVersion")
    if current:
        targets.append(
            versions_dir
            / current
            / "QQUpdate.app"
            / "Contents"
            / "Resources"
            / "app"
            / "package.json"
        )

updated = []
for target in targets:
    if not target.exists():
        print(f"skip missing: {target}")
        continue

    with target.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    main_value = Path(os.path.relpath(load_napcat, target.parent)).as_posix()
    if data.get("main") == main_value:
        print(f"already patched: {target}")
        continue

    backup = target.with_name(target.name + ".bak.napcat")
    if not backup.exists():
        shutil.copy2(target, backup)

    data["main"] = main_value
    with target.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    updated.append(str(target))
    print(f"patched: {target}")

if not updated:
    print("No package.json files needed changes.")
PY

echo "Done. Start QQ with: /Applications/QQ.app/Contents/MacOS/QQ --no-sandbox"
