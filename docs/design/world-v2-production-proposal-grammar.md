# World v2 production proposal grammar

`DecisionProposal v1` is a broad inert envelope. It is not the production
mutation grammar. Production only allows an LLM proposal into the ledger when
its composition lane names one closed specialized compiler, accepted manifest,
and reverse-verification boundary.

The executable catalogue is
`src/companion_daemon/world_v2/production_proposal_grammar.py`.

| Deliberation lane | Allowed Decision change | Actions | Specialized authority chain |
| --- | --- | --- | --- |
| `chat_reply` | exactly one `expression_plan_transition/accept` | expression `reply`, `followup`, or `proactive_message`, all causally bound to that change | `derive_expression_plan_material.1` → `expression-plan-manifest.1` → `expression-plan-acceptance.1` |
| `interaction_appraisal` | zero changes (no-change), or one `appraisal_transition/activate` | none | `appraisal-proposal-compiler.1` → `appraisal-acceptance-manifest.1` → `appraisal-acceptance-runtime.1` |
| `settled_world_appraisal` | zero changes (no-change), or one `appraisal_transition/activate` | none | same appraisal chain |
| `affect` | zero changes (no-change), or one `affect_transition/open` | none | `affect-proposal-compiler.1` → `affect-acceptance-manifest.1` → `affect-acceptance-runtime.1` |
| `outcome` | exactly one `outcome_settlement/settle` | none | `outcome-proposal-compiler.1` → `outcome-acceptance-manifest.1` → `outcome-acceptance-runtime.1` |

`MinimalProposal` is allowed only in `chat_reply`, where the existing minimal
reply manifest remains the sole accepted-effect path. `ContinuationProposal`
is never model-reachable in production. Media continuation is a mechanical,
source-bound state machine; Fact draft, ActivityPlan and reply-later each use
their own non-`DecisionProposal` input grammar.

## Gate

`compose_production_deliberation()` installs a grammar for every production
LLM lane. `assert_production_proposal_grammar_coverage()` verifies that the
catalogue has exactly those five lanes and resolves every compiler, manifest,
and reverse-verifier reference to a real installed implementation object. The
catalogue is an immutable mapping; replacing its public view fails closed. The
corresponding test also statically parses `production_turn_application.py`:
direct or aliased `Deliberation` construction, or a missing registered lane,
fails CI.

Adding a typed change to the generic envelope does not make it production
reachable. To add one, first provide a specialized compiler, immutable
manifest, and reverse verifier, then add the lane to this document and the
executable catalogue in the same change.
