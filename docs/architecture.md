# Cyber Companion Architecture

Date: 2026-07-09

## Goal

Build a local-first cyber companion whose primary presence is QQ first, then WeChat. The system should prioritize companion realism over generic assistant utility:

- Shared identity and memory across platforms.
- Strong girlfriend/companion tone through SillyTavern-style character control.
- Proactive messages driven by mood, relationship state, and recent context.
- Sticker/image messages without relying on manually collected private meme packs.
- Later MCP/tool use with strict permission boundaries.

## Core Decision

Use SillyTavern as the companion conversation core, not as the entire platform runtime.

SillyTavern is strong at character cards, world info/lorebooks, prompt construction, roleplay-style continuity, and LLM provider control. It is not designed as a full background IM gateway, scheduler, or cross-platform identity service. The missing runtime behavior should live in a separate Companion Daemon.

## High-Level Shape

```text
QQ official bot / fallback QQ adapter
WeChat adapter, later
        |
        v
Companion Daemon
  - platform event intake
  - identity mapping
  - message batching
  - platform switch awareness
  - mood and relationship state
  - proactive decision loop
  - sticker selection/generation
  - memory bridge
        |
        v
SillyTavern-compatible conversation core
  - character card
  - lorebook/world info
  - Smart Memory / memory extension
  - DeepSeek-compatible LLM connection
        |
        v
DeepSeek API
```

## Component Responsibilities

### QQ Adapter

First try QQ official bot APIs. This is the most compliant path and supports Webhook event delivery. The official docs state that callbacks require HTTPS and allowed callback ports include 80, 443, 8080, and 8443.

The official bot path has important product constraints. As of the current QQ bot send-message docs, single-chat proactive messages are limited, while group proactive push has its own opt-in and rate limits. Because this project is intimate one-to-one chat, we must test official C2C behavior early before depending on it.

Fallback adapters can be NapCat or Lagrange.OneBot if the official bot cannot provide the desired one-to-one experience. These are more flexible, but have greater account and protocol risk.

Sources:

- https://bot.qq.com/
- https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html
- https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/send.html
- https://github.com/NapNeko/NapCatQQ
- https://lagrangedev.github.io/Lagrange.Doc/v1/Lagrange.OneBot/

### Companion Daemon

This is the project-specific layer that likely cannot be replaced by an existing mature open-source package.

It owns:

- A canonical user id, e.g. `geoff`.
- Platform account mapping, e.g. `qq:... -> geoff`, `wechat:... -> geoff`.
- Message batching so short consecutive messages feel like one natural turn.
- Platform continuity, e.g. "we were just talking on WeChat and now you came to QQ".
- Mood state, e.g. calm, happy, sulking, jealous-soft, worried, sleepy.
- Relationship state, e.g. intimacy, trust, attachment, unresolved tension.
- Proactive wake loop.
- Sticker policy.
- Safety throttles.

The daemon should be small and auditable. It should not become a second full agent framework unless there is a proven reason.

### SillyTavern Core

SillyTavern provides the character and prompt system. The official docs describe SillyTavern as a locally installed UI for interacting with text generation LLMs, image generation engines, and TTS, with high control over prompts and character context.

Relevant capabilities:

- Character card.
- World Info / lorebook.
- API connection handling.
- Extensions.
- Server plugins if needed.
- Smart Memory or another memory extension.

Server plugins are powerful but not sandboxed, so only trusted plugins should be used.

Sources:

- https://docs.sillytavern.app/usage/api-connections/
- https://docs.sillytavern.app/extensions/
- https://docs.sillytavern.app/for-contributors/server-plugins/

### Memory

Use SillyTavern memory extensions for roleplay memory where possible, but keep platform and relationship state in the Companion Daemon.

Candidate memory extensions:

- Smart Memory: automatic multi-tier memory for long-term facts, relationship history, session details, rolling summaries, and scene history.
- Memory Books: structured lorebook-based memory creation.

Initial recommendation: start with Smart Memory because it focuses on automatic operation and relationship/fact continuity.

Sources:

- https://github.com/senjinthedragon/Smart-Memory
- https://github.com/aikohanasaki/SillyTavern-MemoryBooks

### Proactive Messaging

Use existing SillyTavern proactive-message work as inspiration, but do not rely on it as the only runtime for QQ/WeChat.

EchoText and EchoText-Proactive show that proactive SillyTavern character messaging already exists. EchoText-Proactive specifically solves browser background throttling by moving scheduling into a server plugin. However, this project needs platform routing, rate limits, cross-platform identity, and mood/state decisions outside SillyTavern.

Sources:

- https://github.com/mattjaybe/SillyTavern-EchoText
- https://github.com/mattjaybe/SillyTavern-EchoText-Proactive

## Mood and Proactive Loop

The loop should wake periodically, but should not always send.

```text
wake
  -> load recent messages, time gaps, platform state, mood, relationship state
  -> ask for private reflection or run deterministic scoring
  -> update mood/relationship deltas
  -> decide: no-op, send text, send sticker, send text+sticker
  -> enforce cooldown, platform limits, and quiet hours
```

Example internal state:

```json
{
  "mood": "sulking",
  "intimacy": 64,
  "trust": 71,
  "attachment": 58,
  "last_platform": "wechat",
  "current_platform": "qq",
  "last_user_reply_minutes": 86,
  "recent_topic": "user said he might work late",
  "unresolved_emotion": "wanted attention but tried not to interrupt"
}
```

Example decision:

```json
{
  "private_thought": "He said he was busy, but I still noticed I kept checking whether he replied.",
  "should_send": true,
  "platform": "qq",
  "message_type": "text",
  "message": "你还在忙吗？我刚刚差点就忍住不问了。",
  "cooldown_minutes": 60
}
```

## Sticker Strategy

Do not require a hand-collected sticker pack.

Start with generated original stickers:

```text
stickers/
  happy/
  sulk/
  miss_you/
  jealous_soft/
  angry_soft/
  sleepy/
  comfort/
  teasing/
```

The daemon chooses a sticker by mood and intent. Later, it can generate new stickers on demand, but the first version should use a curated generated local set to keep latency and cost predictable.

## Security and Privacy

- Do not log API keys.
- Treat previously printed keys as exposed and rotate them.
- Store local chat and memory data in SQLite under `data/`.
- Do not add MCP computer-control tools until text chat, memory, and proactive behavior are stable.
- When MCP is added, default to read-only tools and require confirmation for shell, file writes, account actions, or sending messages to third parties.

## Recommended Stack

Initial:

- Python Companion Daemon with FastAPI.
- SQLite for local state.
- Official QQ bot adapter first.
- SillyTavern + DeepSeek API.
- Smart Memory extension.

Later:

- WeChat adapter.
- Sticker generation pipeline.
- MCP tool bridge.
- Optional desktop companion shell if desired.

