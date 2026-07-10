# Fact Memory Patterns for Girl-Agent

Updated: 2026-07-11

## Decision

Keep the daemon's local SQLite store. Do not import a graph database or replace
the conversation runtime with Mem0, Graphiti, or Letta. Port the useful parts:

1. **Letta memory blocks:** keep a small, named, budgeted block for each role:
   immutable persona facts, verified user facts, relationship state, and
   retrieval-only episodic hints.
2. **CharMemory extraction discipline:** only new information from the current
   user turn may become a durable fact. Character-card text and prior memories
   are reference material, not extraction input. Facts should be concise and
   topic-shaped rather than chat play-by-play.
3. **Zep/Graphiti temporal provenance:** facts are append-only observations
   with source, ingestion time, validity state, and supersession. A changed
   value invalidates the old current value rather than deleting history.

## Why not import a full project

Mem0 and Graphiti solve multi-user, embedding, graph traversal, and hosted
storage concerns that this one-user local daemon does not currently have. They
would add a second memory authority and more operational failure modes. The
SQLite ledger preserves one source of truth while leaving a clean upgrade path
for embeddings or a temporal graph later.

## Local Contract

| Block | Authority | May support a concrete claim? | Write rule |
| --- | --- | --- | --- |
| Persona facts | Character YAML | Yes | Developer-edited, read-only at runtime |
| User fact ledger | Explicit user message | Yes | Append observation, supersede current conflict |
| Relationship state | Daemon state machine | Behavioral only | State transition only |
| Episodic memory | Message/event history | Topic and tone only | Never elevate alone into a fact |
| Creative flavor | Model output | No | Never write to long-term memory automatically |

## Sources

- Letta memory blocks: https://www.letta.com/blog/memory-blocks/
- Letta GitHub: https://github.com/letta-ai/letta
- Mem0 GitHub: https://github.com/mem0ai/mem0
- Character Memory GitHub: https://github.com/bal-spec/sillytavern-character-memory
- Zep temporal graph paper: https://arxiv.org/abs/2501.13956
- Graphiti / Zep temporal graph overview: https://www.getzep.com/ai-agents/temporal-knowledge-graph/
