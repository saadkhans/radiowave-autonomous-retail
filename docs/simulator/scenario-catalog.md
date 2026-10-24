# Lab Scenario Catalog

All 14 scenarios in the Phase 4 Virtual Store Lab are defined in `radiowave/simulator/lab/scenarios.py`. Each is fully deterministic and exercises a headline algorithmic challenge.

## Scenario table

| ID | Name | Shoppers | Items | Duration | Distinguishing Property | Faults |
|---|---|---|---|---|---|---|
| 01 | normal_purchase | 1 | 1 | 24 s | Simplest case: enter, pick, exit | None |
| 02 | putback | 1 | 1 | 30 s | Item returned to home fixture after pick | None |
| 03 | misplace | 1 | 1 | 26 s | Item placed on wrong rack (not home) | None |
| 04 | handoff | 2 | 1 | 32 s | Item passed between two shoppers mid-floor | None |
| 05 | two_shoppers_cross | 2 | 2 | 22 s | Trajectories cross at overlapping times; unrelated items | None |
| 06 | same_sku_distinct_epcs | 2 | 2 (same GTIN) | 40 s | Two distinct EPCs sharing one GTIN; EPC-first identity must be preserved | None |
| 07 | radar_dropout | 1 | 1 | 24 s | mmWave FOV loss for 3 s during carry | RADAR_DROPOUT (9–12 s) |
| 08 | rfid_dropout | 1 | 1 | 24 s | RFID read-point outage at pick fixture | RFID_DROPOUT (5–8 s) |
| 09 | native_id_reuse | 2 | 1 | 36 s | Second shopper's radar native id drawn from first shopper's retired pool | NATIVE_ID_REUSE (14–20 s) |
| 10 | crowded_rack_ambiguity | 3 | 1 | 30 s | Three shoppers crowd one rack; only one picks; attribution ambiguous | None |
| 11 | multiple_items | 1 | 3 | 34 s | One shopper picks items from three different racks | None |
| 12 | exit_with_item | 1 | 1 | 40 s | Shopper carries item through unrelated fixture browsing before exit | None |
| 13 | sensor_restart | 1 | 1 | 24 s | Radar frame counter resets mid-run | RADAR_RESTART (10–10.5 s) |
| acceptance_60s | Phase-4 acceptance gate | 3 | 30 | 60 s | All headlines: picks, putback, handoff, multi-rack, varied exits | None |

## Headline scenarios

### 06: Same SKU, distinct EPCs

This scenario is the phase's flagship test for SKU/EPC separation. Two physically distinct items share the same GTIN (product code):

- Shopper A picks the first EPC off Rack A and exits.
- Shopper B (much later) picks the second EPC of the same GTIN off Rack A and exits.

A bug that collapses SKU identity into EPC identity (or vice versa) would see one item bought twice, one item teleporting between shoppers, or an impossible negative cart balance. Correct fusion keeps the two units distinct, with two separate `ItemTrack` instances and two cart lines.

### acceptance_60s: Phase-4 acceptance gate

The first formal gate for Phase 4: **three shoppers, 30 unique items across three racks, deterministic 60-second simulation.**

Scenario breakdown:

- **Shopper A** (0–48 s): picks two items at Rack A, returns one, receives a handoff mid-floor, and exits with two items.
- **Shopper B** (2–32 s): picks one item at Rack B and exits.
- **Shopper C** (4–42 s): picks two items at Rack C, hands one off to A, and exits with one.

This single scenario exercises every headline behavior in one deterministic run:
- Multiple items per shopper
- Putback
- Handoff between shoppers
- Multi-rack navigation
- Correct cart state at exit for all three shoppers

The run must complete successfully (inferred events match expected, exits correct, carts finalized) through the live Observatory, exercising the full production pipeline (parsing → normalization → fusion → confidence → cart).

## Fault-injection scenarios

Scenarios 07, 08, 09, and 13 demonstrate sensor fault handling:

- **07 (radar_dropout)**: Simulates mmWave FOV loss. Fusion must handle a temporary absence of person observations while an item is carried; re-acquisition restores continuity.
- **08 (rfid_dropout)**: Simulates RFID reader outage. Fusion must attribute the item despite lost reads at the pick fixture.
- **09 (native_id_reuse)**: Exercises the normalizer's stream-generation scoping. When shopper A leaves and their radar native id retires, shopper B's new track is assigned that same just-retired id. The normalizer must not conflate the two based on id alone.
- **13 (sensor_restart)**: Radar frame counter resets (simulating hardware restart). The normalizer must detect the backward frame number and start a new stream generation, resetting native-id scoping.

All faults are seed-controlled and reproducible.

## Ambiguity scenarios

- **10 (crowded_rack_ambiguity)**: Three shoppers stand at the same rack during overlapping times; only one picks an item. Attribution among three candidates is genuinely ambiguous. A WAIT or REVIEW proposal is acceptable (even desirable) here — the scenario is designed to stress the confidence engine.
- **04 (handoff)**: Two shoppers meet mid-floor and one passes an item to the other. This tests dynamic attribution changes (handoff) and trajectory association under transient co-motion.
- **05 (two_shoppers_cross)**: Opposite stress case to handoff: two shoppers' paths cross but they never interact, carrying unrelated items. Fusion must not confuse trajectory proximity for a handoff or item association.

## Determinism contract

Every scenario:

1. **Defines ground truth**: shopper entry times, scripted paths (waypoints with times), and scheduled interactions (pick/putback/handoff at specific timestamps).
2. **Includes expected outcomes**: the fusion engine should infer specific events at specific times and produce specific cart contents at exit.
3. **Is deterministic**: for a fixed seed and scenario id, `LabEngine` produces bit-identical results on any machine, in any order of operations, any number of times.

Evaluation compares **inferred events** (what fusion concluded) against **expected events** (what should happen). Mismatches signal either a scenario authoring error or a bug in the algorithm.

## Running a scenario

```python
from radiowave.simulator.lab.engine import LabEngine
from radiowave.simulator.lab.scenarios import load_scenario

engine = LabEngine(load_scenario("acceptance_60s"))
result = engine.run()

# What actually happened (ground truth: evaluation only, never fed to fusion).
for event in engine.ground_truth.events:
    print(f"TRUTH    {event.event_type.value:18s} {event.ground_truth_person_id} {event.epc}")

# What the system concluded, entirely from simulated sensor data.
for event in result.pipeline.proposed_events:
    print(f"INFERRED {event.event_type.value:18s} {event.epc.value}")

# Carts at the end of the run.
for cart in result.pipeline.cart_state.carts.values():
    print(f"CART {cart.cart_id} shopper={cart.shopper_track_id} status={cart.status.value}")
```
