# ADR 0016: One Character Interior Boundary for Subjective Integration and Choice

- Status: Accepted
- Date: 2026-08-04

This ADR supersedes the production `Current Self State` assembly and direct
role-adapter topology described by earlier documents. It retains ADR 0014's
`PrivateTurnState`, Recall/source-closure rules and same-author requirement,
placing them inside the unified Inner Turn.

## Context

World V2 has separate durable authorities for Character Core, Appraisal,
Affect, Relationship, Private Impression, Memory, Aspiration, Goal, Thread,
Commitment, Life, perception and Action. Keeping those authorities separate is
necessary because they have different evidence, lifecycle and replay rules.

The character-facing side, however, has accumulated multiple shallow seams.
Inbound expression, appraisal, Affect, relationship reflection, proactive
contact, Life, media and perception can each compile a slightly different
current-self view and invoke a role adapter independently. This creates four
systemic defects:

- the same actor and cursor do not necessarily produce the same current self;
- a current stimulus can be interpreted twice by parallel character-author
  calls, while its emotion becomes visible only on a later turn;
- Recall, private attention, subjective transition and expression lack one
  stable turn identity; and
- adding one inner-life mechanism requires wiring every business path and
  makes legacy and new paths easy to mix.

A single mutable `MindState`, a behavior-policy service, or a compatibility
facade would not solve this. The first would erase typed authority boundaries,
the second would violate ADR 0010, and the third would preserve the divergent
call graph behind a new name.

## Decision

World V2 adopts `CharacterInterior` as the only production boundary through
which an actor model integrates subjective experience or makes a character
choice. It exposes exactly three operations:

```python
class CharacterInterior:
    async def project(
        self, subject: InteriorStimulus | InteriorOpportunity
    ) -> InnerLifeSnapshot: ...
    async def experience(self, stimulus: InteriorStimulus) -> InnerTransition: ...
    async def consider(self, opportunity: InteriorOpportunity) -> InnerDecision: ...
```

`project` deterministically compiles one canonical, source-bound
`InnerLifeSnapshot` for an actor, ledger cursor, privacy scope and compiler
version. It performs no model call and owns no write authority.

`experience` lets the actor interpret a committed stimulus when no immediate
external choice is required. It may propose zero or more typed subjective
transitions. `consider` owns one actor decision opportunity. When the
opportunity includes a current stimulus, the same `InnerTurn` integrates that
stimulus, optionally performs actor-chosen Recall, forms the final
`PrivateTurnState`, proposes any subjective transitions and returns the
business-specific Character Decision. A caller must not run a parallel
background character interpretation for that stimulus.

The canonical snapshot makes eight Interior Faculties available without
turning them into a behavior pipeline or policy:

1. instantaneous private self and current attention;
2. actor-chosen selective Recall;
3. Appraisal and Affect formed from the same private context;
4. continuous emotion, residue, suppression and reinterpretation;
5. directional, fallible relationship orientation;
6. aspirations, goals and internal conflict;
7. autonomous impulse and action intention; and
8. a pre-expression inner stance that every new ExpressionDraft must consume.

An `InnerTurn` binds actor, purpose, opportunity/stimulus identity, ledger
cursor, Logical Time, snapshot hash, capability manifest, privacy scope,
provider request identity, Recall/correction lineage and terminal result.
Concurrent callers join one effect-once turn. Except for recorded Recall and
one constrained reselection by the same actor model, one opportunity cannot
have a second character author.

`CharacterInterior` may route model-authored proposals to existing Appraisal,
Affect, Relationship, Private Impression, Memory, Aspiration, Goal, Thread and
Commitment authorities. It does not replace them and no generic
`InnerLifeStateReplaced` event will be added. World facts, user facts,
biographical outcomes, privacy, consent, safety, external Actions, media,
perception acquisition, CAS, receipts and settlement remain outside the
Module.

Long-horizon proposals produced by `experience` use exact head/source
capabilities. Goal creation remains unavailable because the installed Goal
authority has no immutable character-authored outcome-content authority;
hashing free text into an opaque outcome ref would create a second truth
source. Existing Goal heads may be paused, resumed, or abandoned. Commitment
creation is available only against an offered open Thread fulfillment
contract, and MemoryCandidate retention only against an offered active Fact,
committed Experience, or terminal Thread authority. Invalid or stale choices
are rejected before proposal persistence and use the same-role correction
lifecycle.

This decision applies to the protagonist. NPCs retain their cost-bounded,
actor-scoped autonomy rather than cloning the protagonist's full Interior.
NPC events become source-bound stimuli for the protagonist's Interior. A
future full NPC Interior would require a separate actor-scoped instance and
private authority; it may never read or reuse the protagonist snapshot.

Invalid model output receives at most one constrained complete reselection by
the same actor model using the same pinned evidence and an exact failure
description. A second invalid result or a provider failure is a technical
failure. It must not become `silent`, `no_op`, `no_change`, fallback prose or a
second role provider result.

Production composition removes the direct public role interfaces and builder
parameters for inbound cognition/expression/appraisal, Affect, relationship,
Private Impression, proactive contact, Life Character, media choice and
perception attention. Useful implementation algorithms may move behind the
new Module under private names; the old public interfaces and parallel workers
may not remain as compatibility routes.

The former model-backed `SemanticAdvisoryAdapter`, `AdvisoryCompiler`, and
`PinnedTurn` advisory snapshot are removed from production rather than kept as
a non-authoritative second interpretation. A text endpoint may remain, but its
closed output is only the probability that the user will add another bubble;
it cannot construct current self, Appraisal, Affect, Relationship, motive, or
reply behavior. Its provider configuration is separate from the optional
same-character Thinking route, and legacy appraisal-named environment keys
cause an explicit startup error instead of acting as compatibility aliases.

The `current-self-state.1` payload and older expression/model contracts remain
readable only by explicitly historical replay codecs. New production requests
use `InnerLifeSnapshot` and a new identity formula. There is no dual write,
legacy feature flag or runtime fallback to the old role path.

The atomic cutover installs reducer bundle `.52`. It verifies `.50/.51`
historical head hashes before rebuilding derived state, and deterministically
terminalizes still-open processes belonging to physically retired character
author lanes. That migration performs no live model call and does not append,
rewrite or delete immutable events. New Appraisal writes use
`appraisal-matrix.2` with character-authored bounded free-text meaning;
historical `.1` strings remain replayable data, not a current behavior menu.

Situation stimuli are actor-observable capabilities, not merely events with a
permissive privacy label. NPC-only occurrences and experiences cannot wake or
populate the protagonist's proactive turn unless the protagonist participated
or a separate actor-bound perception was committed. The scheduler, retry,
legacy-process recovery and final proactive audit apply the same authority
test, so restart cannot become a visibility bypass.

The full contract, migration matrix, health metrics, tests and release process
are specified in `docs/design/unified-character-interior.md`.

## Consequences

- Chat, proactive contact, Life, media, perception and subjective follow-up
  see the same actor at the same cursor while receiving only their bounded
  capability view.
- Current Appraisal/Affect can shape the same inbound decision instead of
  necessarily appearing one turn later.
- Selective Recall and the final ExpressionDraft share one auditable causal
  lineage without persisting hidden chain-of-thought.
- New inner-life abilities are implemented once inside a deep Module rather
  than copied to every business path.
- The Module is a larger and more critical seam. Snapshot compilation,
  provider routing, failure classification and typed proposal dispatch need
  strong Interface-level tests and explicit health reporting.
- Migrating one caller at a time behind a long-lived compatibility facade is
  not allowed. The production cutover must remove old imports, constructor
  parameters, runtime workers and configuration together, while historical
  codecs remain isolated for replay.
- Actor-scoped contracts allow later NPC reuse, but NPC private state remains
  separate and inaccessible to the protagonist except through observable or
  settled evidence.

## Rejected alternatives

- **A single `turn()` method for every purpose:** smallest caller API, but it
  conflates deterministic projection, non-behavioral experience integration
  and an actual choice, making a god Module and accidental model calls during
  read paths more likely.
- **Only `resolve()` plus `project()`:** compact, but hides the important
  lifecycle difference between integrating a committed event and deciding an
  opportunity; callers would reintroduce purpose flags and branching.
- **A canonical snapshot without unified authoring:** improves reads but leaves
  parallel role models, next-turn emotion lag and split Recall/decision
  identity intact.
- **Keep old adapters behind a new facade:** preserves the exact divergence and
  compatibility ambiguity this decision is meant to remove.
- **One mutable persistent mind document:** destroys typed evidence,
  concurrency and replay semantics, and gives one model output excessive
  authority.
- **Rules or an emotion-to-expression matrix:** replaces character agency with
  host policy and violates Controlled High Variance.
