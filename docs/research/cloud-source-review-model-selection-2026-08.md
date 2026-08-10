# Cloud model selection for source review (2026-08-10)

Status: superseded deployment recommendation; retained comparison research;
`manual_only`; `qualification_incomplete`.

## Decision

The original recommendation below preferred Qwen Plus for semantic-provider
independence. It is superseded by ADR 0017 for the latency-sensitive text-chat
path: use a separate `deepseek-v4-flash` runtime with the compact
`visible-beat-source-verdict.1` strict tool. The Character author and guard are
the same checkpoint and must be reported as `correlated_same_checkpoint`, never
as independent authority. There is no implicit GPT/Qwen fallback on that path.

The retained exact-schema audit observed 100/100 first-attempt valid verdicts;
reviewer-only latency was p50 1.049s and p95 1.387s. This selects the staging
route but is not release approval. End-to-end first Beat, real QQ receipt,
multi-turn cost, and 24-hour operation remain unqualified.

The remainder of this document records the earlier independent-provider
comparison and may inform a future reserve lane; it is not the active default.

## Why this is the least speculative choice

Girl-Agent's installed evidence registry already records:

- OpenRouter `qwen/qwen-plus`: 13/13 successful exact-contract source-review
  samples for `source-closure-review.7` and
  `report-relative-entailment-adjudication.3`.
- Direct OpenAI `gpt-4.1-mini`: 13/16 successful samples for the same active
  contracts.
- The semantic-authority registry already treats OpenRouter `qwen/qwen-plus`
  and direct DashScope `qwen-plus` as the same semantic checkpoint authority.

The observed operational failures were largely transport/availability failures:
OpenRouter HTTP 403/429 and reserve-route timeout/suppression. Moving the already
audited Qwen checkpoint to its first-party endpoint addresses that evidence
without simultaneously changing the semantic model.

The repository's earlier direct-DashScope probe also found `qwen-turbo` useful
for cheap inventory extraction but unstable on exact spans (roughly 40% invalid
in the recorded short-text sample). Therefore `qwen-turbo` should not be the
final source-closure authority.

## Official capability facts

### Qwen Plus

Alibaba documents Qwen Plus as the balance of quality, speed, and cost for most
applications. Qwen Plus series supports Function Calling and structured output.
The current `qwen-plus` alias is equivalent to `qwen-plus-2025-12-01`; pinning the
snapshot avoids silent model drift.

For mainland China, the official OpenAI-compatible endpoint is:

`https://dashscope.aliyuncs.com/compatible-mode/v1`

Official pricing for `qwen-plus-2025-12-01` below 128K input is CNY 0.8/M input
tokens, CNY 2/M non-thinking output tokens, and CNY 8/M thinking output tokens.
The source-review lane should disable thinking: this is a bounded classification
and evidence-closure task, not open-ended deliberation.

Sources:

- [Alibaba Model Studio overview](https://help.aliyun.com/en/model-studio/what-is-model-studio)
- [Alibaba model pricing](https://help.aliyun.com/en/model-studio/model-pricing)
- [Qwen structured output](https://help.aliyun.com/en/model-studio/qwen-structured-output)
- [Qwen Function Calling](https://help.aliyun.com/zh/model-studio/qwen-function-calling)
- [OpenAI-compatible Chat Completions](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)

### Qwen3.7 Plus challenger

Alibaba describes `qwen3.7-plus` as a flagship Qwen model with a 1M-token
context, Function Calling, and structured output. Official pricing below 256K is
CNY 2/M input and CNY 8/M output in either non-thinking or thinking mode.

It is a credible challenger, but generic capability claims do not prove the
Girl-Agent V7 source-closure semantics, exact visible spans, report-relative
decisions, or first-attempt schema reliability. It should win an exact-contract
comparison before replacing the already audited Qwen Plus checkpoint.

Sources:

- [Qwen3.7 model capabilities](https://help.aliyun.com/en/model-studio/vision-model/)
- [Alibaba model pricing](https://help.aliyun.com/en/model-studio/model-pricing)

### OpenAI reserve

OpenAI documents `gpt-5.4-mini` as a faster high-volume model with Function
Calling and Structured Outputs. It has a 400K context window and costs USD
0.75/M input tokens, USD 0.075/M cached input tokens, and USD 4.50/M output
tokens. The pinned snapshot is `gpt-5.4-mini-2026-03-17`.

This is the strongest reserve candidate because it provides supplier diversity
from both DeepSeek (character author) and Alibaba (primary reviewer). However,
the current project evidence qualifies `gpt-4.1-mini`, not GPT-5.4 mini, for the
active source-review contracts. The newer model remains a challenger until the
same audit passes through the actual proxy route.

Sources:

- [GPT-5.4 mini model page](https://developers.openai.com/api/docs/models/gpt-5.4-mini)
- [OpenAI Structured Outputs reference](https://platform.openai.com/docs/api-reference/responses-streaming/response/output_item)

## Why the other obvious candidates are not the first choice

### DeepSeek V4

DeepSeek V4 Flash and Pro are inexpensive and officially support JSON output,
tool calls, required tool choice, and beta strict tool schemas. But the current
character author is already DeepSeek. Using DeepSeek as the only reviewer would
remove semantic-supplier independence; it is useful as an author, not the first
choice for this independent truth boundary. DeepSeek also warns that JSON mode
may occasionally return empty content.

Sources:

- [DeepSeek models and pricing](https://api-docs.deepseek.com/quick_start/pricing)
- [DeepSeek JSON output](https://api-docs.deepseek.com/guides/json_mode/)
- [DeepSeek tool calls](https://api-docs.deepseek.com/guides/tool_calls)

### Kimi

Kimi K2.6 supports JSON mode/tool calls and a 256K context, and the machine has
a Kimi API key. It is a useful future domestic reserve candidate. It is not yet
release-pinned in Girl-Agent's semantic-authority or strict-contract registries,
and the existing Kimi author probe ended with an unresolved stream tail. That
does not disqualify non-stream review, but it means Kimi is not the evidence-led
first choice.

Sources:

- [Kimi API concepts](https://platform.kimi.com/docs/introduction)
- [Kimi Chat Completions](https://platform.kimi.com/docs/api/chat)

### GLM

GLM models support structured output, but the official Function Calling guide
documents `tool_choice` as `auto` only. Girl-Agent's hard-boundary lanes need
forced/strict output behavior and release-pinned evidence. No GLM key or project
audit is currently installed, so adopting it now would add integration work and
uncertainty without addressing a demonstrated gap better than direct Qwen.

Sources:

- [GLM-5 model page](https://docs.bigmodel.cn/cn/guide/models/text/glm-5)
- [GLM Function Calling](https://docs.bigmodel.cn/cn/guide/capabilities/function-calling)

### OpenRouter

OpenRouter remains useful as an emergency transport aggregator, but it should
not be the primary path for a latency-sensitive hard boundary. Girl-Agent has
already observed 403/429 failures through that route. A first-party DashScope
connection removes one routing and quota layer while keeping the same Qwen
semantic checkpoint.

## Required implementation before an A/B run

The current source-review composition cannot simply point the existing “local
review endpoint” setting at DashScope: that branch still constructs the reviewer
with the OpenRouter key and labels it as a local configuration authority. Add a
distinct direct-DashScope cloud lane instead of overloading the local seam.

The lane must bind:

1. exact first-party endpoint;
2. Qwen API key, never OpenRouter credentials;
3. pinned snapshot model id;
4. non-thinking mode;
5. installed strict schema digest and semantic-authority id;
6. bounded timeout, provider usage, response hash, and typed failure status;
7. serial reserve activation only after the primary terminally fails.

## Qualification plan

Do not compare models with general chat prompts. Replay the exact installed
contracts and parser against a fixed corpus containing:

- supported sourced facts;
- unsupported invented external experience;
- negation and subject swaps;
- temporal/status mismatches;
- exact current-user-report uptake;
- mixed visible spans and multi-bubble output;
- valid `no external proposition` cases;
- malformed and truncated provider output.

Minimum gate before changing the primary lane:

- 100 calls per model/contract route;
- at least 99% first-attempt schema-valid output during selection, with the
  design target remaining 99.9% for release;
- zero false support on the adversarial hard-boundary set;
- report p50/p95/p99, 429/5xx/timeout rate, input/output tokens, and cost;
- restart/cold-replay does not repeat a completed review;
- user-visible technical terminal remains distinct from role silence.

Compare:

1. direct `qwen-plus-2025-12-01` non-thinking (deployment candidate);
2. direct `qwen3.7-plus-2026-05-26` non-thinking (quality challenger);
3. direct `gpt-5.4-mini-2026-03-17`, reasoning `none` (reserve challenger);
4. current OpenRouter Qwen route as the operational baseline only.

Until this run passes, retain `manual_only / qualification_incomplete` and do
not describe the selected route as production-qualified.
