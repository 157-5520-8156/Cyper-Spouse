from pathlib import Path

import pytest

from legacy_story_seed import write_legacy_story_seed


@pytest.fixture
def legacy_story_seed_path(tmp_path: Path) -> Path:
    return write_legacy_story_seed(tmp_path / "legacy-story-world-seed.yaml")
