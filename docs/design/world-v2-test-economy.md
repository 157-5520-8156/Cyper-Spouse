# World v2 test-economy trace gate

`companion_daemon.world_v2.test_economy` is the Phase-8 mechanical cost and
latency verifier. It is a reader of immutable model-audit/replay evidence, not
another model router, ledger writer, or action executor.

The fixed CI profile is `test-economy-v1`:

- `chat`: at most one Flash call and no thinking;
- `expressive`: at most two calls (main plus structural recovery) with an
  explicit bounded thinking allowance;
- `world_action`: one Flash call;
- `deep_deliberation`: one thinking call with an explicit thinking-token cap;
- `quick_recovery`: one Flash call.

The profile contract also carries per-action caps, daily category caps,
proactive/media daily caps, warning/hard-stop thresholds, currency/effective
time, and whether paid Actions are permitted. A production profile can require
cost units in every trace and the verifier then accumulates daily category
costs and rejects both individual and daily cap breaches. `test-economy-v1`
has no paid-action authority and only accepts offline token estimates.

For `expressive`, a second call is valid only when the persisted attempt
lineage is `main_invalid` followed by `main_invalid_recovered`; two ordinary
main calls cannot be hidden under the two-call allowance.

Every trace record carries route class, tier, reason code, router version,
call/attempt identity, input/output tokens, thinking tokens, and a token
provenance. Missing token information is not a zero. In particular, a
`thinking` tier record missing `thinking_tokens` fails the gate. The current
frozen ledger audit has no thinking-token or usage-provenance field; the replay
extractor therefore emits `None`/`unknown` and fails closed until a provider
usage sidecar supplies them.

The historical audit also lacks a persisted semantic route class. The default
replay extractor marks Flash calls `unclassified`; it does not guess from a
model name. Such a trace fails the profile until composition provides a
route-class sidecar/authoritative classifier.

The command is:

```bash
companion-world-v2-test-economy --trace-json artifacts/world-v2-trace.json
```

The input has `model_calls` and `latency_samples` arrays. A zero exit code
means only that the fixed profile and supplied real-transport trace pass. The
report always separates `offline_in_process` timing from `real_transport`;
they are never aggregated together. P50/P95/P99 and hot/cold speedup are
exported per environment. Offline data yields
`real_network_slo_status: "not_measured"` and can never prove a production
network SLO. If any real sample is supplied, all required segments plus hot
and cold ingress samples are required; incomplete real evidence fails rather
than becoming a misleading “measured” status.

`require_paid_action_profile()` is intended for the Action-planning seam:
missing profiles and the fixed offline profile both reject a paid Action before
budget reservation or Action creation. Production must supply its own
provider-reported profile and category caps; monetary defaults are not placed
in domain code. The concrete v2 Media execution seam invokes it before a
positive render, inspection, or repair reservation; an existing effect-once
Action can still be recovered without re-authorizing a new paid request.
