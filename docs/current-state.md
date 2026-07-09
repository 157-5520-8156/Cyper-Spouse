# Current State

Date: 2026-07-09

## What Runs Now

### Companion Daemon

The Python daemon is implemented and tested.

Verified commands:

```bash
uv run pytest
uv run ruff check .
uv run companion-sim --fake "我刚刚在忙，现在回来了"
uv run companion-sim "我刚刚在忙，现在回来了"
```

The real `companion-sim` command successfully used the local `DEEPSEEK_API_KEY` environment variable and produced:

- a companion reply,
- an internal/private proactive thought,
- and sometimes a no-send proactive decision.

This confirms the core loop can call the DeepSeek API.

### HTTP Daemon

Verified:

```bash
uv run companion-daemon
curl -s http://127.0.0.1:8765/health
curl -s -X POST http://127.0.0.1:8765/messages \
  -H 'Content-Type: application/json' \
  -d '{"platform":"qq","platform_user_id":"geoff","text":"我在 QQ 上试一下，你在吗"}'
```

The daemon returned a real generated reply.

### SillyTavern

SillyTavern is checked out at:

```text
external/SillyTavern
```

Verified:

```bash
cd external/SillyTavern
npm install
npm run start
```

It starts at:

```text
http://127.0.0.1:8000/
```

Installed extensions:

- Smart-Memory
- SillyTavern-EchoText
- SillyTavern-EchoText-Proactive server plugin

The server plugin loaded successfully:

```text
[EchoText-Proactive] Plugin loaded. Version: 1.0.0
[EchoText-Proactive] Scheduler started (tick every 60s).
```

SillyTavern logs an AI Horde certificate error on startup. This appears to be from its built-in external AI Horde status check and does not block local startup or the DeepSeek route.

### QQ Official WebSocket

Webhook setup was blocked by QQ's domain filing/备案 requirement for callback URLs. The project switched to QQ official WebSocket using `qq-botpy`.

Verified command:

```bash
uv run companion-qq-ws --sandbox
```

Verified behavior:

- Access token acquisition succeeds.
- WebSocket gateway connects.
- The gateway reports robot startup success.
- Robot name is `沈知栀 Celia Shen`.
- QQ private C2C messages reach the local process.
- The companion replies in QQ private chat.
- Rapid messages from the same QQ user are handled by a turn-taking state machine before one reply.
- The turn-taking policy waits longer for likely unfinished fragments and replies sooner for complete questions or explicit "you can answer now" cues.
- Local sticker/image sending through QQ official rich-media APIs works in sandbox.
- Ordinary QQ replies can attach a mood-appropriate local sticker after the text reply.
- Explicit image/selfie requests can generate and attach an OpenAI image when `ALLOW_AUTO_IMAGE_GENERATION=true`, `OPENAI_API_KEY` is configured, the character boundary check allows it, and the local budget gate allows it.
- Early, pushy, or emotionally badly timed selfie requests are deferred/refused in prompt context instead of generating an image.
- Proactive decisions can rarely attach a self-initiated life image/selfie when relationship state, mood, image generator configuration, and budget allow it.
- The first沈知栀 visual reference set is saved under `assets/reference/`; LoRA/FaceID training has not been done yet.
- A human-rhythm layer injects Chengdu local day phase, private activity, attention mode, and no-stage-direction guidance into chat/proactive prompts.
- Sending a reply or proactive message now feeds back into mood state by reducing unresolved charge/initiative instead of leaving the state untouched.

Verified command:

```bash
uv run companion-send-sticker --user geoff --category happy --sandbox
```

Result:

```text
sent sticker: happy -> assets/stickers/rin-happy.png
sent sticker: teasing -> assets/stickers/rin-teasing.png
```

Implementation path:

```text
local PNG -> base64 file_data -> /v2/users/{openid}/files -> file_info -> msg_type=7 media message
```

### Multimodal

Implemented:

- QQ attachment normalization for images, audio, video, and files.
- Attachment metadata preservation through QQ message coalescing.
- Attachment metadata and summaries are injected into the companion prompt.
- Long-term memory records that the user sent image/audio/file attachments.
- Text-like file analysis hook.
- Provider-agnostic `MultimodalAnalyzer` interface.
- OpenAI-compatible image understanding and speech transcription provider.
- Local CNY budget gate for vision, transcription, and image generation.

Current limits:

- DeepSeek's commonly documented API path is text-oriented, so true image understanding and voice transcription are intentionally provider-pluggable rather than hardcoded.
- OpenAI multimodal calls require `OPENAI_API_KEY` and can be disabled with `MULTIMODAL_PROVIDER=metadata`.
- Usage records are local estimates for throttling, not official billing data.

### Character Profile

The character was reset from a premature girlfriend preset to a richer "just met" college student persona.

Current profile:

- Chinese name: 沈知栀
- English name: Celia Shen
- Age: 20
- Hometown: 嘉兴
- Current city: 上海
- School: 华东师范大学
- Major: 汉语言文学
- Relationship stage: 刚认识
- Origin: met through a QQ reading/city-walk interest group after the user shared a late-night Chengdu street note.

The prompt now explicitly forbids stage directions such as `（手机震了一下）`, action narration, and hidden psychological notes in public QQ/WeChat replies. A sanitizer also removes common roleplay-style stage directions before sending.

The live QQ reply prompt also has a naturalness guard: avoid asking a question every turn, keep at most one question mark per reply, reduce assistant-like phrases such as “我理解/这个问题确实/建议”, and avoid the overused “我有个朋友/同学/室友也...” pattern unless memory or context truly supports it.

### Emotional State Machine

Implemented:

- Interaction event classification for rude/control/warmth/apology/vulnerable/busy/returning/question/attachment-only messages.
- Expanded mood state: patience, security, curiosity, initiative, emotional charge, and boundary level.
- EchoText-inspired Plutchik emotion vector with baseline, affinity drift, natural decay, opposite-emotion suppression, and text intensity multipliers.
- EchoText-inspired MBTI/personality baseline anchors are applied from the character profile on first initialization only.
- Interaction events are persisted in SQLite.
- State is injected into both normal replies and proactive decisions.
- Existing mood state is no longer reset when the runtime builds a new engine.
- Boundary, hurt, anger, and disgust states reduce proactive frequency and life-event probability.
- EchoText-inspired proactive trigger timeline chooses concrete outreach reasons such as hanging question, late night, repair attempt, longing, random thought, inside joke, or soft follow-up.
- Trigger history, semantic category cooldowns, daily-stable jitter, and a max-unanswered-proactive guard reduce repetitive or needy proactive messages.
- Recent emotion impact can create a `mood_follow_up` proactive candidate when the state shift is strong enough.
- A short `open_thread_afterthought` trigger handles the “this turn has not fully ended” case: if she sent the last message 9-90 minutes ago and the mood is safe, she may add one small thought without re-asking the user.
- EchoText-inspired memory highlight detection extracts life facts, favorites, hobbies, important people, recent events, and shared moments.
- Memory injection now selects a small high-signal subset instead of always injecting every recent memory.
- Near-duplicate memory entries are merged instead of stored as separate facts.
- EchoText-inspired image request detection recognizes direct selfie/image requests and affirmative responses to recent image offers.
- EchoText-inspired image prompt building classifies character/object/creative image requests and carries visual identity/context into stable generation prompts.
- Automatic image generation is guarded by character boundary checks and the local CNY budget gate, recording blocked/deferred requests instead of silently spending.
- Self-initiated proactive image sharing is supported as a rare state-machine outcome, separate from user-demanded selfies.
- EchoText-inspired reaction selection can suggest lightweight reactions from emotional deltas.
- EchoText-inspired reply timing model estimates read/reply/ghost delays from emotion vectors.
- EchoText-inspired image style detection carries user-requested styles into generation prompts.
- External context emotion bleed is capped to keep SillyTavern/MCP/multimodal context from overwhelming the core state.
- Chengdu-local human rhythm context keeps replies from feeling like an always-on assistant and explicitly suppresses bracketed stage directions.
- QQ WebSocket delivery now adds read/think/typing delay before the first reply and human-sized pauses between split reply parts, instead of sending 2-3 parts in one burst.
- QQ private chat now has a conservative reply-decision layer enabled by default: pure acknowledgements can be recorded without a reply, questions/emotional/urgent messages always reply, and long story-like messages may be deferred during busy phases with a “just saw this” context hint.
- Normal replies include a question-budget hint based on recent outgoing messages, so if she has already asked several questions recently the next reply is steered toward statements, reactions, or small self-disclosure instead of another follow-up question.
- Sticker selection maps newer moods such as `hurt`, `guarded`, `curious`, and `affectionate` to available visual assets.
- Tool/computer-operation requests are detected and logged as proposals. Risky actions are injected into the prompt as requiring explicit user confirmation; no MCP/computer action executes automatically yet.

See `docs/state-machine.md`.

## Implemented Project Modules

```text
src/companion_daemon/
  app.py             FastAPI app and endpoints
  character.py       character config loader
  cli.py             local simulator
  config.py          environment/config settings
  conversation.py    replaceable conversation core interface
  db.py              SQLite store
  engine.py          message handling and proactive loop
  llm.py             DeepSeek API wrapper and fake test model
  models.py          Pydantic models
  mood.py            mood/relationship updates
  prompts.py         reply/proactive prompt builders
  qq_official.py     QQ official webhook helpers
  time.py            timezone-aware clock helper
```

## Character

The current companion profile lives at:

```text
configs/character.yaml
```

Current name:

```text
沈知栀 / Celia Shen
```

The profile is now loaded by the daemon instead of being hardcoded.

## QQ Official Bot Status

Implemented locally:

- WebSocket adapter command: `uv run companion-qq-ws --sandbox`.
- `/qq/webhook` FastAPI endpoint.
- QQ callback validation response signing.
- QQ callback signature verification helper.
- C2C and group-at event parsing into daemon messages.
- Tests for official validation-signature sample from QQ docs.
- QQ official access-token client.
- QQ official C2C text sending client.
- QQ official group text sending client.

Verified:

```bash
uv run pytest
```

Current local verification count:

```text
81 tests passed
ruff check passed
```

Still not possible without user-side setup:

- Webhook callback without a filed/备案 domain.
- Production use outside the current sandbox/allowlist.
- Confirm whether official C2C proactive-message limits are acceptable.

The official send-message docs currently state that QQ single-chat proactive messages are heavily limited, while passive replies have a time window and per-message reply limits. This is why the first real QQ test must confirm whether the official single-chat product behavior is compatible with the companion experience.

## Important Security Note

Earlier terminal output exposed API keys in the local conversation. Treat those keys as compromised and rotate them.

## Next Best Step

The next meaningful step is improving the QQ private-chat experience now that private C2C is working:

1. Keep this command running:

```bash
uv run companion-qq-ws --sandbox
```

2. Chat with 沈知栀 in QQ private chat.
3. Observe whether reply style, speed, and memory feel right.
4. Next implementation targets:

```text
- better QQ reply error logging
- true vision provider integration
- true speech-to-text provider integration
```

Implemented after this note:

```text
- turn-taking state machine for rapid multi-message bursts
- controlled proactive decision CLI:
  uv run companion-proactive --user geoff --sandbox
  uv run companion-proactive --user geoff --sandbox --send
- QQ sticker/image sending:
  uv run companion-send-sticker --user geoff --category happy --sandbox
- ordinary reply sticker/image delivery through QQ WebSocket
- guarded tool request proposal logging for future MCP integration
```
