# Radiowave Autonomous Retail Roadmap

Updated: 2026-09-17

This roadmap is authoritative for current phase ordering. Older architecture notes remain useful research history but do not override this plan.

## Current direction

Radiowave is now developed as a **radio-first, event-first autonomous retail platform**:

```text
60 GHz mmWave radar = anonymous shopper trajectory
UHF RAIN RFID       = unique item identity + movement/location evidence
trajectory fusion   = WHO TOOK WHAT
selective CV         = semantic/action evidence, ground truth, ambiguity resolution
confidence engine   = COMMIT / WAIT / REVIEW
cart engine         = shopper virtual cart
```

The durable proprietary layer is the digital twin, normalized contracts, replay/recording, fusion, confidence, cart logic, calibration, datasets and operational tooling. Sensor vendors remain replaceable.

## Strategic change

Hardware is no longer on the software critical path.

We build the Virtual Store Lab in parallel with sensor procurement. Before hardware arrives, simulator parameters are explicit assumptions. After hardware arrives, real captures and measurements calibrate those distributions and the same deterministic scenario suite is rerun.

## Phases

### Phase 0 — Product Definition, IP, Regulatory, Test Doctrine
Ongoing foundation.

### Phase 1 — Foundation v0
**Complete / merged.** Canonical contracts, digital twin, deterministic simulator, ingestion, fusion, confidence, cart, recording/replay and CI.

### Phase 2 — Radiowave Observatory v0
**Complete / merged.** Browser engineering console for tracks, events, evidence, carts and deterministic replay.

### Phase 3 — TI IWR6843 Live People Tracking
**Software substantially complete; final software review/hardware acceptance pending.**

TI UART -> defensive parser -> PersonObservation -> canonical PersonTrack -> live Observatory.

### Phase 4 — Virtual Store Lab v0
**Next / in progress.**

Build a realistic mini-store entirely in software using production sensor boundaries. First gate: 3 shoppers + 30 unique items + 3 racks + deterministic 60-second live simulation through production fusion/confidence/cart and Observatory.

See `docs/phases/phase-4-virtual-store-lab-v0.md` for the phase contract, and `docs/simulator/virtual-store-lab.md` for usage and architecture.

### Phase 5 — RFID Native Boundary + Real Reader Integration
Build native RFID simulation first, then integrate Impinj R700 / Times-7 hardware and calibrate real item-location behavior.

### Phase 6 — Person + Item Fusion Hardening
Scale multi-shopper/multi-item attribution, handoffs, co-walking and ambiguity handling across simulation and real lab data.

### Phase 7 — Selective Computer Vision Evidence
Add replaceable CV only where semantic/action confirmation, ground truth, audit or ambiguity resolution materially improves results.

### Phase 8 — Instrumented Shadow Mini-Store
2–3 racks, 30–100 tagged items, 1–3 shoppers, real mmWave + real RFID + optional GT cameras. Shadow cart only; compare inferred basket to ground truth.

### Phase 9 — Confidence-Based Controlled Just-Walk-Out Pilot
Limited users/SKUs/store area, confidence-gated auto-commit and review workflow. No broad rollout until measured accuracy supports it.

### Phase 10 — Production Platform / Chain Rollout
Multi-store operations, integrations, deployment/calibration automation, model/firmware rollout and commercial support only after accuracy and economics are proven.

## Parallel development model

```text
SOFTWARE / SIMULATION                  HARDWARE / MEASUREMENT

Foundation + Observatory               procure TI / RFID
        |                                      |
Virtual Store Lab                      characterize real sensors
        |                                      |
native sensor emulators                capture empirical distributions
        |                                      |
fault injection + scenarios <---------- calibrate simulator
        |
production fusion improvements
```

## Simulator doctrine

The simulator must exercise production boundaries rather than bypass them. For example:

```text
virtual person
-> TI target
-> TI UART/TLV bytes
-> MemoryByteStream
-> production TiFrameParser
-> production TiTargetNormalizer
-> PersonObservation
-> FoundationPipeline
```

Later hardware changes only the transport source, e.g. `MemoryByteStream -> SerialByteStream`.

## Merge discipline

- `main`: stable milestones only
- `dev`: integration
- feature branches: implementation
- no direct implementation on `main`/`dev`
- feature -> `dev` through PR
- never auto-merge without explicit human approval
- stacked PRs are acceptable when a later phase depends on an open prior phase
