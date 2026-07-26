# Human-Like Long-Running Agent Memory: Primary-Source Architecture Survey

Updated: 2026-07-27

This is the evidence appendix for
[`human-like-agent-memory-architecture.md`](human-like-agent-memory-architecture.md).
The Chinese document owns the project recommendation; this appendix preserves
the source-by-source reasoning and does not define a second implementation spec.

## Bottom line

There is no primary-source evidence that one architecture is universally best
for believable long-running companions. The strongest fit for Girl-Agent is
not "RAG" alone. It is a **layered, event-sourced cognitive memory
architecture** in which:

1. the immutable World ledger remains the only factual authority;
2. disposable indexes expose typed episodic, semantic, temporal, and relational
   recall;
3. a small amount of associative recall is prefetched without behavioral
   advice;
4. the character model can search memory again, ignore a recollection, interpret
   it, or remain silent;
5. reflection creates defeasible, source-linked beliefs and self-narratives,
   never new historical facts;
6. all retrieval inputs, index versions, recorded randomness, selected source
   references, and model results needed for replay are persisted.

This is a hybrid of the useful parts of Generative Agents, MemGPT/Letta,
LongMemEval, ACT-R/Soar, and newer temporal/graph retrieval work. Importing one
of those systems wholesale would add a second authority and would not preserve
Girl-Agent's replay and provenance guarantees.

## What the primary sources actually support

### Hybrid episodic/semantic RAG

Generative Agents stores a comprehensive natural-language experience stream,
dynamically retrieves memories, and recursively produces reflections; its
ablation found observation, planning, and reflection all contributed to judged
believability. This is the closest direct evidence for simulated human-like
behavior, but it is a 25-agent sandbox study rather than a production,
multi-month companion study.

LongMemEval finds that long-context and commercial systems still suffer a large
accuracy decline over sustained histories. Its useful engineering findings are
session-level indexing, fact-augmented keys, and time-aware query expansion; it
explicitly evaluates cross-session reasoning, temporal reasoning, knowledge
updates, and abstention.

Mem0 reports substantially lower latency and token use than full-context
baselines while retaining conversational-memory performance. Its paper is
first-party and useful, but its vendor-authored evaluation is not independent
proof that its implementation is best for Girl-Agent. HippoRAG and Zep show why
semantic-vector similarity alone is insufficient: graph and temporal
associations help multi-hop and time-sensitive recall.

**Fit:** Best foundation for long-term continuity, provenance, and latency when
the index is derived from the ledger. Plain vector RAG is not enough.

**Main risk:** A retrieval ranker can silently become a behavioral policy. It
must return evidence and match reasons, not "what she should do."

Sources:

- Generative Agents: https://arxiv.org/abs/2304.03442
- LongMemEval: https://arxiv.org/abs/2410.10813
- Mem0: https://arxiv.org/abs/2504.19413
- HippoRAG: https://arxiv.org/abs/2405.14831
- Zep temporal knowledge graph: https://arxiv.org/abs/2501.13956

### Cognitive architectures

ACT-R separates limited buffers from declarative memory. Declarative retrieval
activation combines recency/frequency, spreading activation from current
buffers, similarity, and optional noise; activation also affects retrieval
latency and can fall below a retrieval threshold. Soar explicitly separates
working, semantic, episodic, and procedural memory, and its episodic memory is
temporally indexed and cue-addressable. CoALA maps this established vocabulary
onto language agents with modular memory, internal/external actions, and a
decision cycle.

These architectures contribute a better model of *accessibility* than ordinary
top-k RAG: a fact can remain true yet not be recalled on every turn. That is
useful for controlled human-like variance. It should be implemented with
recorded draws and source refs so replay remains deterministic.

**Fit:** Excellent design vocabulary and useful retrieval dynamics. Symbolic
state also replays well.

**Main risk:** A wholesale ACT-R/Soar-style production system would put too much
behavior into authored rules, conflicting with model-led character agency. Use
the memory separation and activation ideas, not a rule-authored personality.

Sources:

- ACT-R 7 reference manual: https://act-r.psy.cmu.edu/actr7.x/reference-manual.pdf
- ACT-R integrated theory: https://pubmed.ncbi.nlm.nih.gov/15482072/
- Soar architecture manual: https://soar.eecs.umich.edu/soar_manual/02_TheSoarArchitecture/
- Soar semantic memory: https://soar.eecs.umich.edu/soar_manual/06_SemanticMemory/
- Soar episodic memory: https://soar.eecs.umich.edu/soar_manual/07_EpisodicMemory/
- CoALA: https://arxiv.org/abs/2309.02427

### Recurrent or latent neural state

Transformer-XL and Recurrent Memory Transformer show that segment recurrence or
learned memory tokens can carry dependencies beyond a fixed context at efficient
inference cost. They are attractive for immediate conversational flow and
compact state.

**Fit:** Good as an ephemeral fluency cache or future base-model capability.

**Main risk:** Latent state has no natural event-level provenance, is difficult
to inspect or correct, and is lossy. It cannot be the authority for user facts,
commitments, or world history, and it is a poor match for deterministic audit.
A provider-hosted model also rarely exposes the recurrent state required to
resume it exactly.

Sources:

- Transformer-XL: https://arxiv.org/abs/1901.02860
- Recurrent Memory Transformer: https://arxiv.org/abs/2207.06881

### Fine-tuning and parameter memory

LoRA makes task or domain adaptation much cheaper and adds no inference latency.
This makes it suitable for stable voice, linguistic habits, and broad character
priors. It does not make rapidly changing personal history source-addressable.
Continual instruction-tuning experiments also observe catastrophic forgetting
across model sizes.

**Fit:** Useful for the character's stable expression distribution, possibly
after a carefully curated conversation dataset exists.

**Main risk:** Wrong place for episodic facts, current relationship state, or
daily life. Parameter changes are hard to explain, selectively supersede, or
replay from a ledger cursor. Frequent personalization training also creates
privacy, deployment, and forgetting problems.

Sources:

- LoRA: https://arxiv.org/abs/2106.09685
- Empirical catastrophic-forgetting study:
  https://arxiv.org/abs/2308.08747

### Reflection and planning

Generative Agents provides direct believability evidence for reflection and
planning. Reflexion shows that language-space reflections stored in episodic
memory can improve later decisions without weight updates. Voyager shows that
interpretable, retrievable learned artifacts and environment-feedback loops can
support open-ended lifelong behavior without fine-tuning.

**Fit:** Important for a coherent self-narrative, changing opinions, unresolved
concerns, and plans that span sessions.

**Main risk:** Reflection is model-generated inference, not observation. If it
is promoted to truth or used as an imperative, it creates hallucinated memories
and scripted conduct. Store it as a versioned belief with support/refute source
refs and let the character revise or ignore it. Run consolidation off the reply
critical path.

Sources:

- Generative Agents: https://arxiv.org/abs/2304.03442
- Reflexion: https://arxiv.org/abs/2303.11366
- Voyager: https://arxiv.org/abs/2305.16291

### Agent-controlled retrieval

MemGPT gives the model explicit control over multiple memory tiers. ReAct shows
the benefit of interleaving reasoning with actions that gather information.
Toolformer and Self-RAG train models to decide when retrieval or a tool call is
needed; Self-RAG specifically reports that indiscriminate fixed-count retrieval
can hurt output and instead uses adaptive retrieval and critique. A-MEM lets an
agent dynamically organize and connect memories.

**Fit:** Best mechanism for character agency: the system exposes memories while
the model decides what to look for and what the recollection means.

**Main risk:** Agent-only retrieval has a bootstrap failure: if the model does
not realize that a relevant memory exists, it never searches for it. A-MEM-style
rewriting of old memory metadata also conflicts with immutable truth if the
derived representation is mistaken for the source.

**Resolution:** Use a dual path. Prefetch a tiny, diverse, evidence-only
candidate set on the fast path, then let the model issue optional follow-up
recall. All agentic links and summaries remain rebuildable sidecar projections.

Sources:

- MemGPT: https://arxiv.org/abs/2310.08560
- ReAct: https://arxiv.org/abs/2210.03629
- Toolformer: https://arxiv.org/abs/2302.04761
- Self-RAG: https://arxiv.org/abs/2310.11511
- A-MEM: https://arxiv.org/abs/2502.12110

## Comparative fit for Girl-Agent

| Approach | Human-like continuity | Character agency | Truth/provenance | Latency | Replay/audit | Recommended role |
| --- | --- | --- | --- | --- | --- | --- |
| Typed hybrid RAG | High | High if evidence-only | High with ledger refs | Good with bounded prefetch | High if results/index version are pinned | Primary recall layer |
| Cognitive architecture | Potentially high | Medium; rules can dominate | High | Good after substantial engineering | High | Borrow memory/buffer/activation concepts |
| Recurrent/latent state | Good locally, uncertain over months | High | Low | Excellent | Low | Ephemeral conversational cache only |
| Continual fine-tuning | Stable style, poor exact history | Medium | Low | Excellent at inference | Low/medium with checkpointing | Voice/personality distribution only |
| Reflection/planning | High when grounded | High | Medium unless source-linked | Extra model cost | High if outputs are recorded | Async, defeasible self-model |
| Agent-only retrieval | High when search is initiated | Very high | High with refs | Variable/multi-call | High if calls/results recorded | Optional deliberate recall, not sole path |

## Recommended architecture for Girl-Agent

### 1. Truth plane

Keep the append-only World ledger as the sole authority. Facts carry speaker,
subject, observed/valid time, supersession, privacy, and source event refs.
Character utterances cannot independently prove user history.

### 2. Rebuildable memory plane

Maintain sidecar projections at a pinned World cursor:

- episodic index over observations and experiences;
- semantic/current-belief index over versioned facts;
- temporal index over occurrence and validity intervals;
- lightweight entity/thread/commitment graph;
- reflection/self-narrative notes clearly marked as defeasible interpretations.

Use dense retrieval, lexical retrieval, temporal constraints, and existing
structured links. Fuse candidates; do not create a second factual database.

### 3. Dual-path recall

The fast path deterministically derives a query from the current observation,
entities, open threads, and scene, and prefetches roughly 3-6 diverse memories.
The packet contains evidence excerpts, source refs, time, status, and why each
candidate matched. It contains no emotional or behavioral recommendation.

The character model may then:

- use, reinterpret, or ignore any candidate;
- call read-only recall with its own query;
- inspect adjacent events or the source;
- answer without recalling;
- decide not to speak.

This preserves agency without relying on the model to know that an unseen
memory is searchable.

### 4. Human-like accessibility, not destructive forgetting

Borrow ACT-R's idea that accessibility depends on current cues,
recency/frequency, spreading association, and noise. Apply it to candidate
selection or diversity, not to factual validity. Never delete or mutate truth
to simulate forgetting. Record the random identity/draw and selected refs so a
replay sees the same recollection opportunity.

### 5. Reflection without invented history

Run consolidation asynchronously after meaningful event clusters or at idle
boundaries. Reflections may form feelings, interpretations, priorities, and
self-narrative, but must link to supporting evidence and may be contradicted or
superseded. They influence future cognition as the character's beliefs, not as
verified world facts.

### 6. Exact replay contract

For each consequential turn record:

- World cursor and capsule hash;
- retrieval query/hash and filters;
- index/embedding/ranker versions;
- candidate source refs and scores;
- any recorded random draw;
- model/provider identity and returned semantic result.

Historical replay consumes recorded retrieval/model results. A fresh index can
be rebuilt from the ledger and compared at the same cursor, but should not be
allowed to silently rewrite what the character saw in the historical turn.

## Why this is preferable to the apparent alternatives

If "best" means only spontaneous prose, a recurrent personalized model may feel
smoothest in the short term. If "best" means a psychologically explicit
simulation, ACT-R or Soar is more theoretically committed. If "best" means
question-answering memory accuracy, a larger temporal graph or multi-stage
agentic search may score better.

Girl-Agent requires all of those qualities while also requiring event-sourced
truth, low latency, controlled high variance, and deterministic audit. The
layered design is therefore the best **Pareto compromise supported by current
primary evidence**:

- ledger truth supplies reliability and replay;
- cognitive activation supplies imperfect, contextual accessibility;
- hybrid temporal/relational retrieval supplies continuity;
- model-controlled search and interpretation supply agency;
- reflection supplies a changing self;
- fine-tuning supplies voice, not autobiographical facts.

No paper currently establishes this composite as the universal optimum for
multi-month human-like companionship. It should be validated with Girl-Agent's
own longitudinal evaluation: memory precision/recall and supersession tests,
blind human naturalness judgments, unwanted-recollection rate, latency
percentiles, proactive-behavior diversity, and replay equality.
