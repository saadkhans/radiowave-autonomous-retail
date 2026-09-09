# ADR 0001 — Radio-first, vendor-neutral architecture

Status: Accepted for Foundation v0

## Context
The project targets autonomous retail for apparel, footwear, accessories, electronics and other uniquely taggable merchandise. The central problem is reliable shopper-item attribution: who took which physical item, whether it was returned or handed off, and what exits the store.

## Decision
Use a radio-first multimodal architecture:
- mmWave radar for anonymous people/body trajectories;
- RAIN UHF RFID for unique merchandise identity and movement/location evidence;
- selective CV for semantic confirmation, ground truth and exceptions;
- a proprietary normalized fusion/cart/confidence layer above replaceable hardware adapters.

## Consequences
- Foundation development begins with mock adapters and replayable synthetic data.
- Hardware-specific SDKs cannot leak into business-level contracts.
- All observations are normalized into a common store coordinate/time model.
- We will benchmark multiple radar and RFID vendors before production selection.
- Complex/vendor-specific RF localization is deferred until simple zone/trajectory evidence is measured.
- IP/freedom-to-operate review remains a parallel workstream before adopting patented localization/topology patterns.

## Revisit triggers
Revisit this ADR if measured pilot evidence shows another architecture materially outperforms the radio-first approach on exact-basket accuracy, TCO, maintenance, privacy, or scalability.
