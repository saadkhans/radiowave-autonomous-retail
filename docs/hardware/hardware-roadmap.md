# Hardware Roadmap

Hardware acquisition is phased so software contracts stabilize before vendor SDKs influence architecture.

## Stage 0 — Software-only foundation
No sensing hardware required. Build mock adapters, synthetic scenarios, recorder/replay, digital twin, fusion and cart logic.

## Stage 1A — mmWave characterization
Target evaluation set:
- 2x TI IWR6843ISK-class 60 GHz development boards
- 1x alternative 60 GHz development platform (e.g. Infineon XENSIV BGT60TR13C family)
- mounts/tripods and measurement tools

Goal: characterize people tracks, crossing, stationary shoppers, occlusion and multi-sensor coordinate alignment.

## Stage 1B — UHF RFID characterization
Target evaluation set:
- 1x fixed RAIN UHF RFID reader with developer APIs
- 4x antennas covering general and proximity zones
- RF cables
- 200–500 representative RFID tags/inlays

Goal: characterize EPC identity, read rate, phase/RSSI where available, movement, zone leakage, body attenuation, metal effects and tag placement.

## Stage 2 — Combined fusion lab
- 3–4 mmWave nodes
- 1–2 RFID readers
- 6–8 antenna/read zones
- 300–1000 tagged test items
- 2 ground-truth PoE cameras
- managed PoE switch
- UPS
- x86 edge workstation

Goal: synchronized world-coordinate trajectories and single/multi-shopper fusion.

## Stage 3 — Mini-store
Scale quantities only after Stage 2 coverage data is measured. Typical lab target: 30–100 items, 2–3 fixtures, 1–3 shoppers.

## Stage 4 — Shadow store / controlled walk-out
Add production-style entry/session and payment/POS integration only after shadow-cart accuracy is proven.

## Procurement rule
Do not purchase production quantities before the preceding stage's exit criteria are measured. Every hardware vendor must sit behind a replaceable adapter.
