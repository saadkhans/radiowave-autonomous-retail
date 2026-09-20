# Virtual Store Lab v0

The Virtual Store Lab is a deterministic, standalone simulator of an autonomous retail store. It exercises the same production sensor boundaries and Foundation pipeline code used by real hardware, allowing software development to continue before TI mmWave and RFID readers arrive.

## Purpose

Hardware delivery is not a prerequisite for algorithmic development. The lab produces synthetic observations that flow through the exact same parsing, normalization, fusion and confidence layers that real hardware will use. Before hardware arrives, every simulator parameter is an explicit assumption; after hardware arrives, real captures calibrate those distributions and the same deterministic scenario suite is rerun to compare simulated versus real behavior.

## Architecture

```text
Virtual world (positions, velocities, scripted interactions)
        │
        ├─> virtual TI mmWave radar
        │       └─> TI UART/TLV bytes
        │           └─> MemoryByteStream
        │               └─> real TiFrameParser
        │                   └─> real TiTargetNormalizer
        │                       └─> PersonObservation
        │
        └─> virtual RFID reader
                └─> NativeRfidRead (native adapter sample)
                    └─> real ObservationNormalizer
                        └─> ItemObservation

PersonObservation + ItemObservation
        │
        └─> FoundationPipeline
            │
            ├─> BaselineFusionEngine
            ├─> ThresholdConfidenceEngine (COMMIT / WAIT / REVIEW)
            └─> InMemoryCartEngine
                │
                └─> Observatory (visualization + replay)
```

The simulator produces **INPUT**. Production code produces **INTERPRETATION**. Nothing constructs `PersonObservation`, `ItemObservation` or `TiFrame` directly in the simulator; those flow from the real parsers and adapters that will parse real hardware bytes.

## Boundary rule

The simulator's sensor emulators emit bytes or native samples (like `NativeRfidRead`) exactly as a physical sensor would:

- **TI mmWave**: `TiRadarEmulator` generates TI UART/TLV bytes which are fed to the real `TiFrameParser`, then the real `TiTargetNormalizer`. The simulator never constructs `PersonObservation` directly.
- **RFID**: `RfidReaderEmulator` generates `NativeRfidRead` samples (the vendor-neutral contract), which flow through the real `ObservationNormalizer.item()` method. The simulator never constructs `ItemObservation` directly.

Both `PersonObservation` and `ItemObservation` are inferred from sensor data by production code. If they are constructed anywhere else, the boundary is broken.

## Ground truth isolation

Simulator-only ground truth (shopper identities, item positions, interactions) is captured in `GroundTruthLog` and never read by the pipeline. Fusion must infer all shopper identity and item movement from sensor observations alone.

The only sanctioned exposure of ground truth is in the Observatory timeline overlay — a display-only feature that had no influence on what the fusion engine concluded. Ground truth is purely for evaluation: comparing "what fusion inferred" against "what actually happened."

## Determinism and seed semantics

**Determinism is guaranteed by the `LabEngine`.**

The synchronous, single-threaded `LabEngine` is the reproducibility oracle for Phase 4:

- Every simulation tick is driven by discrete simulation time (`tick_hz` rate), not wall-clock time.
- Every observation is stamped with **simulated time** (`received_at`), never the host's current timestamp.
- The only RNG seeding happens at initialization; the seed drives observation noise, not truth.
- Two `LabEngine` runs with identical scenario, seed, and step sequence produce byte-identical results on any machine.

The **Observatory SIM path** (polling the engine from a browser) is faithful but NOT the determinism guarantee:

- The browser polls on wall-clock intervals, which may vary or slip.
- Results are identical to a sequential run in terms of fusion logic (the algorithm doesn't change), but wall-clock uncertainty means timing may drift.
- SIM via the browser satisfies the "through the live Observatory" acceptance gate; determinism is proven via `LabEngine` directly.

**Seed semantics:** The seed varies observation (noise, faults), not truth. Scripted shopper paths are deterministic unless jitter is explicitly applied via interaction scheduling. This matters for attribution: a metric change when noise changes but truth stays constant is attributed to the algorithm, not simulation randomness.

## Ground-truth shoppers vs. inferred tracks

Each scenario defines ground-truth shopper actors (e.g., `GT-PERSON-001`) with scripted paths and interactions. These IDs are **never** the canonical shopper identity. The Foundation pipeline infers shopper identity (`PersonTrack.track_id`, e.g., `P0001`) independently from sensor data. The ground-truth log exists for evaluation only — it is never read by fusion.

## How to run it

### Python API

```python
from radiowave.simulator.lab.engine import run_lab_scenario
from radiowave.simulator.lab.scenarios import load_scenario

result = run_lab_scenario(load_scenario("01_normal_purchase"))

print(f"frames parsed:      {result.frames_parsed} ({result.frames_rejected} rejected)")
print(f"person observations {result.person_observations}")
print(f"item observations   {result.item_observations}")
print(f"proposed events     {len(result.pipeline.proposed_events)}")
print(f"committed events    {len(result.pipeline.committed_events)}")
print(f"person tracks       {[t.track_id for t in result.pipeline.person_tracks]}")
```

Available scenarios: `01_normal_purchase`, `02_putback`, `03_misplace`, `04_handoff`, `05_two_shoppers_cross`, `06_same_sku_distinct_epcs`, `07_radar_dropout`, `08_rfid_dropout`, `09_native_id_reuse`, `10_crowded_rack_ambiguity`, `11_multiple_items`, `12_exit_with_item`, `13_sensor_restart`, `acceptance_60s`.

### HTTP API (Observatory)

Start the development server:

```sh
pnpm dev
```

Then access `http://localhost:5173` and select **Simulate** to browse scenarios. Alternatively, use the HTTP API directly:

**List all scenarios:**
```
GET /api/sim/scenarios
```

Response: array of scenario summaries with id, name, description, duration_s, shopper count, item count.

**Create a simulated run:**
```
POST /api/runs/sim
Content-Type: application/json

{
  "scenario_id": "01_normal_purchase",
  "seed": 7
}
```

Response: `ObservatorySnapshot` with the initial state at t=0.

**Step the simulation:**
```
POST /api/runs/{run_id}/step
```

Steps forward by the configured `step_interval_s` (default 0.1 s).

**Advance by time:**
```
POST /api/runs/{run_id}/advance
Content-Type: application/json

{
  "seconds": 5.0
}
```

**Get current state:**
```
GET /api/runs/{run_id}/snapshot
```

Returns the full state: tracks, events, carts, confidence decisions.

**Reset to t=0:**
```
POST /api/runs/{run_id}/reset
```

All Observatory mutation routes return the complete snapshot under a lock, ensuring a client sees state and events that are guaranteed to be consistent at that same instant.

### Observable badges

SIM runs are labelled `SIMULATED` in the Observatory UI, making it clear a run is synthetic. The run's mode is still "LIVE" so the existing panels work unchanged, but `ObservatoryLiveStatus.simulated` carries the scenario_id and seed.

## What SIM does NOT exercise

- **TiLiveSession transport layer**: Phase 3 tests the UART reconnect, queue management and frame ordering independently. The lab emulator produces bytes but skips the serial transport and reader-thread scheduling, so nondeterminism is avoided.
- **Real hardware**: No physical mmWave board or RFID reader is used.
- **Multi-radar fusion**: A single virtual radar is simulated; production fusion for multiple radars is future work.
- **Computer vision**: No cameras are simulated in Phase 4; vision is added in Phase 7.

## Scenario expectations

Every scenario is deterministic for a given seed and includes both ground truth and expected outcomes:

- **Scheduled interactions**: The sequence of shopper actions (enter, pick, handoff, misplace, exit) timed against their scripted paths.
- **Expected events**: The fusion engine should infer these `RetailEvent` types (PICK, PUTBACK, MISPLACE, HANDOFF, EXIT_WITH_ITEM).
- **Expected exits**: Which EPCs each shopper should be leaving with.

Scenario evaluation compares actual inferred events against expected events. A WAIT/REVIEW proposal instead of a COMMIT is acceptable where the scenario is ambiguous by design (e.g., three shoppers crowding one rack with only one pick).

## Metrics

The lab captures:

- Frame parsing success/rejection counts
- Observation ingestion and rejection counts
- Inferred event timing and attribution
- Confidence decision distribution (COMMIT / WAIT / REVIEW rates)
- Time-to-commit
- Cart correctness at exit

These metrics are useful for algorithmic improvement but are **not** presented as measured real-world accuracy. Simulator output is simulator output; hardware validation is separate.
