# Private-self expression audit runner

`scripts/run_private_self_expression_audit.py` runs against a fresh isolated
World V2 database. It does not use the production database or QQ delivery.

## Pre-conversation Life Ecology evidence

The nested report contract is
`private-self-expression-preconversation-life-ecology.2`.

Each ten-minute preparation unit reports two separate observations:

- `clock_status`: the return value of the production-shaped clock tick;
- `ecology_status` and `ecology_reason_code`: the Life Ecology terminal result
  actually committed during that unit.

The runner reads the result from the unit's newly committed
`TriggerProcessCompleted` event. If immutable completion rows are not present
in the exported slice, it may use `life_ecology_schedule` only when that
projection changed between the unit's before/after replay snapshots. Merely
receiving `clock_status: observed_only` never proves that Life Ecology
accepted work.

`ecology_status` is a descriptive category:

- `accepted`
- `cooldown`
- `no_op`
- `technical_failure`
- `not_observed`
- `unknown`

The exact `ecology_runtime_outcome_ref` and normalized
`ecology_reason_code` remain in every unit, and the report includes status and
reason counts. `not_observed` means that the read-only evidence contained no
same-unit terminal result; it is not treated as silence or success.

`tick_statuses` remains present for readers of the older runner report, but
`tick_statuses_deprecated: true` and
`tick_statuses_semantics: legacy_clock_status_only` make its limited meaning
explicit. New consumers should read `units`.

The additional replay exports are read-only. They do not drain workers,
advance the clock, schedule retries, or otherwise add production work.

## Recall observability

The immutable expression audit reports the automatic attention channel and
the role model's optional deeper pull separately:

- `causal_chain.prefetch_presented` means one or more recorded prefetch hits
  were presented to a role-model call. An empty prefetch trace leaves this
  false. Presentation does not mean the character attended, believed,
  mentioned, or acted on any hit.
- `causal_chain.character_pull_selected` means the role model authored a
  `recall_request` and selected the one bounded deeper pull available for that
  turn.
- `summary.prefetch_presented_turn_count` and
  `summary.character_pull_selected_turn_count` count those two observations.

The older `character_recall_selected` and `character_recall_turn_count` fields
remain in the report for compatibility. They have the same meaning as the new
`character_pull_*` fields and never include automatic prefetch alone.

## Interaction stress fixtures

`--fixture` can also point at
`tests/world_v2/fixtures/private_self_expression_interaction_stress.json`.
That fixture exercises three presentation boundaries without contacting QQ:

- `fragments` submits several same-user sender bubbles through
  `QQC2CHost.inbound_fragment`; the production ingress store, rather than the
  runner, decides whether they form one coalesced turn.
- turns sharing an `overlap_group` launch at their recorded
  `launch_offset_ms`, so a later inbound can arrive while an earlier provider
  generation is still running.
- the final interjection gives existing Expression reconsideration/cancellation
  machinery an opportunity to stop an unsent multi-beat tail. The runner does
  not invent a second cancellation path.

The optional
`tests/world_v2/fixtures/private_self_expression_natural.json` contains an
ordinary multi-turn conversation without instructions such as “do not ask
questions” or “do not sound like customer service”. It is intended for
descriptive real-provider observation, so corrective wording in the user
fixture does not contaminate the behavior being observed.

Existing report readers remain compatible with
`private-self-expression-real-audit-run.2`. The runner adds the optional
top-level `naturalness_readiness` description; new runtime rows only add
`source_event_ids`, `user_messages`, `ingress_mode`, `overlap_group`, and
`fragment_statuses`; the singular `source_event_id` and `user_text` remain.

## Role-provider latency evidence

`ingress_to_first_role_provider` ends at exact request emission: the production
OpenAI-compatible client emits the marker immediately before its
`client.post(...)` call, after local capacity admission, circuit checks and
payload construction. It is API-external overhead, not time to first token.
Offline fakes and dry-run adapters have no HTTP seam, so only those paths retain
an adapter-entry fallback sample; that fallback must not be described as a
transport observation.

Every foreground provider `client.post(...)` receives its own provider-call
identity and is closed at that same transport's return boundary. This includes
semantic Recall embedding and same-turn advisory work during Context
preparation, a failed role primary followed by fallback, and any
source-review/reselection call inside the foreground candidate.
`model_completion` remains the duration of the first closed non-streaming
role-cognition request for backward-compatible dashboards; an earlier
embedding/advisory call cannot claim that field, and the outer failover wrapper
cannot close it. `role_provider_total` is the union of role-cognition provider
intervals. `foreground_provider_total` is the union of every foreground
provider interval, including auxiliary embedding/advisory work. Overlapping
hedges are counted once in either total.

When the first Action becomes visible and every foreground provider interval
is closed, `api_external_overhead` is computed as
`ingress_to_visible - union(provider intervals)`. The 500 ms health objective
uses this whole-turn value, which includes preparation, gaps between calls,
validation, commit and dispatch without misclassifying provider wait as local
work. An open or unmatched provider span leaves this objective unmeasured; it
is never filled from adapter or wrapper timing.

The current completion API does not expose a first-token callback, so
`latency_evidence` reports `ttft_status: unavailable` with reason
`non_streaming_completion_api`; absence of a `model_ttft` sample must not be
replaced with request-emission or adapter-entry timing.

Foreground cognition freezes role entry/completion markers when it finishes,
while Action dispatch, receipt, and visibility remain joinable. A cancelled
provider task that suppresses cancellation therefore cannot append a late
completion to the user-visible turn after background processing begins.

For a reliability-only control run, keeping
`--preconversation-life-ecology-units 0` is useful. Such a run cannot by
itself evaluate whether the character naturally associates the conversation
with her own life: its report may contain zero committed Experiences, zero
MemoryCandidates, and only empty recall hits. A naturalness run should use
non-zero preparation and read the reported post-preparation counts before
interpreting an absence of self-reference. Life Ecology remains free to
decline events, so a requested unit count is not evidence that personal
material actually existed.

The top-level `naturalness_readiness` object makes that interpretation
machine-readable without becoming a pass/fail rule:

- `assessment` is always `reliability_only` when the requested preconversation
  unit count is zero, even if the conversation happened to look natural.
- `source_bound_self_material` counts only committed, authority-backed
  Experiences and active, source-bound MemoryCandidates. Pending memory is
  reported separately and does not prove that recall could retrieve it.
- `current_self_state` reports whether the immutable cold-replay projection
  contains inputs from which the ordinary Capsule compiler can build that
  view. Its evidence basis is explicitly
  `immutable_projection_inputs_not_provider_delivery`: the retained audit does
  not expose provider request bodies and therefore does not pretend to prove
  that a particular request presented the view.
- `prior_interaction_appraisal` binds every scenario Observation before the
  final turn to its `interaction_appraisal` trigger and distinguishes terminal,
  pending, missing, and unknown evidence.

`reporting_policy` remains
`descriptive_only_not_an_acceptance_rule`, and
`production_behavior_gate` is always `false`. Neither the aggregate assessment
nor any reason code may feed prompts, scheduling, retries, Acceptance, or other
production behavior. A nonzero preheat request can still be
`not_ready_for_naturalness_observation` or `indeterminate`; the model is free
to decline life events, and background appraisals may still be unfinished.

## Surface question counts

`surface_question_mark_count` only counts visible `?`/`？` marks. It cannot
infer conversational intent, whether a question was appropriate, or whether
the character behaved like an assistant. The immutable summary records
`surface_question_counting_policy:
surface_question_marks_only_descriptive`, alongside the existing
`reporting_policy: descriptive_only_not_an_acceptance_rule`. Neither value is
an acceptance gate, a production signal, or prompt input.
