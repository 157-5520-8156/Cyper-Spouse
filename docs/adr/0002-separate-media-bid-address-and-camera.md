# ADR 0002: Separate media bid, recipient address, and camera geometry

Status: accepted

## Context

Earlier event media plans mixed three decisions in free strings and character pose bundles: why a photo was shared, how the whole image addressed its recipient, and where the camera physically was. Different intents therefore rendered as the same generic portrait, while one intent could inherit a fixed selfie angle, smile, or bedroom reference pose.

`capture_mode` also became overloaded. It identifies the camera operator, but cannot describe distance, height, crop, orientation, occupancy, placement, or imperfection. Treating “front camera” as one composition produced repeated arm-length, slightly high, portrait-format selfies.

## Decision

MediaPlan v5 freezes separate contracts:

1. `MediaInteractionBid`: the hoped-for response.
2. `MediaAddressStrategy`: how the complete image faces the recipient.
3. `CameraGeometry`: where the camera is and how the frame is occupied.

Character media additionally freezes Subject Presentation v3 and Embodied Presentation v3. Pose Performance owns body geometry and hand responsibility; Facial Performance owns expression and gaze. The planner receives at most 24 already-compatible `CompleteMediaExpressionCandidate` values and selects one ID.

Identity reference assets are selected and frozen by planned view axis and distance. Expression charge cannot select a bedroom or bold pose reference. References remain identity/geometry aids and never override the shot-local contract.

## Consequences

- One interaction bid can have several credible camera and expression realizations.
- One capture author can produce varied geometry without violating device physics.
- `life_share` may carry grounded intimate address without inventing a visible character or private scene.
- Inspection can reject generic-portrait dilution, authorless editorial imagery, camera mismatch, and copied reference nuisance pose as distinct defects.
- v1-v4 retain their original payload, prompt, reference, inspection, and repair semantics; v5 is feature-gated.
