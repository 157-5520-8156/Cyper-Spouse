# ADR 0017: Selective Proof-Carrying Source Review

- Status: Accepted as staging opt-in; production default and qualification incomplete
- Date: 2026-08-10

## Context

Every visible external proposition needs source closure, but the current chat
path often pays for an Inventory call and a full V7 review over the same
candidate.  The full packet is large and the second provider round trip sits on
the first-visible-Beat critical path.  This is safe but wasteful for the common
case in which the character expresses only immediate private state,
non-assertive content, or a world-unbound generalization.

An independent reviewer remains stronger in principle, but the measured GPT and
Qwen routes put a second vendor on the critical path and failed the product's
latency/cost objective.  The selected deployment therefore makes the tradeoff
explicit: one DeepSeek Flash checkpoint owns cloud semantics, while a separate
runtime of that checkpoint performs a compact adversarial source verdict.  This
is correlated review, not independent semantic authority. JSON Schema proves
wire shape; it does not turn the correlated verdict into mathematical proof.

The text-turn performance targets are:

- first user-visible complete Beat: p50 at most 2 seconds and p95 at most
  3 seconds on the pinned staging route;
- aggregate text-turn provider cost: at most CNY 0.03 per accepted inbound
  message, reported as both mean and p95 over the qualification corpus;
- no relaxation of source closure, privacy, consent, Action authorization,
  effect-once, CAS, receipt, replay, or same-character correction.

Media generation is outside the text-turn cost target.  These values are gates,
not current capability claims.

## Decision

Introduce one deep selective source-authority boundary behind the existing
`review_expression_with_candidate_external_coverage(...)` interface.  Callers
do not select reviewers, fast paths, or fallbacks themselves.

The boundary accepts a pinned authored candidate, exact visible Beat surfaces,
a compact source-capability manifest, candidate hash, source revision, and
contract digest.  It returns one typed terminal result containing the review
decision, provider usage, route, and exhaustiveness evidence.

It uses these routes in order:

1. **Mechanical empty-surface proof.** A candidate with no visible Beat text,
   no visible non-text factual payload, no `WorldClaim`, and no effect-bearing
   factual proposal needs no semantic provider call.  This proves absence by
   structure, not by trusting the author.
2. **Selective correlated guard.** A separate DeepSeek Flash runtime returns an
   exhaustive ordered verdict over the exact complete visible Beat surfaces.
   The provider classifies whole Beats and typed subject/source coordinates;
   the host derives locators, requires one ordered result per Beat, resolves
   only pinned source indexes, and rejects actor swaps. It never authors replacement
   dialogue. Health and lineage must record `correlated_same_checkpoint`; the
   route must never be reported as independent.
3. **Typed failure and same-character correction.** Missing capability evidence,
   provider failure, unsupported contract, or invalid wire is a technical
   terminal for the compact guard. CharacterInterior may give the same
   Character author one bounded correction with the exact failure; there is no
   implicit GPT/Qwen/full-V7 availability fallback and no local fallback prose.

An Inventory-only positive can never authorize visible prose.  Coverage may
authorize the source-free case only when it returns an exhaustive typed verdict
for every independently inventoried coordinate.  Declared `WorldClaim`
dimensions retain their canonical deterministic checks and, where required,
their dedicated review.

Durable verdict reuse is deliberately not part of the first production slice.
Before adding it, cache entries must be immutable and keyed by candidate hash,
source revision, actor/purpose, privacy scope, source-capability digest,
reviewer semantic authority, and exact contract/schema digest. Availability
failures and technical failures must never be cached as semantic verdicts.

For multi-Beat output, review and release may operate on a complete Beat when
that Beat is independently parseable and its evidence surface is closed.  A
later Beat cannot retroactively authorize or alter an already accepted Beat;
invalid tail state terminalizes the tail and physical call truthfully without
rewriting the released head.

Provider selection is a deployment concern inside this boundary. The earlier
8/10 DeepSeek result used an over-complex prompt and an incompatible schema; it
was not evidence of a Flash capability limit. The retained provider schema is
flat and limited to DeepSeek's strict-tool subset. After adding explicit actor
swap, mixed-clause, completed-episode and polarity examples, the exact direct
`deepseek-v4-flash` route returned 100/100 valid adversarial verdicts. The same
checkpoint remains the Character author, so this evidence selects a cheap guard
transport but deliberately does not establish independent review.

## Qualification

Before enabling the selective route by default, retain exact evidence for the
installed schema digests and run the fixed adversarial corpus through the
actual endpoint.  Report first-attempt schema validity, false support, false
rejection, p50/p95/p99 latency, tokens, CNY cost, timeout/429/5xx rate, restart
reuse, and cold replay.  At least 100 calls are required for route selection;
this sample is not statistical proof of the 99.9% design target.

The 2-3 second and CNY 0.03 targets are met only when measured on an isolated
staging path that includes author, review, acceptance, dispatch, and first
visible receipt.  Local tests, mock transports, short samples, or first-token
latency alone do not satisfy the gate.  Real QQ experience and 24-hour soak
remain manual release gates.

The first slice performs a fresh guard call for each newly authored candidate;
restart-stable verdict reuse remains an explicit cost optimization and
qualification item rather than an implemented capability claim.

The retained 2026-08-10 route-selection audit for schema digest
`347069477180408b262fd4ac7da64341c07b8456d18ad6bd6ab7dee7f9ea78e7`
observed 100/100 first-attempt valid verdicts across ten adversarial coordinates.
Reviewer-only latency was p50 1.049s / p95 1.387s / p99 1.530s. These numbers
select the Flash-only staging route; they do not yet meet or prove the
end-to-end latency/cost gates.

## Rejected alternatives

- **Trust `world_claims=[]`:** lets the author omit an invented fact from its
  declaration and is therefore not proof.
- **Embedding similarity as final review:** useful for retrieval, but cannot
  decide negation, actor, time, status, or semantic entailment.
- **Rules or keyword classification:** would be incomplete and risks turning
  the host into a semantic author.
- **Always run Inventory and full V7 in parallel:** bounds latency by the slower
  call but keeps the cost of both and can still require a third enriched pass.
- **GPT/Qwen on every visible Beat:** provides checkpoint diversity but missed
  the latency/cost objective and introduces extra provider availability.
- **Start primary and reserve reviewers together:** improves tails by paying
  duplicate cost and creates avoidable semantic races; reserve activation stays
  serial after a typed primary failure.
