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

## Radiowave Observatory

The Observatory is an engineering console for watching Foundation v0 work: the
store twin, shopper tracks, EPC states, candidate rankings, COMMIT / WAIT / REVIEW
decisions, retail events and virtual carts, replayed deterministically on
simulated time. It is a debug tool, not a checkout, POS, payment or admin UI.

### Requirements

- Python 3.12 with the `api` extra (FastAPI + uvicorn) and the `dev` extra (httpx for tests)
- Node 22+ and pnpm 10+ (the frontend lives in the `apps/observatory` workspace package)

### Install

```bash
uv venv .venv --python 3.12 && uv pip install -e ".[dev,api]"   # or: pip install -e ".[dev,api]"
pnpm install
```

### Launch

```bash
pnpm run dev            # API on http://127.0.0.1:8765 + Vite on http://localhost:5173
```

`pnpm run dev:api` and `pnpm run dev:observatory` start the two halves separately.
Vite proxies `/api/*` to the Python API, so open http://localhost:5173 (if 5173 is
busy Vite prints the port it picked). The API is in-process and stateless across
restarts: no database, broker or auth.

### Using it

1. Pick a scenario in the left panel. Its description, duration, shopper and item
   counts and the expected ground-truth events are shown before anything runs.
2. Press **Run scenario**. A run is created at simulated t = 0 s.
3. Drive time with **Play / Pause / Step / Reset**, choose a speed from 0.25x to
   10x, or drag the timeline. The browser never advances the clock itself: every
   tick asks the API to advance simulated time, and seeking backwards replays
   from t = 0 so the state at any time is reproducible.
4. Click a shopper, item, cart line or event row to open it in the inspector.
   Items show the ranked candidate table (distance, trend, velocity, temporal,
   co-motion, zone, vision; `—` when a feature is not available) and the current
   COMMIT / WAIT / REVIEW decision with confidence, margin, pending duration and
   reason. Scenario 12 stays in WAIT and escalates to REVIEW; 12v resolves to
   COMMIT with vision evidence.
5. Layer toggles switch zones, fixtures, sensors, grid, trajectories, labels,
   candidate lines and confidence labels on and off.

### Architecture

- `radiowave/api/` — thin FastAPI layer over the existing pipeline: `runs.py`
  (in-memory `RunManager`, `ObservatoryRun` advance / step / seek / reset),
  `viewmodels.py` (explicit Pydantic view models), `routes/`.
- `apps/observatory/` — React + TypeScript + Vite + Tailwind. `src/types/api.ts`
  mirrors the view models, `src/lib/geometry.ts` projects world metres to SVG
  pixels from the twin's floor bounds, `src/state/store.tsx` owns run state and
  the playback loop, `src/components/` renders the map, inspector, carts, event
  stream and replay controls.
- Identities on screen are canonical (person track id, session id, EPC, GTIN);
  sensor ids appear only as provenance. See `docs/architecture/observatory-v0.md`.
