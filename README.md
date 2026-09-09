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

Foundation v0: hardware-independent core implemented on synthetic data.

- Typed contracts (`radiowave/contracts`), digital twin, normalization, deduplication
- Mock mmWave / RFID / vision adapters behind vendor-neutral native-sample contracts
- Explainable baseline fusion: canonical tracks, item state machine, ranked candidate ledger
- Confidence engine (COMMIT / WAIT / REVIEW), EPC-level idempotent cart
- JSONL and Parquet recorder/replay, deterministic scenario simulator, CLI
- 12 scenario tests plus unit and replay-determinism tests

Not in scope yet: real sensor SDKs, production CV, payment/settlement, autonomous charging.

## Quick start

```bash
uv venv .venv --python 3.12 && uv pip install -e ".[dev]"   # or: pip install -e ".[dev]"
pnpm run test
python -m radiowave.cli scenarios
python -m radiowave.cli simulate --scenario 05 --out data/synthetic/scenario-05.jsonl
python -m radiowave.cli replay data/synthetic/scenario-05.jsonl
```

See `docs/development/local-development.md` for the full command reference and
`docs/architecture/foundation-v0.md` for the module map and design notes.
