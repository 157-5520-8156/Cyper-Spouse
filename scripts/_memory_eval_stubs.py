"""Deterministic models for the memory recall eval's provider-free mode.

These are not a cheap imitation of the real run.  They answer a different and
sharper question.

``StubReplyModel`` surfaces an expected value if and only if that value is
actually present in the model-facing context it was handed. It never invents,
never forgets, and never declines. Semantic embedding is disabled in this
mode, so the score is specifically the lexical/structured retrieval ceiling.

A low provider-free ceiling means the exact/structured retrieval and budget
pipeline is dropping facts before the model ever sees them. Comparing it with
a real run then shows the combined contribution of semantic retrieval and the
role model; the two are not falsely collapsed into one metric.
"""

from __future__ import annotations

import json


def _semantic_context_text(raw: str) -> str:
    """Flatten model-visible meaning without matching opaque ids or refs."""

    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return ""
    strings: list[str] = []

    def visit(node: object, *, key: str = "") -> None:
        normalized = key.lower()
        if (
            normalized.endswith(("_id", "_ids", "_ref", "_refs", "_hash"))
            or normalized
            in {
                "actor",
                "actor_ref",
                "counterpart_actor_ref",
                "subject_ref",
                "trigger_ref",
                "world_id",
            }
        ):
            return
        if isinstance(node, str):
            strings.append(node)
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            for child_key, item in node.items():
                visit(item, key=str(child_key))

    visit(value)
    return "\n".join(strings)


def _plants(fixture: dict[str, object]) -> dict[str, dict[str, str]]:
    """Map a distinctive substring of each planting turn to its fact draft."""

    out: dict[str, dict[str, str]] = {}
    for turn in fixture.get("turns", []):  # type: ignore[union-attr]
        plant = turn.get("plant") if isinstance(turn, dict) else None
        if not isinstance(plant, dict):
            continue
        match = str(plant.get("stub_match", "")).strip()
        if not match:
            continue
        out[match] = {
            "predicate_code": str(plant["predicate_code"]),
            "value": str(plant["value"]),
        }
    return out


def _probes(fixture: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(turn["text"]): turn["probe"]
        for turn in fixture.get("turns", [])  # type: ignore[union-attr]
        if isinstance(turn, dict) and isinstance(turn.get("probe"), dict)
    }


def _expected_terms(probe: dict[str, object]) -> list[list[str]]:
    """Normalize both probe shapes into a list of alternative-groups."""

    groups = probe.get("expect_all_groups")
    if isinstance(groups, list) and groups:
        return [[str(option) for option in group] for group in groups if isinstance(group, list)]
    return [[str(value) for value in probe.get("expect_any", [])]]


class StubReplyModel:
    """Echo expected values that survived into the provider-free model context."""

    model = "fixture:memory-recall-reply"

    def __init__(self, fixture: dict[str, object]) -> None:
        self._probes = _probes(fixture)

    async def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        system = messages[0]["content"]
        envelope = json.loads(messages[1]["content"])
        request = envelope.get("request", {})
        trigger = request.get("trigger_message") or {}
        text = str(trigger.get("text", ""))
        context = _semantic_context_text(str(request.get("model_content_json", "")))

        probe = self._probes.get(text)
        if probe is None:
            reply = "嗯，我在听。"
        else:
            surfaced: list[str] = []
            for group in _expected_terms(probe):
                # Say a remembered value only when it genuinely reached this
                # turn's context.  This is what makes the run a ceiling rather
                # than a fixture that always passes.
                present = [option for option in group if option and option in context]
                if present:
                    surfaced.append(present[0])
            reply = "，".join(surfaced) if surfaced else "这个……我一时想不起来了。"

        draft = {
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": reply}],
            "stance": "answer_without_world_claims",
            "brief_rationale": "Surface only remembered values that reached this turn's context.",
        }
        if "appraisal_draft" in system and "expression_draft" in system:
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "affect": "no_change",
                        "brief_rationale": "No durable emotional implication is required.",
                        "behavior_tendency": "maintain",
                        "stance": "open",
                        "display_strategy": "natural",
                        "confidence": 7000,
                    },
                    "expression_draft": draft,
                },
                ensure_ascii=False,
            )
        return json.dumps(draft, ensure_ascii=False)


class StubBackgroundModel:
    """One deterministic boundary for every background adapter in the lane."""

    model = "fixture:memory-recall-background"

    def __init__(self, fixture: dict[str, object]) -> None:
        self._plants = _plants(fixture)

    async def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        system = messages[0]["content"]
        user = messages[1]["content"]

        if "Classify fallible semantic interpretations" in system:
            return '{"classifications":[]}'
        if "immediate inner appraisal" in system:
            return json.dumps(
                {
                    "appraise": False,
                    "affect": "no_change",
                    "brief_rationale": "No durable emotional implication is required.",
                    "behavior_tendency": "maintain",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 7000,
                }
            )
        if "already verified user Fact" in system:
            return json.dumps(
                {
                    "retain": True,
                    "cue_kind": "future_utility",
                    "retention_rationales": ["future_utility", "identity_relevance"],
                    "salience": {
                        "autobiographical_relevance_bp": 7000,
                        "relationship_relevance_bp": 5000,
                        "emotional_residue_bp": 1000,
                        "unfinished_business_bp": 1000,
                        "recurrence_bp": 3000,
                        "novelty_bp": 3000,
                        "future_utility_bp": 8000,
                        "world_continuity_bp": 4000,
                    },
                }
            )
        if "Assess one verified user message" in system:
            text = str(json.loads(user).get("text", ""))
            for marker, draft in self._plants.items():
                if marker in text:
                    return json.dumps(
                        {
                            "retain": True,
                            "predicate_code": draft["predicate_code"],
                            "value": draft["value"],
                            "privacy_class": "personal",
                            "confidence": 9200,
                            "rationale": "The user explicitly stated a stable personal fact.",
                        },
                        ensure_ascii=False,
                    )
            return '{"retain":false}'
        if "after one accepted appraisal" in system:
            return json.dumps(
                {
                    "affect": "no_change",
                    "brief_rationale": "The immediate proposal already captured the state.",
                    "behavior_tendency": "maintain",
                    "stance": "hold",
                    "display_strategy": "natural",
                    "confidence": 7000,
                }
            )
        if "relationship" in system.lower() and "suggested_deltas" in system:
            return '{"decision":"no_change"}'
        if "proactive opportunity" in system:
            # Proactive messages are outside what this eval scores, and an
            # unsolicited message between sessions would pollute the reply
            # slice a probe is measured against.
            return '{"timing_choice":"silent","brief_rationale":"Stay quiet during the eval."}'
        if "offered opaque opening token" in system:
            return '{"decision":"decline"}'
        return '{"decision":"no_change"}'


__all__ = ["StubBackgroundModel", "StubReplyModel"]
