# Conversation Rhythm Research Notes

Updated: 2026-07-11

## Decision

Treat *when* a message appears as daemon-owned behavior, separate from the LLM
that writes it. A reply is a cancellable delivery episode rather than one model
completion plus a `sleep()`.

The local implementation keeps the existing lightweight coalescer instead of
pulling in another agent framework. It adopts three useful patterns:

1. **Turn-transition windows:** a gap between bubbles is a semantic window,
   not transport latency. It permits acknowledgement, takeover, or continuation.
2. **Temporal response policy:** deciding whether and when to speak must be
   conditioned on the current interaction, life state, and elapsed time; it is
   not only a prompt instruction.
3. **Interruptible queues:** later output is tentative until it is delivered.
   A new substantive user turn cancels or replaces pending output; a light
   backchannel does not necessarily do so.

## Applied Local Policy

| Situation | Daemon behavior |
| --- | --- |
| One compact reply | One bubble; no manufactured follow-up |
| Multi-sentence reply | Split only when it creates a useful turn boundary, then wait 1.8--7.2 s |
| User says “嗯/对” in the window | Record it as feedback and continue the planned thought |
| User asks, corrects, emotes, or says “等下” | Cancel unsent bubbles, re-coalesce and answer the new turn |
| Rich open-ended turn | May start a 0--2 stage continuation episode |
| New user activity | Cancel every remaining stage of that in-memory episode |
| Adapter restart during delayed reply | Recover the task with the same segment timing; do not burst-send |

The next planned increment is persistent continuation episodes. They need an
episode ID, stage count, expiry, and cross-adapter cancellation token before
being delegated to the periodic proactive scheduler. Until then, only the
short-lived coalescer owns sub-minute continuation timing, which avoids a
coarse scheduler replaying stale text after the user has resumed elsewhere.

## Sources

- Threlkeld, Umair, and de Ruiter, “Using Transition Duration to Improve
  Turn-taking in Conversational Agents,” SIGDIAL 2022:
  https://aclanthology.org/2022.sigdial-1.20/
- “From What to Respond to When to Respond: Timely Response Generation for
  Open-domain Dialog Agents” (TimelyChat / Timer):
  https://openreview.net/forum?id=Nc3S0h3oYc
- GitHub Copilot SDK steering and queueing semantics:
  https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/steering-and-queueing
