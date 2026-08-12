# Continuous Inner Life v1

> Status: implemented thin slice; `manual_only / qualification_incomplete`
>
> This document does not authorize a production daemon replacement.

## Intent

The character should retain a continuous inner life across chat and idle time
without paying for an always-running model loop and without storing raw chain of
thought.  Continuity means that later choices can read durable, role-authored
state and its exact sources.  It does not mean sampling free-form thoughts on a
timer.

## One semantic author, two opportunity sources

During chat, the existing CharacterInterior reply turn authors the concise
`PrivateTurnState` for that turn and may propose typed durable changes.  During
idle time, existing world-stimulus and reflection opportunities call the same
CharacterInterior role for private appraisal and experience transitions.

The scheduler may decide only that an opportunity is due.  It may not decide
that the character wants contact, wants a photo, feels an emotion, or must say
something.

## Durable inner state

There is deliberately no second generic `MindState` JSON blob.  Continuity uses
the existing typed projections:

- Appraisal and Affect for current interpretation and emotional residue;
- Aspiration for personally chosen longer-running tension or direction;
- PrivateImpression and MemoryCandidate for tentative readings and retention;
- Thread and Commitment for unfinished business and accepted obligations;
- Relationship for slow, source-bound relational state.

`PrivateTurnState` remains a concise per-turn audit and never authorizes a World
mutation by itself.

## Contact intention

An idle world-stimulus turn may freely open a source-bound Thread with
`kind=reply_reconsideration`, including role-selected importance, due time and
expiry.  This records “I want to revisit whether to contact them”; it does not
schedule or send a message.  When due, the proactive lane presents the durable
Thread to the same role, which chooses `now`, `later`, or `silent` again.

Technical failure must remain distinct from `silent`.  In particular,
`role_faculty_unavailable` and `required_tool_choice_unsupported` remain exact
failure codes instead of being collapsed into a generic authored exception.

## Media intention

An accepted expression may choose `consider_available_candidate`.  Existing
source-closed candidates remain usable.  If the proposal also selects an exact
reviewed lived-world source in `media_source_refs`, the media request runtime
may ask the existing LifeVisualEvidenceAuthor to compile a candidate before
media selection.

The source may be the settlement event or a ledger-proven alias presented in
the chat snapshot, such as its `ExperienceCommitted` authority or bound life
content descriptor.  Alias resolution follows typed ledger bindings only; no
text, “latest event” heuristic, or counterpart request can supply a scene.

`PrivateTurnState.attended_source_refs` remains audit-only.  Attention by itself
cannot compile a candidate; the accepted expression must carry the separate
source selection and that ref must be bound into durable proposal evidence.

CharacterInterior still makes the later media `select / no_op` choice.  Privacy,
relationship gates, daily limits, provider result, inspection, delivery and
receipt remain authoritative downstream.

## Cost and latency posture

- No periodic free-form model polling.
- One bounded role call only when a durable event/opportunity exists.
- Exact source identity, trigger terminal state and existing effect-once paths
  prevent re-authoring after restart.
- A request with no relevant visual source can still consume an existing media
  candidate; it is not made schema-invalid merely to force candidate creation.

This reduces idle cost but does not yet prove the product targets of CNY 0.03
per text turn or a 2-3 second first Beat.  Those remain real-provider and real-QQ
qualification gates.

## Explicitly incomplete

- Visible relationship commitments are not yet atomically bound to durable
  relationship stage/commitment state.
- Cross-turn offers and transfers do not yet have a typed actor/object/status
  projection, so subject/object continuity remains a known gap.
- The new media bridge has local restart/effect-once coverage but still needs a
  real Provider render followed by real QQ dispatch and terminal receipt.
- Natural proactive frequency, multi-day life continuity and 24-hour soak are
  not established by this slice.
