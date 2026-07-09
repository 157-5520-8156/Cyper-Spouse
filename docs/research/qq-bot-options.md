# QQ Bot Options Research

Date: 2026-07-09

## Question

Which QQ integration path should this project start with for an intimate companion agent that can chat, remember context, send proactive messages, and later send stickers/images?

## Recommendation

Start with QQ official bot APIs. Keep NapCat/Lagrange.OneBot as fallback paths.

The official path is more compliant and better suited for a project that should not depend on private protocol behavior. The risk is product fit: official bots may have approval, sandbox, C2C, and proactive-message limits that make the one-to-one "cyber girlfriend" experience less natural. Those limits must be tested before building the rest of the system around it.

## Option A: QQ Official Bot

### Why Start Here

- Official platform.
- Webhook event delivery is documented.
- Message sending APIs are documented.
- Supports several scenes including QQ single chat, QQ group chat, channel text subchannels, and channel private messages.

### Facts From Official Docs

The event framework supports Webhook and WebSocket-style event delivery. For Webhook mode, the bot service needs an HTTPS callback URL, and the docs list allowed callback ports as 80, 443, 8080, and 8443.

The send-message docs distinguish active push and passive replies. For single chat, the docs currently state that proactive messages are limited to 4 per month for the same user, while passive replies have a 60-minute window and per-message reply limits. The same page also notes C2C interactive recall behavior after the user initiates contact, with limited periods over 30 days.

The docs also state that group proactive push became available on 2026-06-22 if the group owner enables bot proactive speech, with bot-level and group-level rate limits.

### Implications

Official QQ bot is good for:

- First compliant prototype.
- Testing whether official C2C behavior is enough.
- Group or controlled test chats.
- Lower account-risk development.

Official QQ bot may be weak for:

- Frequent proactive girlfriend-style private messages.
- Natural one-to-one personal-account presence.
- Fully free-form sticker behavior, depending on permissions.

### Sources

- https://bot.qq.com/
- https://bot.q.qq.com/wiki/develop/api-v2/
- https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html
- https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/send.html

## Option B: NapCat / OneBot

NapCatQQ is a OneBot framework based on NTQQ. It is attractive because the OneBot ecosystem is flexible and commonly used for QQ bots.

Pros:

- More flexible than official bots in many real-world QQ workflows.
- OneBot-compatible adapters and frameworks are common.
- Likely easier to make the bot feel like a normal QQ presence.

Cons:

- Higher protocol/account risk.
- May break with QQ client/protocol changes.
- Less aligned with official platform rules.

Source:

- https://github.com/NapNeko/NapCatQQ

## Option C: Lagrange.OneBot

Lagrange.OneBot is another OneBot V11-compatible path.

Pros:

- OneBot compatibility.
- Fits existing QQ bot ecosystems.
- Useful fallback if official QQ bot is too limited.

Cons:

- Similar protocol/account risk to other non-official approaches.
- Needs operational care.

Source:

- https://lagrangedev.github.io/Lagrange.Doc/v1/Lagrange.OneBot/

## Option D: Higher-Level IM Bot Platforms

AstrBot, LangBot, and QwenPaw are worth watching because they already connect LLMs to IM platforms.

They are better understood as adapter/runtime candidates, not as the companion "soul".

### AstrBot

AstrBot is described by DeepSeek's integration docs as an all-in-one Agent assistant supporting QQ, WeChat, Feishu, Telegram, skills, plugins, and MCP. It may be useful if we want an off-the-shelf IM bot platform with MCP support.

Sources:

- https://api-docs.deepseek.com/zh-cn/quick_start/agent_integrations/astrbot
- https://github.com/AstrBotDevs/AstrBot

### LangBot

LangBot positions itself as a production-grade platform for building AI-powered instant messaging bots and supports multiple chat platforms. It may be useful for adapter reuse, but companion-specific mood/proactive behavior still needs custom logic.

Source:

- https://github.com/langbot-app/LangBot

### QwenPaw

QwenPaw is strong as a personal assistant runtime with IM channels, memory, skills, MCP, and sessions. It remains a possible adapter/runtime candidate, but for this project it should not replace the SillyTavern-centered companion core unless testing shows its channel layer saves substantial work.

Source:

- https://github.com/agentscope-ai/QwenPaw

## Decision

Use QQ official bot for the first proof of concept. Prefer WebSocket for local development because Webhook callback URL setup may be blocked by domain filing requirements.

Success criteria:

- Receive a message from the target QQ scene.
- Send a normal text reply.
- Send an image/sticker-like message, if permitted.
- Send a proactive message or determine the exact official limitation.
- Preserve enough sender/channel metadata for cross-platform identity mapping.

If C2C proactive behavior is too limited for the desired experience, evaluate NapCat first, then Lagrange.OneBot.

## Current Result

Webhook could not be used with a temporary tunnel because QQ rejected the callback domain as not filed/备案. The project switched to official WebSocket through `qq-botpy`.

Verified:

- `qq-botpy` connects in sandbox mode.
- Bot gateway startup succeeds.
- QQ private chat receives messages and replies.
- Robot name is `沈知栀 Celia Shen`.

## Implementation Notes

The local daemon now implements the official bot path far enough to test once credentials exist:

- inbound Webhook endpoint: `POST /qq/webhook`
- callback validation signature response
- callback request signature verification helper
- C2C and group-at event parsing
- access-token acquisition through `https://bots.qq.com/app/getAppAccessToken`
- C2C text send through `/v2/users/{openid}/messages`
- group text send through `/v2/groups/{group_openid}/messages`

The send client is tested with `httpx.MockTransport`, so no real QQ credentials are needed for unit verification.
