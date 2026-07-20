# ADR 0006: Suggest and verify media lanes

Status: accepted

## Context

`expression_charge` describes an image's visual charge, but it did not answer a more important social question: is this ordinary life sharing with a little intentional femininity, or a photo deliberately shown only to one recipient? Treating every non-neutral charge as private made normal life photos too easy to label as intimate, while a true recipient-exclusive moment could visually dilute into a routine selfie.

The World owns event selection, privacy ceilings, relationship facts and delivery authority. The image machine owns photographic expression, but must never infer exclusive access from a flattering prompt.

## Decision

New character-media v5 planning returns one bounded `MediaLaneRecommendation` in the existing planning model call:

- `ordinary_life`
- `alluring_life`
- `exclusive_private`
- `explicit_reserved`

It also returns one `recipient_access` and one `attraction_expression`. `MediaEligibilityRouter` remains the deep verification module: after the model selects a complete candidate, it validates the proposed Lane against the selected expression charge, capture author, address strategy, sharing intent, privacy ceiling and frozen World evidence.

`alluring_life` permits event-grounded, recipient-directed photos with visible feminine or hormonal expression, but cannot claim recipient-exclusive access. `exclusive_private` reuses the existing private-expression basis and additionally requires a self-authored front-camera or mirror capture. `explicit_reserved` always returns `NotRenderable`; it is a vocabulary and persistence reservation, not a generation path.

The same existing v5 inspection call receives the frozen Lane contract. It rejects an alluring photo that has become neutral documentation, and an exclusive photo that has become a generic broad share.

## Consequences

- One LLM call still selects the plan; no secondary classifier, autonomous send path or prompt-only private bypass is introduced.
- The model may recommend a Lane, but cannot grant itself private access or future explicit capability.
- New plans persist the Lane recommendation and include it in visual diversity; pre-extension v5 payloads omit it and retain their old replay semantics.
- World integration only needs to continue freezing the existing privacy ceiling, expression-charge ceiling, recipient and private-expression basis when it wants exclusive-private eligibility.
