# ADR 0003: Separate facial display from photographic authenticity

Status: accepted

## Context

MediaPlan v5 separated social purpose, whole-image address, camera authorship and camera geometry, but two failure modes remained coupled to loose prompt language.

First, a facial “expression family” plus mouth/eyes/brows often collapsed into the identity reference's small smile and head tilt. It could not reliably distinguish deliberate cuteness, a leaking laugh, mock defiance, embarrassment, direct desire, or withheld attention. Public expression adapters and prompt recipes show useful execution primitives, while facial-action research offers an observable vocabulary; neither supplies a safe software-grade claim that a still image reveals a hidden emotion.

Second, “real phone photo” was treated as one adjective. Ordinary life-share images could become polished product renders, while adding grain or desaturation indiscriminately would merely create another repeated style. Device rendering, exposure compromise, processing, scene orderliness, environmental entropy, aesthetic intent, and regional evidence are separate decisions.

## Decision

New v5 plans add two deep, replayable contracts without changing the public planner or renderer interface:

1. `Facial Display Strategy` freezes the recipient-facing social family and intended effect.
2. `Facial Micro-Performance` freezes coherent visible actions in one selected still-frame beat.
3. `Photographic Authenticity Profile` freezes whole-image phone-rendering behavior and the boundary against unsupported regional claims.

The image machine builds complete compatible candidates and applies stable weighted variation. The planning model still selects one candidate ID and cannot independently assemble facial axes or authenticity tags. Facts, privacy, capture physics and non-explicit boundaries remain hard constraints; social affinities, facial realization and photographic style remain weighted choices.

FACS-like action language is an internal observable vocabulary only. It is not exposed as planner-facing action-unit codes, used to diagnose emotion, or treated as proof of a temporal microexpression. Identity references define identity and coarse geometry only.

## Consequences

- Cute, comic, proud, vulnerable and intimate performances can vary without becoming a bag of prompt tags.
- Nose/cheek action and facial asymmetry become first-class because visual comparison showed they materially separate a leaking laugh from a generic smile.
- Attraction can be recipient-directed through gaze, proximity and an unfinished beat without increasing exposure.
- A believable personal photo may be documentary, pleasant, atmospheric or deliberately arranged; authenticity does not mandate defects.
- Inspection can distinguish a generic-smile fallback, copied reference expression, commercial-render dilution and unsupported regional styling.
- Existing v1-v4 and pre-extension v5 payloads remain recoverable without synthesizing new contracts.
