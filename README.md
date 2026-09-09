# Radiowave Autonomous Retail

Experimental vendor-neutral autonomous retail R&D platform focused on radio-first physical tracking and multimodal sensor fusion.

## Core hypothesis

- 60 GHz mmWave radar provides anonymous shopper/body trajectories.
- RAIN UHF RFID provides unique merchandise identity and item-movement evidence.
- Selective computer vision provides semantic confirmation, ground truth, and exception evidence.
- The proprietary intelligence layer owns the digital store twin, normalized events, fusion, confidence, virtual cart, replay, calibration, and exception tooling.

## Development policy

- `main` is milestone/stable only.
- `dev` is the integration branch.
- All implementation happens on feature branches and enters `dev` through PR review.
- Hardware vendors must remain replaceable behind adapters.
- No autonomous charging until shadow-cart validation meets approved accuracy and confidence thresholds.
- Never commit customer video, production sensor captures, payment data, secrets, or biometric data.

## Status

Foundation bootstrap in progress.
