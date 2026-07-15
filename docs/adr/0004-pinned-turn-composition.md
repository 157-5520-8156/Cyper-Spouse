---
status: accepted
---

# Pinned Turn composes advisory before deliberation, never as a write authority

`WorldRuntime` will process one Observation through a Pinned Turn: it freezes a
complete ledger cursor, compiles deterministic Situation material and bounded
Inner Advisories from that cursor, then records the model result and Proposal
Audit.  Advisory output is available to the model but is never itself a World
Event, an acceptance decision, or a reason to reject a proposal.

The Pinned Turn ends at Proposal Audit for the first integration slice.  A
separate Acceptance module remains the only seam that can create state changes
or Actions.  If the cursor is stale at the audit write, the Pinned Turn is
discarded and rebuilt from a fresh cursor; it must not reuse the old Capsule or
Proposal as current authority.

## Considered Options

- Keep Context, Advisory, Deliberation and audit as independent helpers. This
  preserves unit tests but leaves `WorldRuntime.ingest()` unable to use
  emotion, relationship, memory or world material.
- Let advisory adapters write appraisal or affect events directly. This makes
  classifiers into behavioural rules and lets uncertain inference mutate World
  state without the main model's choice.
- Accept the Pinned Turn composition. It gives one cursor-consistent model
  input while retaining explicit Acceptance as the hard-constraint write seam.

## Consequences

Runtime code owns cursor pinning, retry and trace identity.  Resolver and
classifier failures are represented as unavailable/empty advisory material and
do not prevent the Observation record or force a scripted response.  Tests can
verify the whole composition through its small interface without reaching into
ledger reducers or model adapters.
