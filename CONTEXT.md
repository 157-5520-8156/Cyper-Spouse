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

## Biographical Context

A source-bound reading of age, academic/calendar phase, current residence
context, and active Life Arcs at one Logical Time. It is derived from reviewed
timeline facts plus accepted World Events; it is not copied from a static
persona prompt and does not decide what the companion should do.
_Avoid_: Static age label, behavior script

## Life Arc

An accepted, long-lived chapter such as an internship, job, residence, trip,
or sustained personal undertaking. A Life Arc contributes a reviewed Context
Pack while active and may introduce or retire places, NPCs, and activity
possibilities. It starts only from an authoritative settled consequence and
ends through an explicit event; daily activities remain separate Plans and
Experiences.
_Avoid_: Daily activity, unsourced backstory, permanent persona rewrite

## Major Biographical Transition

An accepted, durable change to a foundational life coordinate such as
education status, work status, or primary residence, which changes the future
possibilities available in the World. A Life Arc may express the chapter that
follows, but cannot replace a foundational coordinate. An accepted occurrence
settlement may carry either an objective coordinate consequence already made
true by its exact selected branch, or a separate Character-Model-authored
subjective direction. Objective and subjective namespaces are disjoint. Prose,
unselected World-Author options, or ordinary Life Arc tags cannot substitute
for the transition or silently override it. Active aspirations are sourced
inner context, not catalog-to-plot mappings: only a Character-accepted open
Plan may explicitly bind the aspiration's planting event and atomically
crystallize it.
_Avoid_: Prose-only Life Arc, daily Plan, retroactive backstory, predetermined life path

## Context Pack

A source-bound bundle of capabilities and environmental coordinates attached
to a Life Arc or biographical phase. It says which resources, places, people,
and effects are currently available; it neither supplies a finite activity
menu nor instructs the character to choose a development.
_Avoid_: Plot script, mandatory routine

## Life Influence

A sourced interaction, memory, relationship change, Affect episode, or
unfinished matter made visible to life deliberation. It may change what the
character notices or chooses, but the system never maps an influence directly
to an activity. A user's place mention may become inspiration for a future
Plan; it never proves that the companion visited.
_Avoid_: Keyword-to-event rule, retroactive Experience

## World Author

A model authority that may propose fictional environmental opportunities,
contingencies, provisional people, and objective outcomes inside the
companion's World. It cannot decide the companion's motives or responses, and
its output remains a Proposal until admitted and settled.
_Avoid_: Plot director, character model, deterministic event table

## Capability Manifest

A revision-pinned declaration of the World effects currently available to a
model, including their scope, consequence budget, and required evidence. It
describes what may be proposed without suggesting what should happen. A
location capability binds an opaque stable ref to the exact place, privacy
floor, local schedule or accepted-Plan interval, admission kind, and authority
source; a current-presence snapshot carries a finite wake-bound horizon rather
than promising future presence. A bare location ID is never executable
permission.
_Avoid_: Plot menu, behavior recommendation

## Life Review Evidence Packet

A minimal, capsule-bound input to the independent Life source reviewers.
General closure receives only its frozen, lane-owned World Author fields, exact
immutable events cited by existing-world claims, the exact selected location
capability, existing-entity refs, and the full manifest/cursor identity. Outcome
text enters this lane only when a typed location needs an exact consistency
coordinate. An entity ref gains descriptor evidence only through an exact
structural ref join to a source-bound item in the already selected Context; the
join never guesses a name or alias, and a missing match is non-evidence.
Focused novel-origin review additionally receives every item already selected
and bounded by the relevant character, current-life, dialogue, relationship,
thread, appraisal, Affect, accepted-fact, recent-experience, world-life,
Private-Impression, and perception slices. Source-bound item hashes are checked
against the transported value. A projected baseline preserves the original
Capsule item hash separately from the hash of the review-visible value and can
only signal uncertainty; it cannot prove a claim or rejection. Unconsolidated
Memory Candidates are non-authoritative and excluded. The compiler removes
transport-only resolver, rank, and budget metadata, but never keyword-filters,
re-ranks, or applies a second item cap. Focused manifest descriptor entries are
hash-bound pointers into those same transported items, not duplicated semantic
copies. Packet contract plus exact request bytes bind current replay identity;
historical proposal versions retain only their historical identity formula, so
changed evidence or compiler bytes cannot reuse an old result. Its compiler has
no verdict or character behavior authority. Life and
visible conversation may reuse provider route configuration and an HTTP pool,
but their reviewer circuits, suppression state, active tasks, and shutdown
leases are separate. Locally installed strict JSON schemas remain distinct from
release-qualified route evidence.
_Avoid_: Full creative Context copied into a classifier, local fact verdict,
background failure suppressing visible review, unqualified route reported ready

## Provisional NPC

A model-authored person that exists only inside a Proposal or unsettled
occurrence. It becomes a referencable World NPC only after the introducing
occurrence settles with sufficient evidence. A proposal-scoped novel place is
likewise not reusable before settlement. After the introducing outcome settles,
its exact source-reviewed descriptor may materialize as a typed `attempt_only`
place capability; missing or hash-invalid descriptor bytes fail closed. This
proves only stable place identity and permission to attempt a later visit, never
opening hours, access, or a completed visit.
_Avoid_: Pre-registered future acquaintance, invented historical fact

## Outcome Resolution Envelope

A frozen authorization for resolving an unsettled occurrence from later
evidence within a bounded set of World capabilities. The current implementation
stores a small set of possibilities authored afresh by the World Author for
that exact context, including their relative plausibility and effect bounds.
This is not an operator-authored plot catalog, but it is intentionally frozen
before settlement so replay cannot silently change the past. A subjective
long-term direction may only be authored freely by the Character Model while
resolving an objective result; World Author does not offer direction choices or
motives. The character may author one structurally closed subjective-direction
coordinate replacement or none. A candidate may separately declare an open,
objective biographical consequence only when the independent source review
finds it entailed by that exact branch; any resolution authority can install it
only by selecting and settling that branch. The validated model audit, exact choice, optional direction,
Outcome Proposal, Acceptance, Settlement, and appraisal trigger share one
pinned CAS transaction; old Context is never carried across a later Clock.
_Avoid_: Operator-authored ending list, predetermined selected result

## Proposal

A candidate produced by a model, rule, or recorded draw that has not yet been accepted as a World Event.

## Committed Experience

A referencable experience derived from a settled activity or confirmed shared event. Character background, model prose, failed delivery, and uncompleted Plans are not Committed Experiences.

## Action

A traceable attempt to produce an observable online or external effect. An Action has one terminal outcome: delivered, failed, cancelled, expired, or unknown.

## External Result

A recorded outcome from a model, random draw, media generator, tool, network, clock, or platform receipt that replay must not invoke again.

## External Signal

A source-bound, versioned claim that something outside the World may be
happening or becoming salient. It records what a source reported, with time,
place, confidence, evidence, expiry, and correction lineage; it is neither a
World Event nor proof that the companion perceived or believed it.
_Avoid_: News fact, World Event, character knowledge

## World Perception Hub

The bounded context that acquires, normalizes, clusters, and retrieves
External Signals from replaceable sources. It may offer a sourced perception
opportunity, but it cannot decide the companion's attention, interpretation,
life response, or communication.
_Avoid_: News engine, event generator, behavior trigger

## Perception Candidate

An ephemeral, source-bound External Signal that is plausibly accessible or
relevant to the companion at one Pinned Turn. It is advisory input to
attention and may expire without entering the World ledger.
_Avoid_: Perceived fact, mandatory stimulus

## Perception Channel

A source-bound capability explaining how the companion could plausibly
encounter an External Signal at one Pinned Turn, such as a public alert,
available online feed, current-place medium, accepted NPC report, or authorized
search result. It proves access, not attention, belief, interest, or action.
_Avoid_: Behavior trigger, invented browsing history, source authority

## Perception Window

A frozen, expiring packet of exact External Signal revisions, correction and
conflict evidence, and available Perception Channels offered to one
source-bound character attention attempt. It is not a Context Capsule, a World
fact, or proof that any candidate was noticed.
_Avoid_: News digest, character knowledge, behavior menu

## External Perception

An accepted World Event recording that the companion encountered a particular
revision of an External Signal through a plausible channel. It proves the
perceptual experience and its evidence lineage, not that every source claim
was objectively correct or that the companion must act on it.
_Avoid_: Internet truth, automatic life event, mandatory response

## Photo Candidate

A rebuildable Projection entry indicating that a Committed Experience may have enough visual and sharing value to become media. It is not permission to generate or send anything.

## Media Opportunity

The World's frozen selection of one Photo Candidate for possible rendering. It identifies one Committed Experience, chooses life-share or character-media, and sets a privacy ceiling.

## Media Plan

One evidence-bound, replayable photographic interpretation of a Media Opportunity. It selects exactly one primary visual subject, capture authorship, visual form, sharing intent, polish, tone, and privacy level without changing World facts or deciding whether to send.

## Media Render Profile

A versioned, frozen declaration of how one Media Plan is rendered: its model ecosystem, rendering route, identity-binding method, supported controls, cost policy, and capability status. A Media Render Profile may only claim a capability proved for that exact route; it does not select an event, reinterpret a plan, or decide delivery.

## Identity Binding

The precise mechanism by which a Media Render Profile preserves character identity: `reference_edit`, `style_reference_unverified`, or `compatible_lora`. Identity Binding is a renderer capability, not a property of an image file; a LoRA cannot be presumed compatible across model ecosystems or provider routes.

## External Generation

One persisted submission of a frozen Media Plan to an external rendering provider. It records the render profile, provider receipt, request hash, terminal result, cost and artifact hash so replay never resubmits or changes the plan.

## Media Lane

The frozen semantic kind of one new character-media plan: `ordinary_life` is pure event sharing; `alluring_life` is event-grounded life sharing with visible feminine or hormonal expression; `exclusive_private` is the legacy evidence-bound recipient display; `suggestive_private` and `explicit_private` are separately routed high-private render lanes. `explicit_reserved` is a historical non-renderable proposal value. A Media Lane is suggested by the planner model and accepted only by deterministic candidate, capture and privacy checks; upstream authorization, relationship and delivery policy are not reimplemented here.

## Suggestive Private Media

`suggestive_private` is the adult-fictional, recipient-exclusive high-private Media Lane for a strongly recipient-directed private expression. It reuses the ordinary Media Plan and photographic contracts and freezes a dedicated rendering route. It never silently degrades to a normal renderer or lower Lane.

## Private Render Contract

The replayable high-Lane portion of a new Media Plan: lane, attraction mechanism, framing mode, coverage mode, visibility tier and dedicated renderer route. It is not an authorization decision; the upstream environment owns authorization, relationship and sending policy. Legacy Suggestive Media Authorization payloads remain readable only for replay of historical plans.

## Suggestive Private Contract

The historical v1 high-Lane contract containing a frozen authorization. It is retained solely to replay old Media Plans; new plans use `PrivateRenderContract`.

## Recipient Access

The frozen intended scope of one Media Lane: ambient, recipient-directed, or recipient-exclusive. It describes how the image addresses its audience, not who is allowed to possess the artifact after delivery.

## Attraction Expression

The frozen communicative intensity of a Media Lane: `none`, `feminine`, `charged`, `sexual_suggestive`, `explicit_adult`, or the historical reject value `explicit_reserved`. It is distinct from visible skin, outfit category and relationship stage; those remain separate evidence and upstream policy concerns.

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

## Current Self State

A compact, source-bound model view derived from the verified Context Capsule
for one Pinned Turn. It keeps Character Core, current Situation, active
Appraisals, concurrent Affect components, relationship state, unresolved
Threads, and non-authoritative Inner Advisories legible together. It is not a
second truth store, does not flatten mixed feelings into one mood label, and
keeps each emotional component bound to its accepted Appraisal cause. Local
semantic advisories may contribute readable weighted candidates only after
source binding; raw model drafts and operational scheduling verdicts never
enter this state. A production-visible turn may finish before its durable
local Appraisal/Affect follow-up; once that source-bound follow-up is accepted,
subsequent turns include it here. Local schema correction may ask the same
model to reselect a valid value, but deterministic code never maps message
keywords to Affect or invents an emotion-display relation. Its bounded recent
self-experience view preserves representatives from both immediate World Life
occurrences and committed Experiences so activity churn cannot erase durable
personal experience from the character's own attention. The background local
Appraisal lane receives only logical time, this sourced Current Self State, at
most four recent dialogue items, and the current Observation; this is a
model-facing performance projection, while the complete Capsule remains the
validation and replay authority.
_Avoid_: Current personality rewrite, emotion-to-expression rule

## Private Turn State

A concise, free-text, role-model-owned account of what is salient to the
companion as she forms one Expression choice. It is a required member of the
same model result that selects silence, timing, questions and Expression
Beats, and its attended source refs must come from that Pinned Turn. JSON
object member order is transport serialization, not evidence of causal order.
It is
audit material bound into Proposal identity, not hidden chain-of-thought,
World truth, a behavior category, a reply-mode switch, or a durable memory.
Attended refs record only what material was available to the character's
attention; they are not fact evidence and the private summary does not enter a
semantic truth review. Any external material later selected into visible text,
a World claim, an Action payload, Memory, Relationship, or another durable
effect must establish source closure again at that effect-bearing boundary and
cannot inherit authority from this private state.
When the character chooses bounded Recall, she forms one Private Turn State
before requesting it and a new one from the augmented Context before the final
Expression.
_Avoid_: Post-hoc rationale, response policy, question quota, motive enum

## Expression Reliability Lifecycle

The durable, effect-once processing state for one inbound Expression choice.
It is opened and claimed atomically with the Observation, binds each provider
attempt to its Model Result, and distinguishes a character-authored silent
Proposal from technical failure. A crash before a model result resumes the
same failure ordinal: only that live Runtime instance may continue generation
before its 120-second provider in-flight lease expires, while another Runtime
waits to reclaim it. Snapshot, Context, or current-self preparation failure is
itself a source-bound technical Model Result, so it cannot busy-spin an
unaudited claim. The short ownership lease is independent of terminal
technical retry deadlines, which remain 10, 30, then 120 minutes; and a
durable now/later/silent Proposal may be continued exactly by any Runtime
under CAS and effect-once without regenerating prose. It owns recovery and
liveness only—it cannot choose whether the character speaks. A candidate
from an older Observation is atomically superseded when a newer
user Observation commits if it has not crossed the Action authorization
boundary; this closes the old lifecycle without manufacturing another attempt.
An already authorized Action remains under its original dispatch and
settlement authority. A combined cognition candidate may be reused only under
the exact originating ModelInput identity;
if its call id, cursor, Capsule, route, or model-facing Context changes, the
role model must be invoked again and the new Model Result must retain that
actual request lineage rather than relabelling cached bytes or usage. Each
provider invocation—including Recall, correction and reselection—has an
identity derived from the messages and temperature the provider actually saw;
follow-up calls retain their parent-call relation. A later cursor also requires
a newly compiled, source-bound advisory. Technical quick recovery is still a
full Character Decision over now/later/silent and Expression Beats; the
Minimal Proposal format is only a lossless representation of the exact
single-immediate-text subset, never a host-imposed reply policy. The normal
successful path retains its 12-second budget and 1.2-second
acceptance/dispatch reserve. Only an observed invalid/exception/timeout or an
actually elapsed candidate deadline may open one bounded recovery attempt by
the same configured role provider; production never switches to a second
character-author provider. The proactive-contact route is stricter: the same
pinned role author may receive its one precise source/shape correction, but an
invalid corrected candidate is retried later through its durable 10/30/120-
minute lifecycle rather than immediately reauthored as a new intention. For
proactive contact, the role includes factual permission metadata in that same
structured Character Decision; production must not insert a second synchronous
claim-binding model between a valid `now` decision and independent truth
review. Local source-lane closure and the independently qualified truth
reviewer remain mandatory, but neither may author or replace the expression.
_Avoid_: Forced reply, failure fallback text, silence inference

The provider-visible request may include a compact hard-boundary manifest that
maps only visible source refs to valid factual claim scopes and states numeric
or cross-field schema constraints. It is an executable interface description,
not behavioral advice; proof-only Capsule refs stay hidden and the full
Capsule remains the Acceptance authority.

## User Request

The user's expressed preference for how the companion should speak or act. It is an input to deliberation, not an invariant the companion must obey.

## Character Decision

A model-owned choice about motive, stance, timing, expression, silence, or use of an available capability after considering the current World. It may be stochastic and surprising, but it cannot create authority or override a Hard Invariant.
_Avoid_: Behavior verdict, scripted reaction

## Controlled High Variance

The project principle that permits broad, context-sensitive variation in Character Decisions while keeping World truth, permissions, external effects, privacy, consent, safety, and replay deterministic.
_Avoid_: Rule-driven personality, unconstrained randomness

## Capability Boundary

A deterministic declaration of what the character can attempt and what evidence or authorization that attempt requires. It constrains executable effects without choosing whether the character wants to use them.
_Avoid_: Behavior policy, suggested action

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

## Producer-First Authority

Any new authority — an event type with its reducers and acceptance chain — must land in the same delivery as its first real producer and its first consumer. An authority that nothing produces must not merge: it is inventory that every schema migration and grammar-coverage assertion then has to carry. An already-merged authority whose producer never arrived must be explicitly marked dormant in `configs/mechanism_closure.yaml`, and activating it later requires a recorded producer verdict first.
_Avoid_: Speculative authority, dormant-by-default inventory

## Inner Advisory

A sourced, bounded, and non-authoritative signal about what may be influencing the companion, such as an Appraisal, Drive, Affect tendency, repair need, or candidate Stance. It may shape a Proposal but cannot write World truth or veto expression.
_Avoid_: Rule verdict, mandatory stance

## Context Capsule

A bounded, revision-pinned packet compiled from authoritative World Projections plus explicitly non-authoritative advisories for one Deliberation. It has a token budget and truncation log, and is not a second store of truth.
_Avoid_: Full-history prompt, free-form context dump

## Pinned Turn

One effect-once deliberation attempt whose Context Capsule, Inner Advisories,
Model Result and Proposal Audit all refer to the same complete ledger cursor.
If that cursor becomes stale before an authoritative write, the Pinned Turn is
discarded and rebuilt; it never grants a stale Proposal acceptance.
_Avoid_: Chat turn, mutable prompt session

## Text Turn Endpoint

A provider-local, advisory estimate of whether the same user is likely to add
another text bubble soon. It may combine the uncommitted batch, personal
bubble-gap history, typing presence, burst evidence, and recent message
lengths to size one bounded listening opportunity. It has no authority over a
character reply, interruption, silence, wording, or World mutation; new input
invalidates the older estimate.
_Avoid_: Turn-taking policy, punctuation rule, reply classifier

## Expression Unit Stream

One role-author provider response whose append-only `expression-events.1`
transport exposes a singular, complete, independently valid first Expression
Beat before any additional model-chosen Beats finish arriving. The head frame
carries the role-owned decision coordinates and exactly one visible Beat
(optionally preceded by one role-chosen typing Beat); later Beat frames and the
terminal frame remain part of the same provider result. The historical
`expression-units.1` envelope remains readable for replay but is not requested
from new production calls. Every frame still passes the normal source,
permission, Action, receipt and latest-cursor gates. A tail cannot win
independently or be regenerated after restart merely because its already
delivered head survived. Process health reports provider TTFT, first complete
frame, completed source closure, first fully validated candidate and
platform-visible ACK separately.
_Avoid_: Two-author provisional reply, token-by-token QQ output

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

## Source Review Qualification

An endpoint/model may enter a strict source-review lane only when its exact
schema digest has response evidence; sending a request or configuring an
OpenAI-compatible URL is not qualification. As of 2026-08-01, Inventory V5 is
release-qualified on both the OpenRouter `openai/gpt-5.4-nano` route (13/14
exact wires) and the direct `gpt-5.4-mini` route (11/12 exact wires; 10/10
semantic boundary cases). A qualified topology may therefore use them as one
serial availability role with 3-second and 8-second attempts and 600-second
route suppression. Coverage V5 remains dormant and unqualified. The active
non-exhaustive path is `inventory_v5_guard_then_full_source_review.7`:
Inventory contributes semantic decomposition only, and the independent RR.3 /
V7 authority receives those locators and still owns every factual verdict. If
Inventory is unavailable, the system falls back to full V7 and reports the
degraded route rather than treating a technical failure as a semantic result.
Production composition must prove that the character-author lane
has a different exact semantic authority for source review before startup.
An unqualified or self-reviewing ordinary route is a deployment error, not a
per-message fallback. Reviewer transport redundancy is availability protection
for a non-authoring truth boundary, never a backup character model. The old
role-reversal topology is no longer production-admissible; inability to prove
this boundary fails startup before provider clients are allocated.
_Avoid_: Configuration as evidence, timeout as schema success, Inventory as a
source verdict, dormant Coverage reported as active

## Private Impression

The companion's fallible, source-bound interpretation of a user, relationship, or event. The character authors its tentative `reflection_summary`; deterministic authority binds that reading to accepted appraisal sources, confidence, possible counter-evidence, and an expiry or settlement condition. It is never a User Fact, and legacy impressions without authored prose continue to resolve through their exact appraisal references.
_Avoid_: Hidden fact, inferred user fact

## Private Commitment

An internal decision to keep caring about, remember, revisit, or later act on something. It may open a Conversation Thread or produce an Action Proposal, but it is neither a completed Plan nor a Committed Experience.
_Avoid_: Completed intention, hidden experience

## Expression Beat

One independently dispatchable and settleable fragment in an ordered, interruptible expression. A Beat may depend on an earlier receipt and may be cancelled or reconsidered when the user interjects.
_Avoid_: Text chunk, random split
