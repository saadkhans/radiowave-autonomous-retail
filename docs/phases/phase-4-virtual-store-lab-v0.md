# Phase 4 — Virtual Store Lab v0

## Purpose

Build a deterministic autonomous-retail store simulator that exercises the same production boundaries used by real hardware so software development can continue before TI/RFID hardware arrives.

Hardware delivery is **not** a prerequisite for this phase. Real hardware measurements will later calibrate the simulator's noise, dropout, latency and identity-stability distributions.

## First acceptance gate

> Three virtual shoppers + 30 uniquely identified items + three racks complete a deterministic 60-second store simulation through the live Observatory, production fusion, confidence and cart engines, with no physical hardware.

## Architecture

```text
virtual shopper / item physics
        |
        +--> virtual TI target
        |       -> TI UART/TLV bytes
        |       -> MemoryByteStream
        |       -> real TiFrameParser
        |       -> real TiTargetNormalizer
        |       -> PersonObservation
        |
        +--> virtual RFID reader/antennas
                -> native reader-style observations
                -> RFID adapter boundary
                -> ItemObservation

PersonObservation + ItemObservation
        -> existing FoundationPipeline
        -> existing fusion
        -> confidence COMMIT / WAIT / REVIEW
        -> cart engine
        -> Observatory
        -> recording / replay / metrics
```

The simulator must not bypass production adapters when a native protocol boundary already exists.

## Store model

Initial mini-store:

- 3 merchandise racks / fixture groups
- 30–50 uniquely identified EPC items
- 1 entrance
- 1 exit
- 1 virtual TI mmWave radar for the first slice
- virtual RFID reader with multiple logical antenna zones
- 1–3 simultaneous shoppers
- 30–120 second deterministic scenarios

The digital twin remains authoritative for fixtures, zones, item homes and sensor poses.

## Shopper simulation

Support deterministic shopper actors with:

- entry / exit
- path following
- velocity / heading
- dwell near fixtures
- reach / pick interaction
- carry
- putback
- misplace
- handoff
- exit with item
- exit and re-entry

Shopper ground truth is simulator-only and must remain separate from canonical inferred tracks.

## Item simulation

Each simulated item has:

- unique EPC identity
- GTIN/SKU mapping
- home fixture / zone
- world position
- movement state
- carrier ground truth when applicable

The simulator must preserve EPC-vs-SKU separation.

## TI native radar emulator

The first radar simulator should produce the native input consumed by the Phase-3 TI boundary:

```text
virtual person
-> virtual TI target
-> binary TI packet/TLV stream
-> MemoryByteStream
-> production TI parser
-> production TI normalizer
```

Support deterministic injection of:

- position/velocity noise
- frame jitter
- fragmentation across reads
- multiple frames per read
- malformed/corrupt packet cases for dedicated fault scenarios
- target dropout
- false target
- native track ID switch/reuse
- radar restart/reconnect generation

Do not duplicate TI parsing logic in the simulator.

## RFID simulator

Phase 4 establishes the simulation side of the item-sensor boundary. It should model:

- EPC reads
- antenna/source identity
- RSSI-like evidence
- phase-like evidence where useful to future adapters
- read rate / burstiness
- missed reads
- duplicate reads
- antenna bleed
- temporary dropout
- reader restart

If the final canonical RFID adapter boundary is not yet present, introduce the smallest vendor-neutral boundary needed for simulation while keeping Phase 5 responsible for real Impinj hardware integration.

## Fault injection

All faults must be seed-controlled and reproducible.

Required classes:

### Radar
- measurement noise
- jitter
- dropout
- false target
- native ID switch/reuse
- packet fragmentation/corruption
- sensor restart

### RFID
- missed reads
- duplicate reads
- RSSI/phase noise
- antenna bleed
- delayed/burst reads
- reader restart

### System
- latency
- out-of-order delivery
- duplicate delivery
- bounded-queue pressure
- clock offset where safe to simulate

## Scenario suite

Initial scenarios:

1. normal purchase
2. putback to same fixture
3. misplace to wrong fixture
4. handoff between shoppers
5. same SKU, different EPC items
6. crowded rack
7. crossing shoppers
8. co-walking ambiguity
9. radar dropout
10. RFID dropout
11. radar native-ID recycle
12. radar restart
13. RFID reader restart
14. fast pickup
15. multiple-item basket
16. exit and re-entry
17. ambiguous pickup -> WAIT/REVIEW
18. optional evidence resolves ambiguity

Every scenario must be deterministic for a given seed.

## Observatory

Add a simulation mode without creating a second map renderer.

Minimum controls:

- simulation scenario
- seed
- shopper count where scenario allows
- item count where scenario allows
- radar noise preset
- RFID reliability preset
- start / stop / reset

The same person/item/cart/event panels should render simulator and hardware-backed runs.

## Metrics

Capture at least:

- ground-truth PICK / PUTBACK / MISPLACE / HANDOFF / EXIT events
- inferred event precision / recall
- attribution accuracy
- COMMIT / WAIT / REVIEW rates
- time-to-commit
- track continuity / ID switches
- observation drop counts
- cart correctness at exit

Do not present simulator metrics as measured real-world accuracy.

## Calibration doctrine

Before hardware:

- all error/noise parameters are explicit assumptions.

After hardware arrives:

1. record real raw and normalized captures
2. measure empirical noise/dropout/latency/ID behavior
3. fit simulator parameter distributions
4. rerun deterministic scenario matrix
5. compare SIM vs REAL
6. use mismatch as an engineering signal

## Non-goals

This phase does **not** implement:

- real Impinj/Zebra readers
- multi-radar production fusion
- custom mmWave DSP/raw ADC
- DCA1000
- production camera CV
- payment/settlement
- cloud brokers/databases
- customer mobile app
- autonomous charging

## Quality gates

Before merge:

- deterministic repeatability for identical seed/config
- production TI parser exercised by native radar emulator
- no simulator-only shortcut into fusion for radar
- item identity remains EPC-first / SKU separate
- replay reproduces a completed simulated run
- existing Foundation, Observatory and Phase-3 tests remain green
- `pnpm run lint`
- `pnpm run typecheck`
- `pnpm run test`
- `pnpm run build`
- `pnpm run security:secrets`

## Branch / PR strategy

This phase is developed as a stacked PR on top of Phase 3 while PR #3 remains open. Once PR #3 merges into `dev`, rebase/retarget this phase to `dev` without mixing Phase-3 fixes into the simulator review.
