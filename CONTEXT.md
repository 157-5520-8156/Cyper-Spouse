# Girl-Agent Domain Glossary

## World

A continuous fictional life epoch centred on the companion. A World has one authoritative history and one Logical Time.

## World Event

An accepted, immutable record that something changed in the World. Correction is expressed by a later compensating World Event, never by rewriting history.

## Projection

A deterministic, rebuildable view derived from World Events. A Projection is not an independent source of truth.

## Logical Time

The World's event-recorded time. It may pause or advance at different rates and is distinct from wall-clock time.

## Character Fact

A maintained fact about the companion's stable identity, values, preferences, or boundaries. It does not prove that a particular life event occurred.

## User Fact

A sourced, confirmed fact about the user. A current User Fact may supersede an older one without deleting the older historical record.

## Plan

An intention or scheduled future activity that has not happened. A Plan is never an Experience.

## Proposal

A candidate produced by a model, rule, or recorded draw that has not yet been accepted as a World Event.

## Committed Experience

A referencable experience derived from a settled activity or confirmed shared event. Character background, model prose, failed delivery, and uncompleted Plans are not Committed Experiences.

## Action

A traceable attempt to produce an observable online or external effect. An Action has one terminal outcome: delivered, failed, cancelled, expired, or unknown.

## External Result

A recorded outcome from a model, random draw, media generator, tool, network, clock, or platform receipt that replay must not invoke again.

## Photo Candidate

A rebuildable Projection entry indicating that a Committed Experience may have enough visual and sharing value to become media. It is not permission to generate or send anything.

## Media Opportunity

The World's frozen selection of one Photo Candidate for possible rendering. It identifies one Committed Experience, chooses life-share or character-media, and sets a privacy ceiling.

## Media Plan

One evidence-bound, replayable photographic interpretation of a Media Opportunity. It selects exactly one primary visual subject, capture authorship, visual form, sharing intent, polish, tone, and privacy level without changing World facts or deciding whether to send.

## Media Inspection

A recorded visual assessment of media. It states whether the artifact is deliverable and describes what is actually visible, including deviations from the Media Plan.

## Appearance State

A time-bound World Projection of visible character facts such as current hair arrangement, outfit role, grooming, and accessories. It must be sourced from committed history and is optional in a Media Opportunity snapshot; generated media never writes it back automatically.

## Subject Presentation

The frozen, shot-local way a character appears and performs in one Media Plan: appearance source, head and shoulder orientation, gaze, expression, posture, gesture, and photo awareness. It is not a World fact or a media category, and identity references must not silently override it.

## Media Interaction Bid

The response a character hopes one delivered Media Plan may invite, including its communicative goal, hoped response, and response pressure. It is an invitation rather than a claim or obligation. Planning freezes it, but only a confirmed delivery may open a pending World interaction state.

## Media Address Strategy

The frozen, whole-image way a Media Plan addresses its intended recipient: observational or direct stance, engagement tactic, disclosure, staging, temporal beat, visual priority, and expression charge. It translates a Media Interaction Bid into photographic communication without deciding whether to send.

## Camera Geometry

The frozen physical camera contract for one Media Plan: distance, height, view axis, pitch, roll, orientation, subject occupancy and placement, environment share, focus, imperfection, and device visibility. It is independent of Capture Mode, which identifies who operates the camera.

Version 2 also freezes camera-to-face distance and the face's radial position in the frame so a front-camera image is not reduced to one arm-length ratio and wide-angle edge distortion can be reasoned about explicitly. Version 1 payloads remain immutable.

## Photo Display Strategy

The shot-local social performance used to make a Media Interaction Bid visually legible, such as playing innocent, sharing restrained pride, or presenting a mishap deadpan. It belongs to Subject Presentation, not Affect or World truth, and freezes a coherent expression recipe rather than independently composed facial axes.

## Facial Display Strategy

The semantic, recipient-facing family of one visible facial performance, such as amusement leaking, deliberate cuteness, mock defiance, tender privacy, or direct/withheld desire. It describes communicative display rather than inferred inner emotion.

## Facial Micro-Performance

The frozen visible actions of one still-frame facial beat: brow, eye aperture, current gaze, nose/cheek action, mouth, asymmetry, intensity, authorship, temporal phase, and energy. The name refers to fine-grained visible performance, not a scientific claim that a static image proves a temporal microexpression.

## Photographic Authenticity Profile

The frozen whole-image phone-photography behavior of one Media Plan: device rendering, exposure and color compromise, processing, scene orderliness, one credible capture imperfection, environmental entropy, regional grounding, and aesthetic intent. It never adds unsupported location facts and does not equate authenticity with blanket noise, blur, clutter, or poor quality.

## Relationship Stage

A slow projection of settled interaction history. It influences likely choices and their cost but does not grant the user control over the companion or act as a context-free vocabulary licence.

## Affect

A sourced, time-varying feeling and residual tendency. Affect influences deliberation and action but cannot authorize life facts.

## Character Core

The companion's stable identity, values, preferences, boundaries, and experience-supported long-term continuity.

## Self Core Projection

A deterministic summary of Character Core, current goals, relationship, and committed continuity. It is a read model, not a free-form write authority.

## User Request

The user's expressed preference for how the companion should speak or act. It is an input to deliberation, not an invariant the companion must obey.

## Appraisal

A structured interpretation of what an event means to the companion, such as care, pressure, offence, repair, or uncertainty.

## Drive

A current action motive such as care, autonomy, curiosity, irritation, repair, withdrawal, or desire to help.

## Stance

The companion's selected position after weighing requests, drives, relationship, Affect, values, and available Actions. Examples include comply, compromise, disagree, refuse, defer, or seek repair.

## Display Strategy

How the companion chooses to express or withhold a felt state: directly, cautiously, playfully, ironically, partially, or not yet.

## Conversation Thread

A sourced and expiring conversational commitment, question, concern, or unresolved matter. It must eventually resolve, cancel, or expire.

## Hard Invariant

A truth, Action, delivery, safety, privacy, legal, or consent rule that personality and user preference cannot override.

## Inner Advisory

A sourced, bounded, and non-authoritative signal about what may be influencing the companion, such as an Appraisal, Drive, Affect tendency, repair need, or candidate Stance. It may shape a Proposal but cannot write World truth or veto expression.
_Avoid_: Rule verdict, mandatory stance

## Context Capsule

A bounded, revision-pinned packet compiled from authoritative World Projections plus explicitly non-authoritative advisories for one Deliberation. It has a token budget and truncation log, and is not a second store of truth.
_Avoid_: Full-history prompt, free-form context dump

## Internal World Snapshot

A revision-pinned, read-only deep Projection containing the authoritative material required by WorldRuntime internals. It is produced by deterministic reducers and is never exposed as a viewer-facing projection or edited as a second source of truth.

## World Revision

The compare-and-swap revision advanced only by events that change authoritative World, Action, budget, or grant state. Draws and model audit records advance a separate deliberation revision so that a turn cannot invalidate its own Acceptance.

## Trigger Process

The effect-once processing lifecycle for one Observation, clock trigger, recovery item, or settlement input. Concurrent callers join the same process instead of independently deliberating and authorizing duplicate Actions.

## Action Intent

A stable-identity value object inside a Proposal describing a candidate external effect. It is not an Action and gains no execution authority until Proposal Acceptance creates an authorized Action.

## Action Reconciliation

A compensating record that resolves evidence about an Action already settled as unknown. It may establish an external outcome or budget correction, but it never reopens or re-executes the original Action.

## Behavior Tendency

A model-facing coordinate describing a plausible direction of action, such as maintain, explore, avoid, repair, or set boundary. It changes proposal likelihood but never mandates a visible response.
_Avoid_: Behavior rule, mandatory reaction

## Change Phase

A sourced, time-bound stage describing how the companion is departing from or returning toward baseline: baseline, preference deviation, stress response, relationship tension, or recovery. A single phase cannot rewrite Character Core.
_Avoid_: Mood label, personality rewrite

## Affect Episode

A sourced set of time-varying Affect components with versioned decay, residue, and lifecycle semantics. Surface expression does not implicitly resolve it, and replay uses Logical Time plus the recorded policy version.

## Relationship Adjustment

A sourced, accepted delta to one or more slow relationship variables under a versioned integrity policy. It records both proposed and accepted deltas and never dictates a particular visible response.

## Action Layer

The authority layer at which a proposed change belongs: internal state transition, World event, external Action, media Action, or read-only tool. Each layer has distinct commit and settlement semantics.

## Model Result

A versioned, hashed record of a bounded model call, including its purpose, input capsule identity, parsed payload, latency, usage, and failure metadata. Replay reuses it and never silently calls a live model.
_Avoid_: Unlogged model answer, replay-time inference

## Private Impression

The companion's fallible, source-bound interpretation of a user, relationship, or event. It carries confidence, possible counter-evidence, and an expiry or settlement condition, and is never a User Fact.
_Avoid_: Hidden fact, inferred user fact

## Private Commitment

An internal decision to keep caring about, remember, revisit, or later act on something. It may open a Conversation Thread or produce an Action Proposal, but it is neither a completed Plan nor a Committed Experience.
_Avoid_: Completed intention, hidden experience

## Expression Beat

One independently dispatchable and settleable fragment in an ordered, interruptible expression. A Beat may depend on an earlier receipt and may be cancelled or reconsidered when the user interjects.
_Avoid_: Text chunk, random split
