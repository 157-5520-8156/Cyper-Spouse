"""Static guard for the selected CharacterInterior composition seam."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_LEGACY_SYMBOLS = frozenset(
    {
        "AffectDraftDeliberationAdapter",
        "AffectDeliberationWorker",
        "AffectDeliberationWorkResult",
        "AffectTriggerRunResult",
        "AffectTriggerRuntime",
        "AppraisalDraftDeliberationAdapter",
        "AppraisalTriggerRunResult",
        "AspirationRuntime",
        "AspirationDraftAdapter",
        "AspirationChatModelAdapter",
        "AuditedReplacementReconsiderationReviewer",
        "ChatModelDeliberationAdapter",
        "ExpressionReconsiderationChatModelAdapter",
        "ExternalResultTriggerRuntime",
        "FastAppraisalDraftDeliberationAdapter",
        "FutureLifeAuthorRuntime",
        "InteractionBidDeliberationTurn",
        "InteractionBidProposalWorker",
        "InteractionBidTriggerRuntime",
        "InteractionAppraisalTriggerRuntime",
        "LifeAuthorResult",
        "LifeAuthorRuntime",
        "NpcInitiativeRuntime",
        "NoopPerceptionResultDeliberator",
        "NoopToolResultDeliberator",
        "_FastAppraisalDraftWire",
        "PrivateImpressionDraftAdapter",
        "ContextualLifeInspirationRuntime",
        "PlanDisruptionAppraisalTriggerRuntime",
        "PlanDisruptionAppraisalTurn",
        "ProactiveDraftAdapter",
        "QuickReactionVerticalWorker",
        "QuickReactionWorker",
        "ReadOnlyToolTriggerRuntime",
        "RelationshipDraftDeliberationAdapter",
        "RoutedChatModelDeliberationAdapter",
        "NpcWorldAppraisalTriggerRuntime",
        "OutcomeDraftDeliberationAdapter",
        "OutcomeSelectionDraftAdapter",
        "SettledWorldAppraisalTurn",
        "SilenceAppraisalTriggerRuntime",
        "SilenceAppraisalTurn",
        "SingleCallAppraisalAdapter",
        "SingleCallExpressionAdapter",
        "SingleCallInboundCognition",
        "SharedPrivateInvitationRuntime",
        "affect_deliberation_trigger_events",
        "affect_deliberation_trigger_id",
        "compose_injected_read_only_tool_deliberation",
        "ToolResultDeliberator",
    }
)

# These calls delegate a protagonist-owned judgment to an independently
# supplied model.  Deterministic authorities and transport models are not in
# this table: the guard protects semantic ownership, not every use of an LLM.
_ROLE_BYPASS_CONSTRUCTORS = {
    "MediaSelectionDraftAdapter": "media_selection",
    "QQPerceptionDecisionModel": "qq_perception",
    "ChatCompletionLiveAttentionModel": "external_attention",
    "ChatCompletionShadowAttentionModel": "external_attention",
    "ContextualLifeInspirationRuntime": "life_character",
    "PrivateImpressionDraftAdapter": "private_memory",
    "FactMemoryDraftAdapter": "private_memory",
    "FutureLifeAuthorRuntime": "life_character",
    "LifeAuthorResult": "life_character",
    "LifeAuthorRuntime": "life_character",
    "NpcInitiativeRuntime": "npc_actor_isolated",
    "NoopPerceptionResultDeliberator": "world_stimulus",
    "ExperienceMemoryDraftAdapter": "private_memory",
    "MemoryWithdrawalReviewAdapter": "private_memory",
    "OutcomeDraftDeliberationAdapter": "life_outcome",
    "OutcomeSelectionDraftAdapter": "life_outcome",
    "AspirationRuntime": "life_character",
    "AspirationDraftAdapter": "life_character",
    "AspirationChatModelAdapter": "life_character",
    "SharedPrivateInvitationRuntime": "life_character",
}
# These protagonist authors were physically retired by the unified
# CharacterInterior cutover. Catch their definitions as well as imports and
# construction so an orphan adapter cannot quietly return in an old module or
# a newly named production file.
_RETIRED_PROTAGONIST_AUTHOR_DEFINITIONS = {
    "AffectDraftDeliberationAdapter": "affect",
    "AffectDeliberationWorker": "affect",
    "AffectDeliberationWorkResult": "affect",
    "AffectTriggerRunResult": "affect",
    "AffectTriggerRuntime": "affect",
    "AppraisalTriggerRunResult": "inbound_turn",
    "AspirationRuntime": "life_character",
    "AspirationDraftAdapter": "life_character",
    "AspirationChatModelAdapter": "life_character",
    "AuditedReplacementReconsiderationReviewer": "expression_reconsideration",
    "ChatCompletionLiveAttentionModel": "external_attention",
    "ChatCompletionShadowAttentionModel": "external_attention",
    "ContextualLifeInspirationRuntime": "life_character",
    "ExperienceMemoryDraftAdapter": "private_memory",
    "ExpressionReconsiderationChatModelAdapter": "expression_reconsideration",
    "ExternalResultTriggerRuntime": "world_stimulus",
    "_FastAppraisalDraftWire": "inbound_turn",
    "FactMemoryDraftAdapter": "private_memory",
    "FutureLifeAuthorRuntime": "life_character",
    "InteractionAppraisalTriggerRuntime": "inbound_turn",
    "LifeAuthorResult": "life_character",
    "LifeAuthorRuntime": "life_character",
    "MediaSelectionDraftAdapter": "media_selection",
    "MemoryWithdrawalReviewAdapter": "private_memory",
    "OutcomeDraftDeliberationAdapter": "life_outcome",
    "OutcomeSelectionDraftAdapter": "life_outcome",
    "NoopToolResultDeliberator": "world_stimulus",
    "PrivateImpressionDraftAdapter": "private_memory",
    "PlanDisruptionAppraisalTriggerRuntime": "world_stimulus",
    "PlanDisruptionAppraisalTurn": "world_stimulus",
    "ProactiveDraftAdapter": "proactive_contact",
    "NpcWorldAppraisalTriggerRuntime": "world_stimulus",
    "SettledWorldAppraisalTurn": "world_stimulus",
    "SilenceAppraisalTriggerRuntime": "world_stimulus",
    "SilenceAppraisalTurn": "world_stimulus",
    "RelationshipDraftDeliberationAdapter": "relationship",
    "SingleCallAppraisalAdapter": "inbound_turn",
    "SingleCallExpressionAdapter": "inbound_turn",
    "SingleCallInboundCognition": "inbound_turn",
    "SharedPrivateInvitationRuntime": "life_character",
    "affect_deliberation_trigger_events": "affect",
    "affect_deliberation_trigger_id": "affect",
    "ToolResultDeliberator": "world_stimulus",
}
_ROLE_BYPASS_PARAMETERS = {
    "interaction_bid_model": "social_continuation",
    "media_selection_model": "media_selection",
    "perception_model": "qq_perception",
    "life_character_model": "life_character",
    "private_impression_model": "private_memory",
    "memory_model": "private_memory",
    "outcome_draft_model": "life_outcome",
    "outcome_model": "life_outcome",
    "outcome_selection_model": "life_outcome",
    "external_result_deliberator": "world_stimulus",
    "external_result_owner": "world_stimulus",
    "read_only_tool_model": "read_only_tool",
    "aspiration_followup": "life_character",
    "aspiration_model": "life_character",
    "aspiration_adapter": "life_character",
    "future_life_author_followup": "life_character",
    "life_author_followup": "life_character",
    "shared_private_followup": "life_character",
}
_RUNTIME_FACULTY_EXTRACTORS = frozenset(
    {"runtime_faculty", "optional_runtime_faculty", "_runtime_faculty"}
)
# A wrapper around the historical author ports is still a compatibility
# surface, even when that wrapper lives inside ``character_interior``.  Keep
# this list deliberately small and exact: private purpose implementations may
# retain useful algorithms, but production must not be able to extract those
# algorithms and wire them as independent authors again.
_FORBIDDEN_INTERIOR_FACADES = frozenset(
    {
        "CharacterInteriorApplicationPorts",
        "CharacterInteriorRuntimeEffects",
        "character_interior_application_ports",
        "compose_character_interior_runtime_effects",
        "runtime_faculty",
        "optional_runtime_faculty",
        "inbound_cognition",
    }
)
_TESTING_HELPER_SYMBOLS = frozenset({"build_sqlite_world_v2_test_application"})
_TEST_ONLY_INTERIOR_FIXTURE_HELPERS = frozenset({"compose_adapter_fixture_character_interior"})
_TESTING_MODULE_LEAVES = frozenset({"testing_application", "world_v2_application"})
_SNAPSHOT_COMPILER_SYMBOL = "compile_inner_life_snapshot"
_RETIRED_LIFE_DEVELOPMENT_CHARACTER_WRITER_SURFACES = frozenset(
    {
        "character_model_role",
        "character_deliberation",
        "_run_character_choice_phase",
    }
)
_SNAPSHOT_PRODUCER_PREFIXES = (
    "build",
    "compile",
    "create",
    "derive",
    "make",
    "project",
)
_LEGACY_MODULE_LEAVES = frozenset(
    {
        "affect_chat_model_adapter",
        "affect_deliberation_worker",
        "affect_trigger",
        "affect_trigger_runtime",
        "appraisal_chat_model_adapter",
        "aspiration_runtime",
        "chat_model_deliberation_adapter",
        "contextual_life_inspiration",
        "expression_reconsideration_model_adapter",
        "external_result_trigger_runtime",
        "future_life_author",
        "interaction_bid_deliberation_turn",
        "interaction_bid_proposal_worker",
        "interaction_bid_trigger_runtime",
        "interaction_appraisal_trigger_runtime",
        "life_author_runtime",
        "npc_initiative",
        "quick_reaction",
        "quick_reaction_vertical",
        "read_only_tool_deliberation",
        "read_only_tool_trigger_runtime",
        "relationship_draft_deliberation_adapter",
        "npc_world_appraisal_trigger_runtime",
        "plan_disruption_appraisal_trigger_runtime",
        "settled_world_appraisal_turn",
        "silence_appraisal_trigger_runtime",
        "single_call_inbound_cognition",
        "outcome_draft_deliberation_adapter",
        "outcome_selection_draft",
    }
)
_NEW_INTERFACE_PARAMETER = "character_interior"
_NEW_INTERFACE_SYMBOL = "CharacterInterior"
_LEGACY_INTERFACE_PARAMETERS = frozenset(
    {
        "affect_model",
        "affect_deliberation_owner",
        "affect_worker",
        "affect_worker_owner",
        "appraisal_model",
        "appraisal_worker_owner",
        "aspiration_followup",
        "aspiration_model",
        "aspiration_adapter",
        "expression_reconsideration_reviewer",
        "external_result_deliberator",
        "external_result_owner",
        "life_character_model",
        "main_model",
        "memory_model",
        "outcome_draft_model",
        "outcome_model",
        "outcome_selection_model",
        "aspiration_enabled",
        "aspiration_fade_chance_bp",
        "aspiration_fade_idle_days",
        "aspiration_crystallize_chance_bp",
        "future_life_author_enabled",
        "future_life_author_followup",
        "life_author_followup",
        "private_impression_adapter",
        "private_impression_model",
        "proactive_model",
        "quick_reaction_enabled",
        "quick_reaction_model",
        "quick_reaction_worker",
        "quick_recovery",
        "interaction_appraisal_owner",
        "interaction_appraisal_turn",
        "interaction_bid_model",
        "legacy_event_media_planner",
        "event_media_result_store",
        "read_only_tool_model",
        "read_only_tool_transport",
        "relationship_deliberation_owner",
        "relationship_model",
        "relationship_worker",
        "relationship_worker_owner",
        "shared_private_invitation_enabled",
        "shared_private_invite_chance_bp",
        "shared_private_followup",
    }
)

_RETIRED_QUICK_REACTION_NAMES = frozenset(
    {
        "QuickReactionVerticalWorker",
        "QuickReactionWorker",
        "_bdv_pilot_disabled",
        "_quick_reaction_worker",
        "quick_reaction_enabled",
        "quick_reaction_model",
        "quick_reaction_worker",
    }
)
_RETIRED_DORMANT_CHARACTER_LANE_CONSTRUCTORS = frozenset(
    {
        "InteractionBidDeliberationTurn",
        "InteractionBidProposalWorker",
        "InteractionBidTriggerRuntime",
        "ReadOnlyToolTriggerRuntime",
        "compose_injected_read_only_tool_deliberation",
    }
)
_ACTOR_SEMANTIC_MODEL_NAMES = frozenset({"actor_model", "npc_actor_model"})
# NPC Ecology is the one approved low-cost, actor-isolated semantic domain.
# Keep every injection surface exact so a newly named protagonist author (or
# a second NPC lane) cannot hide behind a generic ``actor_model`` parameter.
_APPROVED_ACTOR_SEMANTIC_SIGNATURES = frozenset(
    {
        ("npc_ecology.py", "__init__", "actor_model"),
        (
            "production_turn_application.py",
            "build_sqlite_world_v2_turn_application",
            "npc_actor_model",
        ),
    }
)
_APPROVED_ACTOR_SEMANTIC_CALLS = frozenset(
    {
        ("cli.py", "build_sqlite_world_v2_turn_application", "npc_actor_model"),
        (
            "qq_c2c_host.py",
            "build_sqlite_world_v2_turn_application",
            "npc_actor_model",
        ),
        (
            "http_capture_host.py",
            "build_sqlite_world_v2_turn_application",
            "npc_actor_model",
        ),
        ("production_turn_application.py", "NpcEcology", "actor_model"),
    }
)
_LEGACY_SIGNATURE_SYMBOLS = _LEGACY_SYMBOLS | frozenset(
    {
        "ExpressionReconsiderationReviewer",
        "PrivateImpressionChatModel",
        "SingleCallAppraisalAdapter",
        "SingleCallExpressionAdapter",
    }
)
_COMPOSITION_ROOTS = (
    Path("src/companion_daemon/cli.py"),
    Path("src/companion_daemon/napcat_cli.py"),
    Path("src/companion_daemon/world_v2/production_turn_application.py"),
    Path("src/companion_daemon/world_v2/runtime.py"),
    Path("src/companion_daemon/world_v2/semantic_chat_composition.py"),
)

# ``FakeCompanionModel`` is a useful explicit scenario/offline fixture, but a
# production host importing it can silently turn missing provider
# configuration into authored fixture prose.  Keep this list to the actual
# online composition roots: simulator CLI, scenario runner, OneBot fixture
# host, and tests remain outside it by design.
_PRODUCTION_COMPOSITION_ROOTS = (
    Path("src/companion_daemon/app.py"),
    Path("src/companion_daemon/napcat_cli.py"),
    Path("src/companion_daemon/world_v2/http_capture_host.py"),
    Path("src/companion_daemon/world_v2/production_turn_application.py"),
    Path("src/companion_daemon/world_v2/qq_c2c_host.py"),
    Path("src/companion_daemon/world_v2/runtime.py"),
    Path("src/companion_daemon/world_v2/semantic_chat_composition.py"),
)
_FIXTURE_CHARACTER_MODEL_SYMBOL = "FakeCompanionModel"
_COMPOSITION_CALLEES = frozenset(
    {
        "SemanticChatComposition",
        "WorldRuntime",
        "build_sqlite_world_v2_turn_application",
    }
)

# Generic hard-boundary infrastructure still uses these transport-oriented
# names. Exempt the exact definitions, not their files: every other function,
# class and new module remains covered by the reverse-boundary scan.
_GENERIC_BOUNDARY_SURFACES = frozenset(
    {
        ("deliberation.py", "Deliberation"),
        ("deliberation.py", "__init__"),
        ("production_proposal_grammar.py", "compose_production_deliberation"),
    }
)

# Replay codecs are allowed to name historical contracts but are never a live
# composition surface. Keep this list exact and review additions individually.
_HISTORICAL_REPLAY_ALLOWLIST = frozenset(
    {
        "world_v2/legacy_deferred_reply_migration.py",
        "world_v2/replay_evaluator.py",
        "world_v2/replay_evidence.py",
        "world_v2/upcasting.py",
    }
)
_RETIRED_LIVE_PROVIDER_CONTRACTS = frozenset({"expression-units.1"})
_PROVIDER_CONTRACT_POLICY_MODULE = "world_v2/character_interior_architecture_guard.py"

_INBOUND_PRIVATE_AUTHOR = "_InboundCharacterAuthor"
_INBOUND_PRIVATE_WIRES = frozenset(
    {
        "_ExpressionDraftWire",
        "_PairedAppraisalMaterializer",
        "_PairedExpressionMaterializer",
        "_RoutedExpressionDraftWire",
    }
)
_RETIRED_INBOUND_AUTHOR_NAMES = frozenset(
    {
        "_AppraisalDraftWire",
        "_discover_recovery_model",
        "_recovery_expression",
        "_recovery_model",
        "_retry_with_recovery_provider",
        "discover_recovery_model",
        "recovery_model",
    }
)


def _module_leaf(module: str) -> str:
    return module.rsplit(".", 1)[-1]


def _is_legacy_module(module: str) -> bool:
    return _module_leaf(module) in _LEGACY_MODULE_LEAVES


def _is_testing_module(module: str) -> bool:
    return (
        _module_leaf(module) in _TESTING_MODULE_LEAVES
        or module == "tests"
        or module.startswith("tests.")
    )


def _inside_character_interior(path: Path) -> bool:
    return "character_interior" in path.parts


def _is_production_composition_root(path: Path) -> bool:
    parts = path.parts
    return any(
        len(parts) >= len(relative.parts) and parts[-len(relative.parts) :] == relative.parts
        for relative in _PRODUCTION_COMPOSITION_ROOTS
    )


def _call_symbol(node: ast.Call, imported_aliases: dict[str, str]) -> str | None:
    if isinstance(node.func, ast.Name):
        return imported_aliases.get(node.func.id, node.func.id)
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _literal_keyword(node: ast.Call, name: str) -> object | None:
    for keyword in node.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


def _is_snapshot_producer_name(name: str) -> bool:
    normalized = name.casefold().lstrip("_")
    return "inner_life_snapshot" in normalized and normalized.startswith(
        _SNAPSHOT_PRODUCER_PREFIXES
    )


def _annotation_symbols(annotation: ast.expr | None) -> frozenset[str]:
    if annotation is None:
        return frozenset()
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return frozenset(
            symbol
            for symbol in (*_LEGACY_SIGNATURE_SYMBOLS, _NEW_INTERFACE_SYMBOL)
            if symbol in annotation.value
        )
    return frozenset(
        node.id for node in ast.walk(annotation) if isinstance(node, ast.Name)
    ) | frozenset(node.attr for node in ast.walk(annotation) if isinstance(node, ast.Attribute))


def _function_surface(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[frozenset[str], frozenset[str]]:
    arguments = (
        *node.args.posonlyargs,
        *node.args.args,
        *node.args.kwonlyargs,
        *((node.args.vararg,) if node.args.vararg is not None else ()),
        *((node.args.kwarg,) if node.args.kwarg is not None else ()),
    )
    names = frozenset(argument.arg for argument in arguments)
    symbols = frozenset().union(
        *(_annotation_symbols(argument.annotation) for argument in arguments),
        _annotation_symbols(node.returns),
    )
    return names, symbols


def _class_surface(node: ast.ClassDef) -> tuple[frozenset[str], frozenset[str]]:
    fields = tuple(
        statement
        for statement in node.body
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
    )
    return (
        frozenset(statement.target.id for statement in fields),
        frozenset().union(*(_annotation_symbols(statement.annotation) for statement in fields)),
    )


def _surface_violation(
    *,
    path: Path,
    lineno: int,
    names: frozenset[str],
    symbols: frozenset[str],
) -> CharacterInteriorArchitectureViolation | None:
    legacy = (names & _LEGACY_INTERFACE_PARAMETERS) | (symbols & _LEGACY_SIGNATURE_SYMBOLS)
    if not legacy:
        return None
    has_new = _NEW_INTERFACE_PARAMETER in names or _NEW_INTERFACE_SYMBOL in symbols
    rule = (
        "mixed_character_interior_signature" if has_new else "legacy_character_interior_signature"
    )
    return CharacterInteriorArchitectureViolation(
        path,
        lineno,
        rule,
        ", ".join(sorted(legacy)),
    )


def _retired_author_definition_violation(
    *,
    path: Path,
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
) -> CharacterInteriorArchitectureViolation | None:
    lane = _RETIRED_PROTAGONIST_AUTHOR_DEFINITIONS.get(node.name)
    if lane is None:
        return None
    return CharacterInteriorArchitectureViolation(
        path,
        node.lineno,
        "retired_protagonist_author_definition",
        f"{lane}: {node.name}",
    )


@dataclass(frozen=True, slots=True)
class CharacterInteriorArchitectureViolation:
    """One source-level regression to an independent character-author lane."""

    path: Path
    lineno: int
    rule: str
    detail: str

    def render(self, *, repository_root: Path) -> str:
        try:
            rendered_path = self.path.relative_to(repository_root)
        except ValueError:
            rendered_path = self.path
        return f"{rendered_path}:{self.lineno}: {self.rule}: {self.detail}"


class CharacterInteriorArchitectureError(RuntimeError):
    """Raised when a production composition regresses to an old author lane."""


def scan_character_interior_source(
    path: Path,
) -> tuple[CharacterInteriorArchitectureViolation, ...]:
    """Return forbidden legacy imports and constructions in one source file."""

    if _inside_character_interior(path):
        return ()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported_aliases: dict[str, str] = {}
    violations: list[CharacterInteriorArchitectureViolation] = []
    for node in ast.walk(tree):
        if path.name == "life_development_runtime.py":
            retired_life_surface: str | None = None
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in _RETIRED_LIFE_DEVELOPMENT_CHARACTER_WRITER_SURFACES
            ):
                retired_life_surface = node.value
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                node.name in _RETIRED_LIFE_DEVELOPMENT_CHARACTER_WRITER_SURFACES
            ):
                retired_life_surface = node.name
            if retired_life_surface is not None:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "retired_life_development_character_writer",
                        retired_life_surface,
                    )
                )
        retired_name: str | None = None
        if isinstance(node, ast.Name):
            retired_name = node.id
        elif isinstance(node, ast.Attribute):
            retired_name = node.attr
        if retired_name in _RETIRED_QUICK_REACTION_NAMES:
            violations.append(
                CharacterInteriorArchitectureViolation(
                    path,
                    node.lineno,
                    "retired_quick_reaction_surface",
                    retired_name,
                )
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_leaf(alias.name) in {"quick_reaction", "quick_reaction_vertical"}:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "retired_quick_reaction_surface",
                            alias.name,
                        )
                    )
                if _is_testing_module(alias.name):
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "production_testing_application_dependency",
                            alias.name,
                        )
                    )
                if _is_legacy_module(alias.name):
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "legacy_character_interior_import",
                            alias.name,
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imported_aliases[alias.asname or alias.name] = alias.name
                if (
                    _is_production_composition_root(path)
                    and alias.name == _FIXTURE_CHARACTER_MODEL_SYMBOL
                ):
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "production_fixture_character_model",
                            f"import {module}.{alias.name}",
                        )
                    )
                if (
                    _module_leaf(module) in {"quick_reaction", "quick_reaction_vertical"}
                    or alias.name in _RETIRED_QUICK_REACTION_NAMES
                ):
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "retired_quick_reaction_surface",
                            f"{module}.{alias.name}",
                        )
                    )
                if _is_testing_module(module) or alias.name in _TESTING_HELPER_SYMBOLS:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "production_testing_application_dependency",
                            f"{module}.{alias.name}",
                        )
                    )
                if alias.name in _TEST_ONLY_INTERIOR_FIXTURE_HELPERS:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "production_test_fixture_dependency",
                            f"{module}.{alias.name}",
                        )
                    )
                if alias.name == _SNAPSHOT_COMPILER_SYMBOL:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "snapshot_compiler_bypass",
                            f"{module}.{alias.name}",
                        )
                    )
                bypass_lane = _ROLE_BYPASS_CONSTRUCTORS.get(alias.name)
                if bypass_lane is not None:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "protagonist_role_bypass",
                            f"{bypass_lane}: import {alias.name}",
                        )
                    )
                if _is_legacy_module(module) or alias.name in _LEGACY_SYMBOLS:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "legacy_character_interior_import",
                            f"{module}.{alias.name}",
                        )
                    )
        elif isinstance(node, ast.Call):
            symbol = _call_symbol(node, imported_aliases)
            if _is_production_composition_root(path) and symbol == _FIXTURE_CHARACTER_MODEL_SYMBOL:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "production_fixture_character_model",
                        f"construct {symbol}",
                    )
                )
            if symbol in _RETIRED_DORMANT_CHARACTER_LANE_CONSTRUCTORS:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "retired_dormant_character_lane",
                        str(symbol),
                    )
                )
            if symbol == _SNAPSHOT_COMPILER_SYMBOL:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "snapshot_compiler_bypass",
                        symbol,
                    )
                )
            if symbol is not None and symbol in _LEGACY_SYMBOLS:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "legacy_character_interior_construction",
                        symbol,
                    )
                )
            if symbol in _TEST_ONLY_INTERIOR_FIXTURE_HELPERS:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "production_test_fixture_dependency",
                        str(symbol),
                    )
                )
            if symbol in _RUNTIME_FACULTY_EXTRACTORS:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "runtime_faculty_extraction_outside_interior",
                        symbol,
                    )
                )
            bypass_lane = _ROLE_BYPASS_CONSTRUCTORS.get(symbol or "")
            if bypass_lane is not None:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "protagonist_role_bypass",
                        f"{bypass_lane}: {symbol}",
                    )
                )
            if symbol == "RoleBoundLifeDevelopmentModelAdapter":
                role = _literal_keyword(node, "role")
                role_is_character = isinstance(role, str) and not role.startswith("world_author")
                role_is_dynamic_outside_adapter = (
                    role is None and path.name != "life_development_model_adapter.py"
                )
                if role_is_character or role_is_dynamic_outside_adapter:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "protagonist_role_bypass",
                            "life_character: RoleBoundLifeDevelopmentModelAdapter",
                        )
                    )
            keyword_names = frozenset(
                keyword.arg for keyword in node.keywords if keyword.arg is not None
            )
            for parameter in sorted(keyword_names & _ACTOR_SEMANTIC_MODEL_NAMES):
                if (path.name, str(symbol), parameter) not in _APPROVED_ACTOR_SEMANTIC_CALLS:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "unregistered_actor_semantic_domain",
                            f"call {symbol}: parameter {parameter}",
                        )
                    )
            for retired in sorted(keyword_names & _RETIRED_QUICK_REACTION_NAMES):
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "retired_quick_reaction_surface",
                        retired,
                    )
                )
            for parameter in sorted(keyword_names & _ROLE_BYPASS_PARAMETERS.keys()):
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "protagonist_role_bypass",
                        f"{_ROLE_BYPASS_PARAMETERS[parameter]}: parameter {parameter}",
                    )
                )
            if symbol in {"LiveCharacterAttentionContext", "CharacterAttentionContext"}:
                for keyword in node.keywords:
                    if keyword.arg == "inner_life_snapshot" and isinstance(keyword.value, ast.Call):
                        violations.append(
                            CharacterInteriorArchitectureViolation(
                                path,
                                node.lineno,
                                "second_inner_life_snapshot_producer",
                                f"{symbol}.inner_life_snapshot",
                            )
                        )
            legacy_keywords = keyword_names & _LEGACY_INTERFACE_PARAMETERS
            if legacy_keywords and symbol in _COMPOSITION_CALLEES:
                rule = (
                    "mixed_character_interior_call"
                    if _NEW_INTERFACE_PARAMETER in keyword_names
                    else "legacy_character_interior_call"
                )
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        rule,
                        ", ".join(sorted(legacy_keywords)),
                    )
                )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            retired = _retired_author_definition_violation(path=path, node=node)
            if retired is not None:
                violations.append(retired)
            if node.name in _RETIRED_QUICK_REACTION_NAMES:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "retired_quick_reaction_surface",
                        node.name,
                    )
                )
            if node.name in _TESTING_HELPER_SYMBOLS:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "testing_helper_in_production_tree",
                        node.name,
                    )
                )
            if _is_snapshot_producer_name(node.name):
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "second_inner_life_snapshot_producer",
                        node.name,
                    )
                )
            function_names, function_symbols = _function_surface(node)
            for parameter in sorted(function_names & _ACTOR_SEMANTIC_MODEL_NAMES):
                if (
                    path.name,
                    node.name,
                    parameter,
                ) not in _APPROVED_ACTOR_SEMANTIC_SIGNATURES:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "unregistered_actor_semantic_domain",
                            f"signature {node.name}: parameter {parameter}",
                        )
                    )
            for parameter in sorted(function_names & _ROLE_BYPASS_PARAMETERS.keys()):
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "protagonist_role_bypass",
                        f"{_ROLE_BYPASS_PARAMETERS[parameter]}: parameter {parameter}",
                    )
                )
            violation = (
                None
                if (path.name, node.name) in _GENERIC_BOUNDARY_SURFACES
                else _surface_violation(
                    path=path,
                    lineno=node.lineno,
                    names=function_names,
                    symbols=function_symbols,
                )
            )
            if violation is not None:
                violations.append(violation)
        elif isinstance(node, ast.ClassDef):
            retired = _retired_author_definition_violation(path=path, node=node)
            if retired is not None:
                violations.append(retired)
            if _is_snapshot_producer_name(node.name):
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "second_inner_life_snapshot_producer",
                        node.name,
                    )
                )
            names, symbols = _class_surface(node)
            for parameter in sorted(names & _ROLE_BYPASS_PARAMETERS.keys()):
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "protagonist_role_bypass",
                        f"{_ROLE_BYPASS_PARAMETERS[parameter]}: field {parameter}",
                    )
                )
            violation = (
                None
                if (path.name, node.name) in _GENERIC_BOUNDARY_SURFACES
                else _surface_violation(
                    path=path,
                    lineno=node.lineno,
                    names=names,
                    symbols=symbols,
                )
            )
            if violation is not None:
                violations.append(violation)
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "_actor_model"
            and path.name != "npc_ecology.py"
        ):
            violations.append(
                CharacterInteriorArchitectureViolation(
                    path,
                    node.lineno,
                    "unregistered_actor_semantic_domain",
                    "private actor model outside npc_ecology.py",
                )
            )
    return tuple(dict.fromkeys(violations))


def _scan_forbidden_interior_facades(
    path: Path,
) -> tuple[CharacterInteriorArchitectureViolation, ...]:
    """Reject a new public-looking wrapper around the pre-cutover authors.

    The rest of the CharacterInterior implementation is intentionally opaque
    to the reverse-boundary scan.  This focused pass is the exception that
    prevents the Module itself from exporting a bag of legacy author ports or
    a second runtime effects interface.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[CharacterInteriorArchitectureViolation] = []
    for node in ast.walk(tree):
        name: str | None = None
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _FORBIDDEN_INTERIOR_FACADES:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "character_interior_compatibility_facade",
                            alias.name,
                        )
                    )
        if name in _FORBIDDEN_INTERIOR_FACADES:
            violations.append(
                CharacterInteriorArchitectureViolation(
                    path,
                    node.lineno,
                    "character_interior_compatibility_facade",
                    name,
                )
            )
        if isinstance(node, ast.Call):
            symbol = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if symbol == "InboundTurnFaculty":
                keyword_names = {
                    keyword.arg for keyword in node.keywords if keyword.arg is not None
                }
                retired = keyword_names & {"expression", "appraisal", "recovery"}
                if retired:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "split_inbound_cognition_composition",
                            ", ".join(sorted(retired)),
                        )
                    )
    return tuple(dict.fromkeys(violations))


def _scan_interior_private_author_boundary(
    path: Path,
) -> tuple[CharacterInteriorArchitectureViolation, ...]:
    """Keep the sole inbound author and its wires non-extractable by shape.

    Unlike the former whole-file allowlist, this pass names the only allowed
    definition and construction sites. A new file is scanned automatically,
    and a private wire cannot quietly become a second purpose Faculty.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[CharacterInteriorArchitectureViolation] = []
    for node in ast.walk(tree):
        retired_surface: str | None = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names, _symbols = _function_surface(node)
            retired = names & _RETIRED_INBOUND_AUTHOR_NAMES
            if node.name in _RETIRED_INBOUND_AUTHOR_NAMES:
                retired_surface = node.name
            elif retired:
                retired_surface = ", ".join(sorted(retired))
        elif isinstance(node, ast.ClassDef) and node.name in _RETIRED_INBOUND_AUTHOR_NAMES:
            retired_surface = node.name
        elif isinstance(node, ast.Attribute) and node.attr in _RETIRED_INBOUND_AUTHOR_NAMES:
            retired_surface = node.attr
        elif isinstance(node, ast.Call):
            retired = {
                keyword.arg
                for keyword in node.keywords
                if keyword.arg in _RETIRED_INBOUND_AUTHOR_NAMES
            }
            if retired:
                retired_surface = ", ".join(sorted(retired))
        if retired_surface is not None:
            violations.append(
                CharacterInteriorArchitectureViolation(
                    path,
                    node.lineno,
                    "retired_inbound_author_surface",
                    retired_surface,
                )
            )
        if isinstance(node, ast.ImportFrom) and not _inside_character_interior(path):
            for alias in node.names:
                if alias.name == _INBOUND_PRIVATE_AUTHOR or alias.name in _INBOUND_PRIVATE_WIRES:
                    violations.append(
                        CharacterInteriorArchitectureViolation(
                            path,
                            node.lineno,
                            "inbound_private_implementation_import",
                            alias.name,
                        )
                    )
        if isinstance(node, ast.ClassDef):
            if node.name == _INBOUND_PRIVATE_AUTHOR and path.name != "inbound_author.py":
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "duplicate_inbound_character_author",
                        node.name,
                    )
                )
            if node.name in _INBOUND_PRIVATE_WIRES and path.name not in {
                "inbound_appraisal_wire.py",
                "inbound_author.py",
                "inbound_wire.py",
            }:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "inbound_wire_outside_private_implementation",
                        node.name,
                    )
                )
        if isinstance(node, ast.Call):
            symbol = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else None
            )
            if symbol == _INBOUND_PRIVATE_AUTHOR and path.name != "production.py":
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "inbound_character_author_construction_outside_composition",
                        symbol,
                    )
                )
            if symbol in _INBOUND_PRIVATE_WIRES and path.name not in {
                "inbound_appraisal_wire.py",
                "inbound_author.py",
                "inbound_wire.py",
            }:
                violations.append(
                    CharacterInteriorArchitectureViolation(
                        path,
                        node.lineno,
                        "inbound_wire_construction_outside_author",
                        str(symbol),
                    )
                )
        if (
            isinstance(node, ast.Attribute)
            and node.attr
            in {
                "_appraisal_materializer",
                "_expression_materializer",
            }
            and path.name != "inbound_author.py"
        ):
            violations.append(
                CharacterInteriorArchitectureViolation(
                    path,
                    node.lineno,
                    "inbound_wire_extraction_outside_author",
                    node.attr,
                )
            )
    return tuple(dict.fromkeys(violations))


def _scan_retired_author_definitions(
    path: Path,
) -> tuple[CharacterInteriorArchitectureViolation, ...]:
    """Scan files otherwise exempted as private algorithm implementations."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[CharacterInteriorArchitectureViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        violation = _retired_author_definition_violation(path=path, node=node)
        if violation is not None:
            violations.append(violation)
    return tuple(dict.fromkeys(violations))


def _scan_retired_live_provider_contracts(
    path: Path,
) -> tuple[CharacterInteriorArchitectureViolation, ...]:
    """Reject historical provider wires everywhere except replay codecs."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return tuple(
        CharacterInteriorArchitectureViolation(
            path,
            node.lineno,
            "retired_live_provider_contract",
            node.value,
        )
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in _RETIRED_LIVE_PROVIDER_CONTRACTS
    )


def _scan_causal_opportunity_bypasses(
    path: Path,
) -> tuple[CharacterInteriorArchitectureViolation, ...]:
    """Keep production scheduling on the unified opportunity entry point."""

    if path.name != "production.py":
        return ()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations: list[CharacterInteriorArchitectureViolation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if (
            node.func.attr == "drain_one"
            and isinstance(target, ast.Attribute)
            and target.attr == "_world_stimulus"
        ):
            violations.append(
                CharacterInteriorArchitectureViolation(
                    path,
                    node.lineno,
                    "scattered_causal_opportunity_bypass",
                    "_world_stimulus.drain_one",
                )
            )
    return tuple(dict.fromkeys(violations))


def scan_character_interior_architecture(
    repository_root: Path,
) -> tuple[CharacterInteriorArchitectureViolation, ...]:
    """Scan every production Python file outside the unified Module.

    This is deliberately a reverse boundary: new files are covered without
    being added to a list. Only exact replay codecs and the Module's own
    implementation directory are excluded.
    """

    repository_root = repository_root.resolve()
    production_root = repository_root / "src" / "companion_daemon"
    if not production_root.is_dir():
        return ()
    violations: list[CharacterInteriorArchitectureViolation] = []
    for path in sorted(production_root.rglob("*.py")):
        relative = path.relative_to(production_root).as_posix()
        if relative in _HISTORICAL_REPLAY_ALLOWLIST:
            continue
        if relative != _PROVIDER_CONTRACT_POLICY_MODULE:
            violations.extend(_scan_retired_live_provider_contracts(path))
        if _inside_character_interior(path):
            violations.extend(_scan_forbidden_interior_facades(path))
            violations.extend(_scan_retired_author_definitions(path))
            violations.extend(_scan_interior_private_author_boundary(path))
            violations.extend(_scan_causal_opportunity_bypasses(path))
            continue
        if "tests" in path.relative_to(production_root).parts or path.name.startswith("test_"):
            continue
        if relative == "world_v2/testing_application.py":
            violations.append(
                CharacterInteriorArchitectureViolation(
                    path,
                    1,
                    "testing_helper_in_production_tree",
                    "move fixture composition to tests/support",
                )
            )
        violations.extend(scan_character_interior_source(path))
        violations.extend(_scan_interior_private_author_boundary(path))
    return tuple(dict.fromkeys(violations))


def classify_character_interior_violations(
    violations: tuple[CharacterInteriorArchitectureViolation, ...],
) -> dict[str, tuple[CharacterInteriorArchitectureViolation, ...]]:
    """Group a complete scan without hiding duplicate sites or categories."""

    grouped: dict[str, list[CharacterInteriorArchitectureViolation]] = {}
    for violation in violations:
        grouped.setdefault(violation.rule, []).append(violation)
    return {rule: tuple(items) for rule, items in grouped.items()}


def render_character_interior_architecture_report(
    violations: tuple[CharacterInteriorArchitectureViolation, ...],
    *,
    repository_root: Path,
) -> str:
    """Render one stable, category-first migration report."""

    if not violations:
        return "CharacterInterior architecture: no violations"
    lines = ["CharacterInterior architecture violations:"]
    for rule, items in classify_character_interior_violations(violations).items():
        lines.append(f"- {rule} ({len(items)})")
        lines.extend(f"  {item.render(repository_root=repository_root)}" for item in items)
    return "\n".join(lines)


def assert_character_interior_architecture(repository_root: Path) -> None:
    """Raise one actionable error for every old or mixed composition seam."""

    violations = scan_character_interior_architecture(repository_root)
    if violations:
        raise CharacterInteriorArchitectureError(
            "CharacterInterior architecture guard failed:\n"
            + render_character_interior_architecture_report(
                violations,
                repository_root=repository_root,
            )
        )


__all__ = [
    "CharacterInteriorArchitectureError",
    "CharacterInteriorArchitectureViolation",
    "assert_character_interior_architecture",
    "classify_character_interior_violations",
    "render_character_interior_architecture_report",
    "scan_character_interior_architecture",
    "scan_character_interior_source",
]
