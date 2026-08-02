# Barge-in and cancellation semantics in voice/conversation systems

Updated: 2026-08-02

## Question

When a user starts speaking while an agent is producing or playing a reply,
what triggers the interruption, how is the old generation cancelled, how are
late events and duplicate responses prevented, and is a second model required?

This note uses first-party API documentation, standards, and vendor-owned
open-source documentation. “Barge-in” is used for the user taking the floor
while agent output is active. Stopping playback and cancelling the producer are
treated as separate operations.

## Bottom line

The consistent industry shape is a cancellable **reply episode** with three
distinct boundaries:

1. **Speech start / overlap detection**: VAD or an equivalent detector says the
   user may be taking the floor. This is the low-latency trigger for stopping
   audible output.
2. **Endpoint / speech stop**: VAD silence, STT end-of-utterance, or a semantic
   turn detector says the new user turn is complete. This is when a new reply
   may be generated.
3. **Cancellation and commit**: the old response and its audio queue are
   cancelled or made stale, and only the new turn may commit visible output.

Vendors do not put all of this in WebRTC, RTP, or SIP. The transport can stop a
track or cancel a protocol transaction, but the model/output pipeline needs an
application-level response identifier or equivalent ownership token. A useful
local invariant is **one active visible response per conversation lane**, with
idempotent cancellation and stale-output rejection.

A second model is optional. Basic VAD plus endpointing is sufficient for a
responsive baseline. A semantic turn detector or adaptive overlap classifier
is useful when false barge-ins (backchannels, noise, short pauses) are costly;
it should classify the interruption/endpoint, not author a second reply.

## Primary-source comparison

| System | Trigger and endpoint | Cancellation / playback semantics | Concurrency and identity evidence | Extra model required? |
| --- | --- | --- | --- | --- |
| **OpenAI Realtime API** | `input_audio_buffer.speech_started` is emitted by server VAD when speech is detected; `speech_stopped` marks the end. Server VAD and semantic VAD expose `interrupt_response`, `create_response`, silence/threshold or semantic eagerness controls. | Server VAD can automatically cancel an ongoing default-conversation response. Client `response.cancel` cancels a chosen `response_id` and ends with `response.done(status=cancelled)`. On WebRTC/SIP, `output_audio_buffer.clear` cuts already-buffered audio and must follow `response.cancel`. | `response_id` identifies the response; cancelled reasons include `turn_detected` and `client_cancelled`. Only one response may write to the default conversation, although out-of-band responses can run in parallel and should use metadata to disambiguate. | No for server VAD; semantic VAD is an optional turn-detection model for better endpoint timing. |
| **LiveKit Agents** | Turn detection covers both end-of-user-turn and user speech mid-response. Modes include VAD-only, STT endpointing, a turn-detector model, realtime-model server detection, and manual control. | User speech stops current agent speech by default. `session.interrupt()` or a `SpeechHandle.interrupt()` explicitly interrupts; the framework truncates conversation history to the portion of speech the user heard. `allow_interruptions=false` disables interruption for selected speech. | The public API exposes a current speech handle/session interrupt and interruption events rather than a wire-level response ID. The framework owns cancellation and transcript truncation; an integrating pipeline still needs a local episode/turn token for late async work. | No for VAD mode or realtime models with their own detection. LiveKit Cloud's adaptive model runs after VAD to distinguish genuine barge-in from backchanneling. |
| **Google Dialogflow CX** | Fulfillment/agent/page-level barge-in lets end-user speech interrupt response audio. `BargeInConfig` specifies a no-barge-in phase and a barge-in phase while the client streams input audio during playback. | When interrupted, Dialogflow stops sending audio and processes the next user input. The client is explicitly told to stop playback and prepare for the current request. Barge-in-enabled message queues can stop all following queued messages; message cancellation is also configurable. | The API describes queue and phase behavior, but does not expose an LLM generation ID or a general response-cancel event. The integration must not assume that stopping audio alone stops application-side generation. | No separate model is required by the barge-in configuration; speech detection is provided by Dialogflow. |
| **Twilio ConversationRelay** | `interruptible` controls whether caller speech/DTMF stops TTS (`none`, `dtmf`, `speech`, `any`); `interruptSensitivity` uses confidence and input length. `speechTimeout` is the silence/end-of-prompt boundary. | Twilio sends an `interrupt` WebSocket message containing the utterance and duration heard at interruption. `preemptible` controls whether later application tokens replace current media. `interruptible` and `reportInputDuringAgentSpeech` are deliberately independent. | Messages have talk-cycle fields (`last`, `preemptible`) but the documented interrupt event has no generation ID. The application must bind it to its own episode and stop/cancel its LLM/TTS work. | No mandatory second model; sensitivity and provider STT/endpointing are the available controls. |
| **Apple Speech framework** | An `SFSpeechRecognitionTask` streams partial/final recognition results asynchronously; the app decides when to finish or cancel recognition. | `SFSpeechRecognitionTask.cancel()` cancels the current recognition task; `canceling` means result delivery has ended even though audio recording may still be ongoing. This is input-recognition cancellation, not cancellation of an assistant reply. | Task object/state is the identity boundary. Reply generation/playback still needs a separate owner token. | No, but this API alone does not implement barge-in for generated output. |

## What the sources say

### OpenAI Realtime: explicit response cancellation and single-writer scope

The Realtime API's server VAD emits `input_audio_buffer.speech_started` as soon
as speech is detected and says the client may use it to interrupt playback;
`speech_stopped` identifies the end of speech. Its turn-detection settings make
the distinction explicit: `interrupt_response` controls whether an ongoing
default-conversation response is cancelled at VAD start, while `create_response`
controls whether a response is created at VAD stop. Server VAD uses silence and
threshold parameters; semantic VAD adds model-estimated turn completion and an
eagerness setting. ([Realtime server events and turn detection](https://developers.openai.com/api/reference/resources/realtime#realtime-server-events-input-audio-buffer-speech-started), [Realtime session turn detection](https://developers.openai.com/api/reference/resources/realtime#realtime-session-turn-detection))

For an explicit client decision, `response.cancel` accepts an optional
`response_id`, returns `response.done` with `status=cancelled`, and is safe in
the sense that an absent response leaves the session unaffected (with an
error). This is a real producer cancellation, not merely a UI mute. For
WebRTC/SIP, `output_audio_buffer.clear` is a second operation that cuts audio
already buffered for playout; the API says it should be preceded by
`response.cancel`. ([Realtime client events: `response.cancel` and `output_audio_buffer.clear`](https://developers.openai.com/api/reference/resources/realtime#realtime-client-events-response-cancel), [Realtime server events: cancelled response details](https://developers.openai.com/api/reference/resources/realtime#realtime-server-events-response-done))

The response model has an important concurrency rule: only one response can
write to the default conversation at a time, while out-of-band responses may
run in parallel and should be disambiguated with metadata. A cancelled response
records whether it was caused by server turn detection or a client cancel.
That combination—explicit identity, one default-conversation writer, and a
terminal cancellation event—is a strong reference design for preventing stale
tokens from becoming a second visible reply. ([Realtime client events: `response.create`](https://developers.openai.com/api/reference/resources/realtime#realtime-client-events-response-create), [Realtime server events: `response.done`](https://developers.openai.com/api/reference/resources/realtime#realtime-server-events-response-done))

### LiveKit Agents: interruption is part of the turn manager

LiveKit defines turn detection as both “the user finished speaking” and “the
user started speaking mid-response.” It supports VAD-only, STT endpointing,
semantic/audio turn-detector, realtime-model server detection, and manual turn
control. When a realtime model owns server-side detection, LiveKit forwards user
audio and reacts to the model's interruption signal instead of applying its
local interruption thresholds. ([Turns overview](https://docs.livekit.io/agents/logic/turns/))

In the default mode, user speech pauses agent speech. The documented
`session.interrupt()`/`SpeechHandle.interrupt()` path stops current speech and
truncates conversation history to what the user actually heard before the
interruption. Speech can opt out with `allow_interruptions=False`. This is a
useful separation between delivery state (heard audio) and generated text:
the interrupted assistant prefix is not silently treated as fully delivered.
([Interruptions and manual turn control](https://docs.livekit.io/agents/logic/turns/))

LiveKit's adaptive interruption handling is explicitly a second classifier only
when needed: VAD first identifies incoming audio, then an acoustic model
distinguishes intentional barge-ins from “uh-huh/okay/right” backchannels. VAD
mode remains available, and realtime models generally use their own detection.
([Adaptive interruption handling](https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling/))

The public abstraction is a current speech handle/session rather than a
standardized response ID. That is sufficient inside the framework, but an
application that has separate LLM, tool, TTS, or queue tasks should add its own
monotonic episode/turn identifier and reject completions from older episodes.
This last sentence is an integration recommendation, not a claim that LiveKit
requires a particular token format.

### Google Dialogflow CX: stop output and process the next input

Dialogflow CX's advanced speech settings say that when barge-in is enabled, an
end user can interrupt response audio; Dialogflow stops sending audio and
processes the next end-user input. If multiple response messages are queued,
barge-in can propagate to following messages and stop their playback as well.
The same page describes a separate cancellation option for queued fulfillment
messages. ([Dialogflow CX advanced speech settings](https://docs.cloud.google.com/dialogflow/cx/docs/concept/advanced-speech))

The REST `QueryInput.BargeInConfig` makes the client timing contract more
concrete: while the client is playing previous response audio and streaming
input, the API has a no-barge-in phase followed by a barge-in phase; once an
utterance is detected, the client should stop playback and immediately prepare
for the current request. ([Dialogflow CX `BargeInConfig`](https://docs.cloud.google.com/dialogflow/cx/docs/reference/rest/v3beta1/QueryInput#bargeinconfig))

Neither page promises a general-purpose cancellation of an arbitrary LLM task
owned by the integrator, nor does it expose a model-generation ID. Therefore a
Dialogflow integration should treat the API's stop-sending-audio signal and
the application’s own generation cancellation as separate, idempotent actions.

### Twilio ConversationRelay: interrupt events and preemptible media

ConversationRelay exposes three deliberately separate controls. `interruptible`
decides whether caller speech/DTMF stops TTS; `reportInputDuringAgentSpeech`
decides whether the application receives prompt/DTMF messages while the agent
is speaking; and `preemptible` decides whether later text/media from the
application replaces current playback. `interruptSensitivity` trades fast
barge-in against confidence and input length, while `speechTimeout` supplies an
end-of-prompt silence boundary. ([ConversationRelay TwiML attributes](https://www.twilio.com/docs/voice/twiml/connect/conversationrelay))

When speech interrupts TTS, Twilio sends a WebSocket `interrupt` message with
`utteranceUntilInterrupt` and `durationUntilInterruptMs`. The text-token
contract recommends streaming tokens and provides `last`, `interruptible`, and
`preemptible` fields. The docs do not say that an application LLM is cancelled
automatically, so the application should use the interrupt event to cancel its
current generation/TTS producer and to invalidate any queued tokens. ([ConversationRelay WebSocket messages](https://www.twilio.com/docs/voice/conversationrelay/websocket-messages))

### Apple, WebRTC, and SIP: cancellation exists at lower layers, but not as a
reply-generation protocol

Apple's `SFSpeechRecognitionTask` has an explicit `cancel()` and a
`canceling` state, but that state concerns recognition result delivery and
recording, not assistant output. ([Apple `SFSpeechRecognitionTask`](https://developer.apple.com/documentation/speech/sfspeechrecognitiontask))

The WebRTC specification's `RTCRtpSender.replaceTrack(null)`/`removeTrack()`
stops sending a media track; it does not name or cancel the server-side model
that produced already-buffered audio. ([W3C WebRTC, `RTCRtpSender`](https://www.w3.org/TR/webrtc/#rtcrtpsender-interface))

SIP `CANCEL` is similarly narrower: RFC 3261 says it requests cancellation of a
pending transaction, must be matched to the original transaction, and is a
no-op after a final response. The original and CANCEL transactions complete
independently, and sending CANCEL before the required provisional response can
create a race. It is not a barge-in or LLM-generation primitive. ([RFC 3261, section 9.2](https://www.rfc-editor.org/rfc/rfc3261#section-9.2))

## Implications for Girl-Agent

These are transport/runtime guardrails, not character behavior rules:

1. **Trigger quickly, commit later.** On a credible speech-start/overlap event,
   stop or clear audible output and mark the active reply episode interrupted.
   Use speech-stop plus endpointing to decide when the new inbound turn is
   complete enough to pass to the same character model.
2. **Give each episode an owner token.** A monotonic `turn_id`/`generation_id`
   (or provider `response_id` where available) should be attached to LLM
   streams, tool continuations, TTS chunks, queued bubbles, and terminal
   callbacks. Cancellation increments/replaces the owner; any late chunk whose
   ID is not current is dropped.
3. **Make cancellation idempotent and layered.** Cancel the producer, stop
   media already queued for playback, and reconcile the transcript to what was
   actually heard. Repeated cancel requests must not create a new reply or
   mutate the conversation twice.
4. **Enforce one visible writer.** Automatic VAD response creation and manual
   response creation must not both run for the same endpoint. A CAS/lock around
   the active episode and an effect-once commit for visible output are simpler
   and safer than trying to infer ownership from arrival order.
5. **Use a second model only for uncertainty.** Start with VAD plus measured
   endpointing. Add an adaptive/semantic detector if backchannels, noise, or
   language-specific pauses produce unacceptable false interruptions. That
   detector may classify “yield now?” or “turn ended?”; it must not become a
   second character-response path or bypass source-bound fact/permission checks.

## Sources

- OpenAI, Realtime API reference: https://developers.openai.com/api/reference/resources/realtime
- LiveKit Agents, Turns overview: https://docs.livekit.io/agents/logic/turns/
- LiveKit Agents, Adaptive interruption handling: https://docs.livekit.io/agents/logic/turns/adaptive-interruption-handling/
- Google Cloud Dialogflow CX, Advanced speech settings: https://docs.cloud.google.com/dialogflow/cx/docs/concept/advanced-speech
- Google Cloud Dialogflow CX, `QueryInput.BargeInConfig`: https://docs.cloud.google.com/dialogflow/cx/docs/reference/rest/v3beta1/QueryInput#bargeinconfig
- Twilio, `<ConversationRelay>`: https://www.twilio.com/docs/voice/twiml/connect/conversationrelay
- Twilio, ConversationRelay WebSocket messages: https://www.twilio.com/docs/voice/conversationrelay/websocket-messages
- Apple, `SFSpeechRecognitionTask`: https://developer.apple.com/documentation/speech/sfspeechrecognitiontask
- W3C, WebRTC Recommendation: https://www.w3.org/TR/webrtc/#rtcrtpsender-interface
- IETF, RFC 3261 SIP: https://www.rfc-editor.org/rfc/rfc3261#section-9.2
