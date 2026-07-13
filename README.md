# Girl Agent

Local-first cyber companion project.

The current implementation focuses on the part that existing open-source projects do not provide cleanly: a Companion Daemon that can sit between QQ/WeChat adapters and a SillyTavern-style companion core.

## Current MVP

- FastAPI daemon.
- SQLite local state.
- Canonical identity mapping across platforms.
- Message batching-ready storage.
- Emotional state machine and relationship state.
- DeepSeek-compatible LLM client.
- Simulated QQ message flow.
- Proactive reflection/decision loop.

## Commands

Install dependencies:

```bash
uv sync
```

Run tests:

```bash
uv run pytest
```

Run the daemon:

```bash
uv run companion-daemon
```

Simulate a QQ message:

```bash
uv run companion-sim "我刚刚在忙，现在回来了"
```

The daemon reads `DEEPSEEK_API_KEY` from the environment. Tests do not call the real API.

Run the QQ official WebSocket adapter:

```bash
uv run companion-qq-ws --sandbox
```

This is the currently working QQ route. Webhook was blocked by QQ's domain filing requirement during local development.

Run the QQ small-account route through NapCat (OneBot v11):

```bash
scripts/run_napcat_adapter.sh
```

It uses local loopback only and is an alternative to the official adapter. See
[`docs/napcat-setup.md`](docs/napcat-setup.md) for the NapCat WebUI configuration,
health checks, and Antify direct-routing rule.

The generic OneBot route remains separately selectable:

```bash
scripts/run_onebot_adapter.sh
```

By default, QQ messages from the same user are batched for a short moment before replying, so rapid short messages are handled as one turn. Override with:

```bash
QQ_MESSAGE_BATCH_SECONDS=2.5 uv run companion-qq-ws --sandbox
```

The batcher is stateful. If a burst looks unfinished, such as messages beginning with "还有/然后/因为" or ending with "，/：/...", it waits longer. If the message is a complete question or you say "你先说", it replies sooner.

If an outbound action remains `unknown` after a process crash and the QQ adapter
cannot query a receipt, an operator can reconcile it only after checking external
evidence. Configure `DELIVERY_RECONCILIATION_TOKEN`, then send the current world
revision, the `delivery_id`, a platform receipt (or another stable evidence
reference), reviewer identity, and a review note to:

```text
POST /world/{world_id}/deliveries/{delivery_id}/reconcile
X-Delivery-Reconciliation-Token: <operator token>
```

The endpoint accepts only `unknown` deliveries and only the `delivered` or
`failed` terminal state. Without evidence it leaves the action unchanged; it
never retries the message.
For a partially sent multi-part reply, also provide the unknown `segment_id`.
The verified segment is committed once; by default any never-sent remainder is
cancelled (`cancel_remaining=true`) so it cannot be replayed as a duplicate.

Run one proactive decision without sending:

```bash
uv run companion-proactive --user geoff --sandbox
```

Actually send the proactive QQ wakeup message only if the decision says to send:

```bash
uv run companion-proactive --user geoff --sandbox --send
```

Use `--send` sparingly. QQ official single-chat proactive messages have strict limits.

`companion-send-sticker` has been retired. A manually chosen local image has
no World-authorized expression Action or conversational causation, so sending
it directly would bypass durable receipt settlement. Stickers are now sent
only when a World-backed companion turn schedules them.

```bash
uv run companion-send-sticker --user geoff --category happy --sandbox
```

## SillyTavern

SillyTavern is checked out under `external/SillyTavern` as an upstream project.

Run it with:

```bash
cd external/SillyTavern
npm install
npm run start
```

It listens on `http://127.0.0.1:8000/` by default.

## QQ official bot status

The working QQ path is WebSocket:

```bash
uv run companion-qq-ws --sandbox
```

Verified:

- QQ official access token works.
- QQ official WebSocket connects in sandbox.
- Private C2C chat receives messages and replies.
- Robot name shown by the gateway: `沈知栀 Celia Shen`.
- Rapid QQ messages are coalesced before reply.
- Local sticker/image sending works through QQ official rich-media APIs.
- Incoming QQ attachments are normalized as image/audio/video/file metadata for multimodal handling.

Current multimodal status:

- Image: receives and records image metadata; if `OPENAI_API_KEY` is present, image understanding uses the configured OpenAI-compatible vision provider behind the local budget gate.
- File: receives and records file metadata; text-like files have a parser hook.
- Audio: receives and records audio metadata; if `OPENAI_API_KEY` is present, transcription uses the configured OpenAI-compatible STT provider behind the local budget gate.

Cost control and visual identity notes:

- Budget settings live in `.env` as `MONTHLY_BUDGET_CNY`, `DAILY_BUDGET_CNY`, `SOFT_DAILY_BUDGET_CNY`, and monthly multimodal limits.
- See `docs/cost-control.md` for the current spending policy.
- See `configs/visual_identity.yaml` and `docs/visual-identity.md` for the selfie/virtual-life image consistency plan.
- See `docs/state-machine.md` for the interaction event and emotional state loop.

The daemon also includes a local QQ official webhook endpoint:

```text
POST /qq/webhook
```

Implemented:

- Official callback validation response signing.
- Official callback signature verification helper.
- C2C and group-at event parsing into daemon messages.
- Access-token acquisition.
- Single-chat text sending client.
- Group text sending client.

Still needs user-side QQ setup:

- QQ bot app id and secret.
- Sandbox or allowed private-chat target.
- For Webhook only: a filed/备案 public HTTPS callback domain.
- Confirmation that official proactive C2C limits are acceptable.
