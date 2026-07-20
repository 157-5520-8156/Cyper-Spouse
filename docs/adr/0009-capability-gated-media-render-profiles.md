# ADR 0009: Freeze capability-gated media render profiles

Status: accepted

## Context

The image machine needs two intentionally different rendering routes. Ordinary life media is currently most reliable with GPT Image 2 and selected identity references. The future `suggestive_private` lane needs a Krea 2 RAW character LoRA, with a cloud orchestration route capable of loading that exact asset.

It would be tempting to treat a model family name, a Civitai account, or a LoRA file as interchangeable proof that any Civitai Krea request can use it. That is false for the documented standard Krea v2 recipe: it accepts style references but not a LoRA field. A silent fallback from a high-lane plan to GPT Image 2 or an unrelated SDXL checkpoint would also break the frozen recipient-exclusive contract.

## Decision

Every externally generated plan will name a versioned Media Render Profile before submission. The profile freezes the model ecosystem, route, Identity Binding, supported controls, budget policy and capability status.

The initial intended profiles are:

- `ordinary_openai_image2`: GPT Image 2 with `reference_edit`; eligible for ordinary and non-adult character media.
- `krea2_raw_custom_comfy_candidate`: Krea 2 RAW plus a compatible LoRA through a verified Custom Comfy workflow; initially experimental and ineligible until its actual model files and graph pass preflight.
- `civitai_standard_krea2_reference_candidate`: standard Krea v2 orchestration with `style_reference_unverified`; it is never eligible for identity-critical automatic high-lane delivery because that documented route cannot load a LoRA.

The planner never chooses a provider. The Media Renderer accepts one frozen profile and persists one External Generation receipt. A failing profile may only use a fallback explicitly authorized in the frozen plan; there is no implicit cross-model or cross-lane downgrade. Until the Krea2 custom workflow is verified, no `adult_suggestive` generator is registered.

## Consequences

- Krea2 RAW LoRA remains in the same intended model ecosystem; no Flux path is required or assumed.
- The existing generic Civitai SDXL adapter becomes opt-in legacy code, rather than becoming an accidental high-lane default whenever a Civitai key exists.
- Model delivery is not sufficient for activation. The workflow must prove asset loading, provider eligibility, cost, error behavior and visual identity across the acceptance matrix.
- More providers can be added without exposing provider workflow formats to `MediaPlanner` or World v2.
