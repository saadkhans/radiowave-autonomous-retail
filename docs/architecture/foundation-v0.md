# Foundation v0 — module map and design notes

Foundation v0 is the hardware-independent core: everything a real mmWave or RFID
integration will plug into later, exercised end to end on synthetic data.

## Package layout

| Package | Responsibility | Depends on |
| --- | --- | --- |
| `radiowave.contracts` | Canonical Pydantic v2 contracts: geometry, store twin, observations, tracks, events, confidence, sessions, recording entries | — |
| `radiowave.digital_twin` | Rigid sensor-to-store transforms and indexed store lookups (`StoreRegistry`) | contracts |
| `radiowave.adapters.{mmwave,rfid,vision}` | Native sample contracts per modality (`base.py`) and mock sources (`mock.py`) | contracts, simulator |
| `radiowave.ingestion` | Native -> store-frame normalization; idempotent deduplication | digital_twin |
| `radiowave.fusion` | Canonical person/item tracks, item state machine, explainable association ledger, baseline fusion engine, all thresholds in `config.py` | digital_twin |
| `radiowave.confidence` | COMMIT / WAIT / REVIEW from configured thresholds | contracts |
| `radiowave.cart` | EPC-level cart with idempotent event application | contracts |
| `radiowave.replay` | JSONL / Parquet recorders and replay sources, pacing clocks | contracts |
| `radiowave.simulator` | Scripted ground-truth scenarios, deterministic generators, lab store, runner | adapters, pipeline |
| `radiowave.pipeline` | Deterministic glue: observations -> fusion -> confidence -> cart | fusion, confidence, cart |
| `radiowave.cli` | `scenarios`, `simulate`, `replay` commands | pipeline, simulator |

Nothing under `radiowave/` imports a vendor SDK, a message broker, or a database.

## Data flow

```text
scenario ground truth ──> RadarGenerator ──> NativeRadarSample (radar frame)
                     ──> RfidGenerator  ──> NativeRfidRead   (read-point frame)
                     ──> VisionGenerator──> NativeVisionDetection (camera frame)
                                             │
                          ObservationNormalizer (StoreRegistry transforms)
                                             │
              PersonObservation / ItemObservation / VisionEvidence  (store frame, UTC)
                                             │
                     ObservationDeduplicator ──> Recorder (JSONL / Parquet)
                                             │
                        BaselineFusionEngine.ingest(); .step(now) every 0.25 s
                          ├─ PersonTrackManager   canonical P0001… ids, multi-sensor merge, re-acquisition
                          ├─ ItemTrackManager     EMA location per EPC at configured accuracy
                          ├─ ItemStateMachine     ON_FIXTURE → INTERACTION_CANDIDATE → CARRIED → …
                          └─ CandidateLedger      ranked shoppers per item with per-feature evidence
                                             │
                                 RetailEvent proposals (PICK, CARRY, PUTBACK, MISPLACE, HANDOFF, EXIT_WITH_ITEM)
                                             │
                        ThresholdConfidenceEngine ──> COMMIT / WAIT / REVIEW
                                             │ COMMIT
                                    InMemoryCartEngine (EPC lines, idempotent)
```

## Identity rules

* **Shopper identity** is the canonical `PersonTrack.track_id` assigned by fusion.
  Vendor track numbers, antenna ports and native coordinates are retained only in
  `metadata` and never used as keys.
* **Item identity** is the EPC of one physical unit. `Product` (GTIN/SKU) is a
  commercial type; two units of the same GTIN are two `Item`s, two `ItemTrack`s and
  two cart lines.
* **Frames**: `SensorCoordinate` values exist only inside adapters and the
  normalizer. Fusion, events and carts see `WorldCoordinate` only.

## Item state machine

```text
ON_FIXTURE / MISPLACED
   ── smoothed estimate > movement_threshold_m on movement_confirm_reads consecutive fresh reads ──> INTERACTION_CANDIDATE
INTERACTION_CANDIDATE
   ── displacement > carry_displacement_m or left home zone, for carry_min_duration_s ──> CARRIED
   ── back within threshold, or candidate_timeout_s ──> previous rest state   (jitter, no event)
CARRIED
   ── estimate drift < rest_displacement_m over rest_window_s, nobody within hold_radius_m,
      held for rest_confirm_s:  near home fixture ──> ON_FIXTURE (PUTBACK) else ──> MISPLACED (MISPLACE)
   ── item estimate, or committed carrier with item within exit_item_radius_m, inside exit boundary ──> EXITED (EXIT_WITH_ITEM)
```

HANDOFF is an attribution change while CARRIED: another shopper out-scores the
committed carrier by `handoff_min_margin` for `handoff_confirm_s`.

The `hold_radius_m` rule is deliberate: with RFID alone, an item standing still
next to a stationary shopper is indistinguishable from an item in that shopper's
hand, so rest is only declared once every tracked person has stepped away.

## Association evidence

Every (item, shopper) pair is scored per step into an `AssociationEvidence`:
distance, distance trend, velocity similarity, movement-start timing, co-motion,
departure-zone proximity and (when a provider exists) vision. Two weight profiles
apply: `pick_weights` while attributing who took the item off the fixture, and
`carry_weights` once a carrier is committed (pick-time features no longer
discriminate). Scores are smoothed per pair in the `CandidateLedger` (EMA), and
every proposed event carries the full ranked candidate list with weights and raw
features. Nothing returns an unexplained scalar.

## Confidence decisions

`ConfidenceThresholds` is the only place thresholds live:

| Condition | Decision |
| --- | --- |
| confidence ≥ `commit_min_confidence` and margin ≥ `commit_min_margin` | COMMIT |
| waited > `max_wait_seconds` | REVIEW |
| confidence ≥ `wait_min_confidence`, or still inside `review_grace_seconds` | WAIT |
| otherwise | REVIEW |

Margin is the gap between the best and runner-up candidate; with fewer than two
candidates there is nothing to disambiguate and margin is 1.0. Physical events
(PUTBACK, MISPLACE, EXIT_WITH_ITEM) use the state machine's
`physical_event_confidence`.

## Determinism and replay

* Generators use `numpy.random.default_rng(seed + modality offset)`; the same
  scenario always produces byte-identical observations.
* The pipeline steps fusion on simulated time only; no wall clock is read.
* While an item has no fresh reads (stale) it is neither scored nor attributed; a cached
  position is never treated as new evidence, and dwell timers (handoff, rest) restart after
  the blackout. Exit detection also accepts zone-only reads from an exit portal.
* A pending WAIT proposal whose episode has ended (item put back, re-picked, exited) is
  dropped rather than decided late.
* A localization gap longer than `reset_after_s` breaks motion continuity: a resting item
  re-initializes where it reappears (no PICK is inferred from the jump) and a carried item
  keeps its committed carrier. LOST shoppers are never re-scored from item-only updates.
* A JSONL or Parquet recording replays to the identical committed events, carts
  and item states (`tests/integration/test_pipeline_replay.py`). Every recording
  starts with `STORE_TWIN` and `PIPELINE_CONFIG` entries so replay uses the original
  twin and thresholds.
* Observations older than the pipeline clock are dropped and counted, never applied.

## Known limitations (Foundation v0)

* RFID mock models range-dependent read probability and Gaussian location error
  only: no body attenuation, metal, multipath or antenna pattern effects.
* Rest detection defers PUTBACK/MISPLACE while any person is within
  `hold_radius_m`; a shopper who sets an item down and keeps standing next to it
  delays the event until they move.
* Two shoppers walking together holding one item remain ambiguous by design
  (WAIT then REVIEW) unless vision evidence resolves it.
* Person tracking merges by proximity gating; crossing shoppers in a dense crowd
  are out of scope until real radar characterization data exists.
* Sessions are created on first sight, not strictly at the entry boundary. Entering
  the exit boundary ends the session; a track that steps back in gets a new session
  and a new cart lifecycle (`<track>#2`), and exited sessions/carts never receive
  further attribution.
* Items with no configured home fixture rest as MISPLACED, never ON_FIXTURE.
* A cart closes when the shopper's session ends (stamped with the session end);
  `EXIT_WITH_ITEM` only freezes that item's line as the settlement candidate. When the
  track was lost at the door, the cart is closed at the end of the run with the latest
  exit event, provided that event is not older than the newest line.
* Observations below the configured minimum confidence are ignored by tracking, and
  observations naming an unknown sensor (or one of another modality) are rejected at intake.
* Candidate scores move only when the item or the shopper produced a new sample; elapsed
  fusion ticks never accumulate evidence.
* A shopper whose track ends (left radar coverage) is dropped from every candidate
  ledger; if they were the true carrier the item is later attributed to whoever remains.
* No persistence, streaming or services: everything runs in-process on synthetic data.
