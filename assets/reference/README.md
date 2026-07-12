# Celia Reference Set

This directory stores the first visual reference set for 沈知栀 / Celia Shen.

- `celia-reference-01-portrait.png`: primary face and hairpin anchor.
- `celia-reference-02-campus.png`: half-body campus outfit anchor.
- `celia-reference-03-desk-selfie.png`: private-chat selfie framing anchor.
- `celia-reference-04-cafe-profile.png`: side/profile and quiet-life mood anchor.

These images are not a trained LoRA. They are the current visual baseline for
prompting, manual selection, and future IP-Adapter/FaceID/LoRA work.

## Celia v2 identity set

- `celia-v2-reference-01-canonical.png`: approved canonical face, teal hair clip, and asymmetric beauty-mark anchors.
- `celia-v2-reference-02-no-hairclip.png`: approved single-variable identity check with the hair clip removed.
- `celia-v2-reference-03-angle-10deg.png`: approved first angle-progression image, with a mild 8–10° turn.

The v2 set is the authoritative identity direction. The older
`celia-reference-*` images are visual-development references only and should
not be mixed into v2 identity training without manual review.

### User-supplied v2 candidates

`celia-v2-candidates/` contains sources that are useful for later curation,
but are not automatically equal-weight identity-training data:

- `01-selfie-peace.jpg` and `02-casual-selfie.jpg`: close selfie candidates
  with useful expression and framing variation; manually verify face anchors
  before adding either to the final training set.
- `05-office-fullbody.jpg` and `06-casual-fullbody.jpg`: full-body candidates
  for pose, wardrobe, and silhouette diversity. Keep their proportion low in
  a face-identity LoRA because the face occupies too few pixels.

The supplied intimate bedroom images are deliberately not copied into this
project reference set: they conflict with the current everyday visual-identity
constraints and would bias a small training set toward that context.

### Relationship-only visual assets

`celia-v2-relationship-private/` contains two user-approved, adult intimate
images retained for a future opt-in relationship/ambiguity mode. They are not
part of the everyday visual identity, default selfie flow, or base LoRA
training set. Any future use must remain an explicit, adult, consent-aware
relationship-state decision rather than a response to a generic image request.

If the repository is ever public or shared beyond the intended operators,
move this directory to an ignored private-asset location before publishing.

### Everyday hairstyle variants

`celia-v2-hairstyle-variants/` contains user-approved, everyday loose-bun
variants. They represent the canonical shoulder-length hair gathered into a
messy low bun with loose face-framing strands, not a second long-hair identity:

- `01-study-low-bun.png`: close identity and hairstyle candidate.
- `02-campus-low-bun-fullbody.png`: full-body wardrobe and silhouette
  candidate; use at low proportion in face-identity training.
