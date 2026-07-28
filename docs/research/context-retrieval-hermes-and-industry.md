# Context Retrieval for Girl-Agent: Hermes and Primary-Source Patterns

Updated: 2026-07-28

## Question

How should Girl-Agent assemble context intelligently without loading a large
World dump on every turn, losing emotional continuity, adding a second reply
path, or turning retrieval rules into character behaviour?

This note examines the current Hermes Agent architecture and four
primary-source reference points: Letta, Generative Agents, Graphiti/Zep, and
LongMemEval.

## Bottom line

Hermes Agent is useful as an **operational pattern**, not as a complete memory
algorithm to copy. Its current core keeps a small always-present memory,
prefetches external memory using the cleaned user message, writes completed
turns in the background, gives memory failures a bounded non-fatal path, and
allows only one external memory provider. The actual retrieval quality depends
on the selected provider; Hermes itself does not currently synthesize a rich
state-aware retrieval query.

The best fit for Girl-Agent is therefore:

1. keep a small, source-bound **hot context** always visible;
2. run **hybrid associative recall** in parallel from a bounded attention cue;
3. keep time, entity links, privacy, and validity as structured filters rather
   than serializing the entire World into an embedding query;
4. return a small, diverse evidence set to the **same inbound character
   deliberation**;
5. let the character model use, reinterpret, ignore, or deliberately deepen
   that recall;
6. degrade to hot context plus lexical/structured recall when semantic recall
   fails, never to a silent turn;
7. consolidate memories asynchronously, outside visible reply latency.

This preserves Girl-Agent's existing memory investment. It does not add a
fact-check bypass or a second response generator.

## What Hermes Agent actually does

### Small always-on core

Hermes's built-in `MEMORY.md` and `USER.md` are intentionally bounded to about
2,200 and 1,375 characters. They are injected into the system prompt as a
frozen session snapshot. Mid-session writes persist immediately but do not
change the snapshot, explicitly to preserve prefix-cache performance.
([Hermes persistent-memory documentation](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md))

This resembles hot context, but it is aimed at a tool-using personal agent. It
does not model Girl-Agent's live affect, relationship transition, current
activity, unresolved conversational tension, or momentary attention.

### External recall is a lifecycle, not one retrieval design

Hermes defines a provider interface with these relevant hooks:

- `system_prompt_block()` for static provider context;
- `prefetch(query)` before a turn;
- `queue_prefetch(query)` to prepare a later turn;
- `sync_turn(...)` after a completed turn;
- `on_pre_compress(...)` and session-end hooks for consolidation;
- optional memory tools for model-directed search and writes.

The interface explicitly says prefetch should be fast and background-capable,
and completed-turn persistence should be non-blocking.
([Hermes `MemoryProvider`](https://github.com/NousResearch/hermes-agent/blob/main/agent/memory_provider.py))

The manager permits one external provider alongside built-in memory to prevent
schema bloat and conflicting memory backends. It strips skill prompt
scaffolding, but otherwise sends the cleaned user text directly as the
retrieval query. External prefetch runs in a daemon thread under a timeout; a
timeout yields no recalled context for that turn, and a still-running call is
not duplicated. Post-turn writes run on a serialized background worker, and
provider failures are logged without holding the conversation open.
([Hermes `MemoryManager`](https://github.com/NousResearch/hermes-agent/blob/main/agent/memory_manager.py))

Hermes providers differ substantially. For example, its Hindsight integration
supports automatic full-turn retention, automatic recall, hybrid
context-plus-tools mode, knowledge-graph/entity retrieval, and asynchronous
retention. Other providers use vector search, BM25 plus reranking, FTS5, or
hierarchical retrieval.
([Hermes memory-provider documentation](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory-providers.md))

**Implication:** “Use Hermes's approach” should mean adopting the lifecycle and
failure isolation. It should not mean using raw user text as Girl-Agent's only
query or importing one of Hermes's provider stores as a second source of
truth.

## Other primary-source approaches

### Letta: explicit context tiers and model-directed memory

Letta separates:

- memory blocks that are always present in context and can be agent-managed;
- recent messages in the context window;
- semantically searched archival memory available on demand;
- persisted old messages that remain retrievable after compaction.

Its documentation explicitly recommends memory blocks for small, important or
frequently changing working state, and archival memory for less important
long-term material. Archival memory is a semantic database queried through
tools; conversation search retrieves the historical messages themselves.
([stateful agents](https://docs.letta.com/v1-sdk/concepts/stateful-agents),
[memory blocks](https://docs.letta.com/v1-sdk/memory/memory-blocks),
[archival memory](https://docs.letta.com/v1-sdk/memory/archival-memory),
[context hierarchy](https://docs.letta.com/v1-sdk/memory/context-hierarchy))

Letta preserves recent messages while summarizing older ones when context
fills. Its default sliding-window compaction uses a separate smaller model,
while self-compaction modes retain a stable prompt prefix for cache hits.
([Letta compaction](https://docs.letta.com/v1-sdk/messages/compaction))

**Useful for Girl-Agent:** the hot/cold separation and character-directed
deeper retrieval.

**Do not copy literally:** letting the model overwrite authoritative memory
blocks would conflict with the World ledger and source closure. Girl-Agent's
hot block should be a rebuildable projection, not a mutable truth store.

### Generative Agents: associative accessibility and reflection

Generative Agents stores observations, plans, and reflections in a
timestamped memory stream. Retrieval combines:

- recency of access;
- model-assigned importance;
- embedding relevance to the current situation.

The three normalized scores are summed, and the highest-ranked memories that
fit the context window are supplied to the model. Reflection is triggered by
accumulated importance, uses recent records to generate salient questions,
retrieves evidence for those questions, and stores higher-level insights with
pointers to supporting records.
([Generative Agents, sections 4.1–4.2](https://ar5iv.labs.arxiv.org/html/2304.03442))

The controlled evaluation found the full memory/reflection/planning
architecture more believable than each ablation. The paper also reports
important failures: missed retrieval, fabricated embellishment, and overly
formal model speech. Its 25-agent two-day simulation cost thousands of dollars
and took multiple days, so the architecture is evidence for believability but
not evidence that synchronous LLM reflection is affordable for every chat
turn.
([evaluation and limitations](https://ar5iv.labs.arxiv.org/html/2304.03442))

**Useful for Girl-Agent:** affect, current situation, recency, importance, and
semantic relatedness should influence what becomes accessible, without
dictating what the character says.

**Do not copy literally:** importance scoring and reflection calls belong at
memory formation/consolidation time, not on a “早” reply's critical path.
Reflections must remain source-linked, defeasible interpretations.

### Graphiti/Zep: temporal provenance and hybrid retrieval

Graphiti represents episodes as the raw provenance stream, derives entities
and relationships, and gives facts validity windows so superseded information
remains historically queryable. Retrieval combines semantic embeddings,
keyword/BM25 search, and graph traversal; query-time retrieval does not require
LLM summarization.
([Graphiti repository](https://github.com/getzep/graphiti))

The Zep paper describes bi-temporal fact tracking and reports, on its
LongMemEval setup, about 1.6k average context tokens and 2.58–3.20 seconds
end-to-end latency versus 115k tokens and 28.9–31.3 seconds for full-context
baselines. It also reports regressions on some single-session assistant
questions, so graph retrieval is not universally better. The authors are also
the system vendor; these figures should guide engineering hypotheses, not be
treated as independent proof.
([Zep temporal knowledge-graph paper](https://arxiv.org/html/2501.13956))

**Useful for Girl-Agent:** validity intervals, raw-event provenance, structured
links, and lexical+dense+temporal candidate generation match the event ledger.

**Do not copy literally:** a separate graph database and its extracted facts
would create another authority. The graph/index should remain a disposable
projection of the ledger.

### LongMemEval: index and query shape matter

LongMemEval separates memory design into indexing, retrieval, and reading. Its
experiments motivate session decomposition, fact-augmented index keys, and
time-aware query expansion rather than relying on one raw-vector search over a
full transcript. It evaluates information extraction, multi-session reasoning,
temporal reasoning, knowledge updates, and abstention.
([ICLR 2025 paper](https://proceedings.iclr.cc/paper_files/paper/2025/hash/d813d324dbf0598bbdc9c8e79740ed01-Abstract-Conference.html),
[official experiment repository](https://github.com/xiaowu0162/LongMemEval))

**Useful for Girl-Agent:** store source text as the value, but enrich the
rebuildable retrieval representation with source-bound episodic summaries,
entities, time, and memory kind. Temporal expressions should narrow the search
scope through structured timestamps.

**Do not copy literally:** query expansion generated by another LLM on every
turn would add latency and a hidden interpretive authority. Girl-Agent already
has typed state and can construct bounded selectors without a new model call.

## Comparative fit

| System | Always-on context | Recall | Time/episode model | Consolidation | Failure/cost lesson | Fit for Girl-Agent |
| --- | --- | --- | --- | --- | --- | --- |
| Hermes | Bounded frozen core | Provider prefetch plus tools | Provider-dependent | Background turn sync and hooks | Timeout/skip is non-fatal; stable prefix | Adopt lifecycle and isolation |
| Letta | Editable memory blocks | Semantic archive and conversation search | Persisted messages | Sliding-window summaries | Tier by importance/size | Adopt tiers, keep ledger authoritative |
| Generative Agents | Current situation and plan | Recency + importance + relevance | Timestamped memory stream | Recursive source-linked reflection | Believable but expensive; retrieval errors remain | Best believability evidence |
| Graphiti/Zep | Query-assembled graph context | Dense + BM25 + graph | Bi-temporal facts and raw episodes | Incremental graph extraction | Small context and low retrieval latency; ingestion is heavier | Adopt temporal/provenance shape |
| LongMemEval | Not prescribed | Retrieval over enriched keys | Time-aware query expansion | Not a runtime design | Query/index shape materially affects recall | Use as evaluation framework |

## Audit of Girl-Agent's present seam

Girl-Agent already has most of the right structural pieces:

- a cursor-pinned, source-bound recall corpus;
- episodic, semantic, and reflective document kinds;
- lexical, dense, temporal, and structured scores;
- cached document embeddings and one query embedding;
- a 6,000-byte recall-result budget;
- parallel automatic prefetch with a 0.3-second first-pass join;
- an optional character-requested deeper recall;
- recorded retrieval traces, index version, cursor, match channels, and
  embedding degradation;
- lexical/structured fallback when the semantic provider is unavailable.

The immediate bug is narrower and concrete:

- `ledger_context_resolver._recall_attention_cue()` serializes the new
  observation, affect source references, and nested situation JSON, then clips
  at 2,048 characters;
- `CharacterRecallRequest` and `RecallQuery` accept at most 1,024 characters;
- therefore a valid rich World state can fail validation before retrieval,
  causing the resolver to discard the recall sidecar for that turn.

This is an interface-budget mismatch, not evidence that the memory system
should be removed. Opaque event/source references inside embedding text also
spend query budget without adding useful semantics; they belong in structured
bindings and audit traces.

Relevant code:

- `src/companion_daemon/world_v2/ledger_context_resolver.py`
- `src/companion_daemon/world_v2/recall_runtime.py`
- `src/companion_daemon/world_v2/recall_index.py`
- `src/companion_daemon/world_v2/chat_model_deliberation_adapter.py`

## Recommended unified inbound architecture

### 1. Hot context is always present

Compile a compact, source-bound projection containing:

- character core and current self-state;
- active affect/appraisal, including target and cause;
- relationship state and recent relationship changes;
- current activity, availability, attention, and local time;
- a short verbatim recent-dialogue tail;
- unresolved threads, commitments, interruption state, and the new
  Observation.

This is not a behavioural instruction. For “早”, it is what lets the same
character model see whether she is cheerful, busy, hurt, or angry even when
long-term recall returns nothing. Whether she says “早”, teases, complains,
changes the subject, or remains silent remains her decision.

The production model view names this derived projection
`current-self-state.1`. It keeps stable Character Core separate from dynamic
Situation, Appraisal, Affect, relationship, unresolved Thread, and advisory
material. Concurrent feelings remain separate components with their own
source refs and temporal fields; the host must not average them into a mood
label or map them to an expression. Only accepted Capsule items enter this
view. Raw local-model drafts, `immediate` scheduling decisions, provider
timeouts, and retry counters remain operational evidence rather than parts of
the character's self.

### 2. Build a bounded attention packet, not a World dump

Use separate retrieval inputs:

- **lexical cue:** the exact new user text and, when needed, the immediate
  unresolved utterance;
- **dense cue:** the user text plus a short, human-readable present-attention
  description derived from already-pinned state;
- **structured selectors:** actor/subject refs, thread/entity links, occurrence
  interval, validity, privacy ceiling, and requested memory kinds;
- **accessibility seed:** recorded variation that can slightly alter which
  equally plausible memories surface.

Do not put source IDs, payload hashes, full activity arrays, or the entire
Context Capsule into `query_text`. Keep those in filters and provenance.

The packet must enforce one canonical byte/character budget at construction
time. Downstream schemas should accept exactly that contract; a caller must not
be able to construct an oversized query and discover the limit through an
exception.

### 3. Retrieve in parallel and fuse evidence

Generate candidates from lexical, dense, structured-link, and temporal
channels. Fuse/rerank them as **accessibility**, not behavioural preference,
then enforce diversity across episodic, semantic, reflective, emotional, and
self/counterpart memories. Preserve exact source bindings on every returned
item.

No extra query-rewriting LLM should run on ordinary turns. Document embeddings
remain cached; the fast path pays at most one query embedding. Consolidation
and retrieval-text enrichment run asynchronously.

### 4. Keep one character deliberation path

The same inbound cognition receives:

1. hot context;
2. the small automatic recall set, if ready within the bounded join;
3. the same expression capabilities and hard authority boundaries.

The character may answer immediately, ignore recalled material, or request one
deeper retrieval when an ambiguous reference or spontaneous association
matters. That second retrieval is another step inside the same deliberation,
not a separate reply policy and not a fact-validation bypass.

### 5. Fail open for accessibility, not for truth

| Failure | Required behaviour |
| --- | --- |
| Oversized attention material | Budget it before schema construction; never throw |
| Embedding unavailable/slow | Use lexical + structured + temporal candidates |
| Recall index unavailable | Continue with hot context and mark recall degraded |
| No relevant memories | Return an empty evidence set; do not invent one |
| Consolidation provider slow | Queue/retry off the visible reply path |
| Main character generation fails | Record technical failure and enter the inbound retry state |

Recall degradation must never become `observed_only`, `model_silent`, or a
locally authored fallback utterance.

### 6. Preserve deep audit while exposing a compact outcome

Keep the full ledger, pinned cursor, query/result hashes, index/embedding
versions, source closure, and retrieval scores. Add a compact turn-facing
summary such as:

```text
hot_context=ready
recall=degraded(embedding_timeout)
fallback_channels=lexical,structured,temporal
hits=2
character_outcome=action_authorized
```

Operational diagnosis should normally read this projection; full event
expansion remains available for exceptional debugging.

## Suggested evaluation

The implementation should be judged on paired scenarios, not only retrieval
unit tests:

- “早” under neutral, affectionate, busy, hurt, and angry hot states;
- abrupt topic switches followed hours later by a meaningful continuation;
- correction/supersession of an old user fact;
- temporal prompts such as “上午那件事后来怎么样了”;
- a vague cue that should evoke an emotional episode but not a false fact;
- embedding timeout, invalid output, empty recall, and restart replay;
- one message arriving while an earlier multi-beat response is in flight.

Measure recall hit provenance, final continuity, unsupported-claim rate,
time-to-first-visible-beat, additional model calls, query-embedding latency,
prompt tokens, and technical-silence rate. A memory improvement that increases
recall accuracy but recreates silent turns is not an improvement for this
project.

## Implemented production contract

The 2026-07-28 implementation installs the following boundaries:

- automatic prefetch constructs one canonical request before schema
  validation, with a maximum 1,024-character dense attention cue;
- exact inbound wording is retained separately as the lexical cue, while
  opaque cluster/thread identities remain structured link selectors;
- accepted Appraisal, active Affect, relationship, Situation, and open Thread
  values can enrich dense accessibility without becoming response
  instructions;
- dense embedding uses the state-aware cue, lexical matching uses the exact
  observation, and both continue through the existing structured and temporal
  fusion;
- the same verified Context produces `current-self-state.1` for the inbound
  role model. Affect components retain their independent cause cluster and
  accepted Appraisal refs; Appraisals retain source clusters/evidence, and
  local semantic advisories retain their readable weighted candidates. Raw
  model drafts remain excluded, and no separate reply generator or fact-check
  bypass is introduced;
- the current Observation remains the ordinary `trigger_message` beside this
  state rather than being duplicated inside the character's self. Interruption
  judgments remain source-bound advisory candidates; relationship state keeps
  its current variables and `last_adjusted_at` without inventing a narrative
  of why it changed;
- retrieval fusion deterministically preserves memory-kind, source-lane, and
  self/counterpart subject diversity before filling the remaining top-score
  slots. This changes accessibility only, never required response content;
- the attention request carries canonical temporal and memory-kind selectors
  when a typed caller has them. Automatic prefetch deliberately does not parse
  phrases such as “上午” with keywords; the character-owned deeper pull can
  provide those selectors without adding a query-rewrite model to every turn;
- recall health exposes a compact `turn_summary` with hot-context readiness,
  recall status, fallback channels, and hit count while retaining the complete
  sealed trace for audit;
- semantic-recall failure continues through lexical/structured/temporal
  fallback and cannot become a local utterance or a silent character verdict;
- same-turn emotion scheduling is decided only by the bounded local semantic
  model. Keyword tables no longer force a scheduling or provider-route
  outcome. A valid model-authored `true` selects same-turn Appraisal; `false`,
  a missing model, timeout, or invalid output leaves the already-open durable
  appraisal trigger to the background lane. This deliberately keeps the local
  classifier outside visible-reply availability: current accepted Affect
  remains in `current_self_state`, and the new turn is enriched later instead
  of converting a local-model outage into silence.
