# ADR 0015: Advisory Text Endpoint and Expression Unit Stream

- Status: Accepted
- Date: 2026-08-02

## Context

A fixed packet-coalescing delay cannot distinguish a complete short bubble from
the first bubble of a longer thought. Increasing that delay globally makes
ordinary exchanges feel slow; shortening it globally causes premature turns
and repeated generation. Independently, waiting for an entire multi-beat JSON
completion before authorizing its first beat wastes the provider's early-token
latency.

Both problems sit next to character behavior but are not behavior authority.
The controlled-high-variance boundary in ADR 0010 therefore remains binding.

## Decision

QQ transport retains a small packet-coalescing floor. A separate, advisory
endpoint model may estimate only the probability that the same user will add
another text bubble soon. Its evidence is bounded provider-local state:

- the current uncommitted bubble batch;
- the user's observed bubble-gap distribution;
- current typing presence;
- whether this is already a multi-bubble burst;
- recent user and positively delivered character-text lengths; and
- recent paired user/character text-bubble counts, excluding typing, media,
  failed sends and merely authorized Actions.

The estimate may size one bounded listening opportunity. It cannot decide the
character's timing choice, stance, wording, message count, silence, or any
World mutation. Failure is a fast transport fallback. New input invalidates the
older estimate. At most one endpoint provider request may remain in flight; a
listener timeout does not cancel and poison the shared serial local-model
capacity, and no second request queues behind it.

For visible expression, `stream` is distinct from the historical two-author
Expression Episode `on` mode. One role-author request returns the
`expression-units.1` wire envelope. For an immediate expression, the first
field contains one complete ExpressionDraft with one visible Beat and may also
contain one character-chosen leading typing Beat; later fields contain any
additional model-chosen visible Beats. Silent and deferred choices remain
complete decisions with no continuation. The wire shape does not tell the
model how many messages to produce or what to say.

The first unit may be authorized only after the ordinary proposal, source,
permission, budget, Action, cursor and receipt checks. Remaining units reuse
the same provider response and the existing Expression Episode lifecycle. A
tail is never an independent winner. Provider arrival advances a process
attention epoch before batching, so an older head that finishes validation
later cannot register a stale tail. Cancellation gets only a tiny foreground
grace; a cancellation-suppressing transport is observed and resource-bounded
in the background rather than blocking the new inbound. A newer ledger cursor
also rejects an older tail, and only the latest cursor-pinned Beat may reach dispatch.
Head and tail have distinct semantic model-result identities bound to one
physical provider-call parent. The parent records completed, cancelled, or
unresolved terminal state; completed calls retain the full response hash and
provider-reported usage rather than inventing per-unit token counts.
After process restart, a missing process-local tail is completed without
regeneration; an already delivered head is never replayed merely to recover its
tail.

## Consequences

- A complete short message can begin World cognition after the transport floor
  instead of a fixed human-turn timeout.
- Likely continuation can still be collected using semantic, personal cadence
  and typing evidence without turning punctuation or keywords into behavior
  rules.
- Multi-bubble replies can expose the first validated unit before the same
  provider request finishes, while preserving one author identity and
  effect-once dispatch.
- True first-token latency is observable separately from full model completion.
- Endpoint uncertainty may still choose the fast fallback; it can reduce but
  cannot eliminate the inherent trade-off between premature interruption and
  delayed response.
