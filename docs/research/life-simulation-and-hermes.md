# Continuous Life Simulation and Hermes Model Notes

Date: 2026-07-10

## Question

Can the companion become more believable by maintaining an event-driven private
life, including a distinct `delivered -> unread -> read -> reply` path? What can
be reused from EchoText/SillyTavern, and should Hermes replace the current
DeepSeek chat model?

## Primary-source findings

### EchoText

EchoText is a SillyTavern texting-side-channel, rather than a cross-platform
daemon. Its useful core is still highly relevant: it keeps a nine-axis Plutchik
emotion state, applies natural decay, long-term affinity drift, opposing-emotion
suppression, intensity amplification, and lets reactions affect state. It also
offers a trigger-driven proactive system, memory rotation, image prompting, and
read/ghost timing. The source README describes these mechanisms and their
configuration directly:

- https://github.com/mattjaybe/SillyTavern-EchoText
- Local installed source:
  `external/SillyTavern/data/default-user/extensions/SillyTavern-EchoText/`

Its optional server plugin is the important operational lesson: browser timers
are not reliable when a page is backgrounded, so it moves trigger evaluation to
a server-side `setInterval`, preserving per-character state, cooldowns, trigger
history, emotion ghost windows, and a deferred generation queue. The project
source is installed locally at
`external/SillyTavern/plugins/SillyTavern-EchoText-Proactive/index.js`.

- https://github.com/mattjaybe/SillyTavern-EchoText-Proactive

EchoText is not a complete solution for this project because it does not own QQ
small-account delivery, cross-platform identity, local budget policy, or a
durable companion-specific event ledger.

### Hermes Agent and Hermes models

Hermes Agent is a different layer from a companion chat model. Its documented
memory design is bounded profile/agent memory plus searchable historical
sessions, optimized for tool-using personal agents and prefix-cache stability.
It is useful as a reference for separate compact self/user core memories and
on-demand retrieval, but should not replace the daemon's relationship state or
conversation ledger.

- https://github.com/NousResearch/hermes-agent
- https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md

Hermes 3 was trained substantially around instruction following and structured
function calling. That can make it an attractive optional model for tool use or
unfiltered roleplay, but it does not itself provide continuous life, read state,
or reliable long-term companion memory. Those properties must remain in the
daemon and be covered by deterministic tests.

- https://nousresearch.com/wp-content/uploads/2024/08/Hermes-3-Technical-Report.pdf

## Current project audit

Already in live code:

- EchoText-style nine-axis emotion vector, baseline, decay, affinity drift,
  opposition suppression, intensity scaling, and anger/disgust ghost windows.
- Trigger timeline, semantic cooldowns, jittered scheduling, unanswered-message
  guards, and daemon-side scheduling rather than browser scheduling.
- Deterministic context selection, memory extraction/deduplication, compact
  self-core, reply timing, split-message timing, follow-up cancellation, and
  image/reaction handling.
- A `life_continuity` prompt line that describes the current time-of-day phase.

Only partially implemented:

- `human_rhythm.py` chooses a plausible activity from the clock and mood each
  time it is asked. It is not a scheduled event that starts, continues, ends,
  succeeds, or gets interrupted.
- `has_unread` currently means a delayed/withheld reply exists. It is not a
  durable phone attention state, so it cannot model delivered-but-unread,
  notification-driven reading, or an interrupted activity.
- Life events are generated close to delivery time. Although successful events
  are written to memory, they are not selected from a prior planned timeline.

## Recommended next architecture

Add a daemon-owned `life runtime` rather than expanding the reply prompt.

1. `life_events`: planned/private events with start/end, activity, location
   class, attention demand, interruptibility, outcome, and provenance.
2. `phone_state`: `away`, `notified`, `glanced`, `reading`, `typing`, and
   `do_not_disturb`; it records timestamps rather than pretending a message was
   read immediately.
3. Event wake-up: the proactive scheduler advances events and phone state on a
   jittered cadence. It may create a small low-cost deterministic event, finish
   one, notice a notification, or decide to leave a message unread temporarily.
4. Incoming message handling: message arrives -> notification policy -> either
   read now, schedule a glance, or stay unread. A new urgent/direct question,
   a second message, or the end of the current activity can pull the message
   forward. This decision updates state before a reply is generated.
5. Delivery closes the loop: only a successfully delivered message can create a
   shared/lived event. An unsent plan remains private and never becomes history.

The first version should use a deterministic library of daily event templates
plus seeded variation. An LLM should be used only for rare, approved
event-specific details; it should never silently invent an entire day on each
reply. This keeps continuity inspectable and within the monthly budget.

## Model recommendation

Do not replace the daemon with Hermes Agent. Keep the daemon as the state,
memory, scheduling, and delivery authority.

If an OpenAI-compatible Hermes endpoint is available, add it as a configurable
model profile and run the existing dialogue/context evaluations against both
DeepSeek and Hermes before changing production traffic. Prefer a routing setup:

- inexpensive model for deterministic background classification is unnecessary;
  keep that local/rule-based;
- primary chat model selected per profile;
- optional stronger/less-restricted model only for explicit adult roleplay,
  behind a separate opt-in and the same state/memory/postprocessing gates;
- tool calls remain confirmation-gated regardless of model.

The key risk is not only cost: a more expressive model can produce more vivid
but unsupported autobiographical claims. The event ledger and grounded-context
checks become more important, not less.
