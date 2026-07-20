# Hosted media provider options — 2026-07-16

Scope: hosted (not local) options for the event-driven image machine.  This note deliberately distinguishes **published product capability** from **permission to generate a particular class of content**.  It is not a guide to bypassing a provider's safety controls.

## Findings

### Black Forest Labs / FLUX

- The FLUX API documents `flux-2-pro` and `flux-2-max`, plus the FLUX Kontext editing models.  FLUX.2 documents multi-reference image editing with up to ten source images; this is relevant to the project's identity-reference selection rather than a reason to pass every reference on every request. [FLUX.2 overview](https://docs.bfl.ai/flux_2/flux2_overview)
- Published list pricing is per generated megapixel: FLUX.2 Pro generation starts at US$0.03, FLUX.2 Max at US$0.07, Kontext Pro at US$0.04 and Kontext Max at US$0.08.  Exact cost therefore depends on the requested output dimensions. [BFL pricing](https://docs.bfl.ai/quick_start/pricing)
- The older FLUX 1.1 Pro and Ultra Raw documentation exposes a `safety_tolerance` request parameter; Ultra Raw also documents image-prompt strength and a `raw` option intended for a less polished photographic result. [FLUX 1.1 Pro](https://docs.bfl.ai/flux_models/flux_1_1_pro) · [FLUX 1.1 Ultra Raw](https://docs.bfl.ai/flux_models/flux_1_1_pro_ultra_raw)
- BFL generation is asynchronous.  Result URLs expire after ten minutes and the integration guide requires the client to persist returned assets and implement rate-limit backoff. [generation API](https://docs.bfl.ai/quick_start/generating_images) · [integration guidance](https://docs.bfl.ai/api_integration/integration_guidelines)

Interpretation: FLUX is a worthwhile **controlled benchmark** for the project because its reference/editing and cost profile fit the existing renderer.  The documented tolerance parameter is not evidence that any particular sexual or adult-oriented output is permitted.  Before production use, the account owner must verify the then-current terms and test only content that those terms permit.

### Stability AI

- Stability's hosted image services state that they are heavily safeguarded and warn that attempts to circumvent the terms can lead to denial of access. [Stable Image guide](https://platform.stability.ai/docs/getting-started/stable-image)
- Its terms prohibit obscene, lewd, pornographic or prurient content and specifically enumerate explicit sexual activity, visible genitalia, bare breasts, fully nude buttocks and fetishistic content. [Stability terms](https://platform.stability.ai/docs/terms-of-service)

Interpretation: not a suitable primary provider for a lane whose product requirement is deliberately high-intensity adult sexual suggestion, even if the planned output remains non-explicit.

### OpenAI image generation

- The OpenAI API exposes moderation controls for image generation and its moderation taxonomy includes sexual material intended to arouse sexual excitement. [image-generation parameter reference](https://platform.openai.com/docs/api-reference/responses-streaming/response/refusal/delta?lang=curl) · [moderation reference](https://platform.openai.com/docs/api-reference/moderations?api-mode=responses&lang=curl)

Interpretation: OpenAI remains a useful quality baseline for ordinary media, but should not be the architectural dependency for a product lane that expects adult-suggestive requests to be reliably accepted.

### MiniMax as a planner candidate

- MiniMax documents OpenAI-compatible and Anthropic-compatible text interfaces, with current M2.7/M2.5 text models; this makes it a low-friction replacement for the present DeepSeek planner adapter. [API overview](https://platform.minimaxi.com/docs/api-reference/api-overview)
- Published pay-as-you-go text pricing for M2.7 and M2.5 is ¥2.1 / million input tokens and ¥8.4 / million output tokens.  Its `image-01` is published at ¥0.025 per image, but that price is not evidence of adequate identity consistency or of permission for the project's intimate-media lane. [MiniMax pricing](https://platform.minimaxi.com/docs/guides/pricing-paygo)
- MiniMax's Open Platform agreement prohibits the dissemination of obscene or pornographic content.  Its text API documentation also exposes `input_sensitive_type` and `output_sensitive_type`, with `色情` as a distinct category that can cause a content-violation error. [Open Platform user agreement](https://platform.minimaxi.com/protocol/user-agreement) · [text API reference](https://platform.minimaxi.com/docs/api-reference/text-post)

Interpretation: MiniMax is **not** a fit for the intimate-media planner or renderer if the lane's product requirement expects sexualised prompts or outputs to work reliably.  At most, it could be used for ordinary, non-intimate planning, but splitting the planner by content lane creates avoidable operational complexity; it is not the recommended default here.

## Proposed evaluation, not a production policy

1. Keep the current event snapshot, candidate matrix, reference selection and inspection contracts provider-neutral.
2. Add a `ProviderCapabilityProfile` that freezes provider/model/version, reference-image limits, supported routes, output-cost basis, retry policy and the account-confirmed content ceiling.
3. Run a small fixed benchmark of frozen, policy-compliant adult-fictional/non-explicit plans: identity consistency, geometry compliance, human-phone authenticity, rejection rate and effective cost per accepted image.
4. Do not silently fall back across providers on a frozen plan.  A provider choice is part of the recorded rendering contract; explicit retry is allowed only under its configured policy.
5. Do not automatically deliver a newly introduced high-intensity lane until its visual inspection and failure behaviour have been manually evaluated.
