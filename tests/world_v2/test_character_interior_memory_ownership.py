from __future__ import annotations

import inspect

from companion_daemon.world_v2.character_interior.production import (
    compose_fixture_character_interior,
)
from companion_daemon.world_v2.production_turn_application import (
    build_sqlite_world_v2_turn_application,
)


class _CharacterAuthor:
    model = "fixture-single-character-author"

    async def complete(self, messages, *, temperature=0.2) -> str:
        del messages, temperature
        return '{"status":"silent","summary":"no private action now"}'


def test_memory_authors_cannot_be_extracted_as_runtime_faculties() -> None:
    author = _CharacterAuthor()
    interior = compose_fixture_character_interior(model=author)

    assert not {
        "fact_memory_author",
        "experience_memory_author",
        "memory_withdrawal_reviewer",
    } & set(interior.runtime_health()["faculty_names"])


def test_production_application_has_no_independent_memory_model_seam() -> None:
    parameters = inspect.signature(build_sqlite_world_v2_turn_application).parameters

    assert "memory_model" not in parameters
