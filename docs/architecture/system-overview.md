# System Overview

## Architectural position

Radiowave Autonomous Retail is a vendor-neutral, radio-first, multimodal retail intelligence platform.

### Primary physical signals
- 60 GHz mmWave radar: anonymous people/body trajectories.
- RAIN UHF RFID: unique item identity and item movement/location evidence.
- Selective computer vision: semantic confirmation, ground truth, and exception evidence.

### Proprietary intelligence layer
- Digital store twin
- Common world-coordinate/time model
- Normalized event contracts
- Shopper/item trajectory association
- Sensor fusion
- Confidence decisions
- Virtual cart
- Replay/ground-truth datasets
- Calibration and exception tooling

## High-level flow

```text
entry/session
   |
people tracker ----+
                   |
item tracker ------+--> normalized world/time model --> fusion --> retail events --> confidence --> cart
                   |
vision evidence ---+
```

## Non-goals for Foundation v0
- Production sensor SDK integrations
- Production computer-vision models
- Payment settlement
- Autonomous charging
- Vendor-specific localization algorithms
- Store-wide production deployment

## Foundation v0 success criteria
1. Stable typed contracts.
2. Deterministic synthetic observations.
3. Replayable scenarios.
4. Hardware-independent adapters.
5. Explainable baseline fusion.
6. Automated tests for pick, putback, misplace, handoff, and exit ownership.
