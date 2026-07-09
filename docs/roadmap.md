# Roadmap

Date: 2026-07-09

## Phase 0: Repository Foundation

Deliverables:

- Architecture document.
- QQ integration research.
- Minimal service skeleton.
- Local config strategy that never logs API keys.

Exit criteria:

- We know which QQ path to prototype first.
- The project has a clear boundary between adapter, daemon, and SillyTavern core.

Status: complete as of 2026-07-09. See `docs/current-state.md`.

## Phase 1: SillyTavern + DeepSeek Companion Core

Goal: Make the character feel right before connecting her to QQ.

Tasks:

- Install/run SillyTavern locally.
- Configure DeepSeek through the existing `DEEPSEEK_API_KEY` environment variable.
- Create a first character card.
- Add a companion prompt policy for tone, mood, boundaries, and relationship continuity.
- Install/test Smart Memory or another memory extension.

Exit criteria:

- A local chat session produces the desired companion tone.
- The character can remember core facts across chats.
- We can identify what data should stay in SillyTavern versus the daemon.

Status: partially complete. SillyTavern starts locally, DeepSeek works through the daemon, and Smart-Memory/EchoText extensions are installed. Manual UI configuration inside SillyTavern is still needed for full ST-native chatting.

## Phase 2: Companion Daemon MVP

Goal: Build the smallest layer that can mediate IM messages and companion state.

Tasks:

- FastAPI service.
- SQLite database.
- Tables for users, platform accounts, messages, mood state, relationship state, proactive events, stickers.
- DeepSeek client for daemon-side reflection tasks.
- Message batching.
- Platform metadata injection.

Exit criteria:

- A local HTTP endpoint can accept a simulated QQ message.
- The daemon stores it, updates state, and returns a companion-style reply.
- The daemon can say when the user switched platform, using synthetic test events.

## Phase 3: QQ Official Bot Prototype

Goal: Test official QQ bot feasibility with real platform constraints.

Tasks:

- Create QQ bot app.
- Configure Webhook endpoint.
- Implement signature validation.
- Receive message events.
- Send passive text replies.
- Test image/sticker sending.
- Test active/proactive message limits.

Exit criteria:

- Real QQ message in, real QQ reply out.
- Known answer on whether official C2C proactive behavior is acceptable.
- Written decision: continue official path or switch to NapCat/Lagrange.

Status: partially complete. Webhook was blocked by QQ callback domain filing requirements. WebSocket sandbox mode works, and QQ private C2C chat has been verified by the user.

Additional status: QQ private-chat input now uses a turn-taking state machine rather than a fixed debounce timer. It can wait longer when the user appears to be continuing the same thought.

## Phase 4: Proactive Mood Loop

Goal: Implement the "third level" companion behavior.

Tasks:

- Periodic wake loop.
- Mood scoring from recent context and time gaps.
- Private reflection prompt.
- Decision JSON schema.
- Cooldowns and quiet hours.
- Proactive text send through QQ adapter.
- "Do nothing" path as a first-class outcome.

Exit criteria:

- She sometimes decides not to send.
- She can send a restrained message after a meaningful delay.
- Mood affects message length and tone.
- The same situation does not produce repetitive canned messages.

Status: mostly complete locally. The daemon has a proactive decision loop, delivery cooldown records, a guarded CLI, and a guarded scheduler. It only sends to QQ when explicitly invoked with `--send`, because official C2C proactive messages are limited.

The scheduler can run one pass or loop continuously:

```bash
uv run companion-proactive-scheduler --once --sandbox
uv run companion-proactive-scheduler --sandbox --send
```

## Phase 5: Generated Sticker Library

Goal: Give her a personal sticker vocabulary without manually collected meme packs.

Tasks:

- Define sticker categories.
- Generate an initial local sticker set.
- Store metadata for mood, intent, and intensity.
- Add sticker selection to daemon.
- Send sticker/image through QQ.

Exit criteria:

- She can choose text, sticker, or text+sticker.
- Sticker choice matches mood and relationship context.
- No dependence on private or copyrighted personal meme packs.

Status: complete for QQ image/sticker sending. An original沈知栀 sticker sheet was generated, cropped into 8 local PNGs, connected to daemon-side sticker metadata, and verified through QQ official rich-media APIs in sandbox with `rin-happy.png` and `rin-teasing.png`.

Ordinary replies can now attach mood-appropriate local stickers automatically after the text reply.

Image/selfie requests can now generate and attach an OpenAI image when `ALLOW_AUTO_IMAGE_GENERATION=true`, an OpenAI key is configured, and the budget gate allows the automatic spend.

## Phase 6: WeChat Adapter

Goal: Add the second platform after QQ is stable.

Tasks:

- Evaluate current WeChat adapter options.
- Start with lowest-risk account strategy.
- Connect WeChat messages into the same daemon.
- Map WeChat account to the same canonical user.
- Test platform-switch awareness.

Exit criteria:

- QQ and WeChat share memory and mood state.
- She can notice platform switching.
- WeChat activity obeys stricter rate and safety limits.

## Phase 7: MCP Tools

Goal: Add tool use after the companion behavior is stable.

Tasks:

- Define allowed tools.
- Start with read-only tools.
- Require user confirmation for risky actions.
- Log tool proposals and results.
- Keep tool calls outside the private mood loop unless explicitly allowed.

Exit criteria:

- Tool use does not make the companion feel like a generic assistant.
- Risky actions require confirmation.
- The daemon can explain why a tool was used.

Status: foundation implemented. The daemon detects likely tool/computer-operation requests, records tool proposals, and injects confirmation requirements into the prompt. Actual MCP execution is intentionally not enabled yet.
