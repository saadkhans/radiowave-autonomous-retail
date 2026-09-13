# Phase 3 — TI mmWave Live People Tracking

## Objective

Integrate a real TI IWR6843-class mmWave radar into Radiowave without weakening the vendor-neutral Foundation contracts or Observatory replay path.

The first hardware milestone is:

> real radar detections -> normalized `PersonObservation` -> canonical `PersonTrack` -> live Observatory visualization

## Scope

- Add a TI IWR6843 adapter behind the existing `PeopleTracker` interface.
- Keep TI SDK/native packet structures isolated inside the adapter boundary.
- Convert device-native coordinates into the shared store/world frame before fusion.
- Preserve canonical person-track identity, uncertainty, timestamps, sensor provenance and health.
- Add live ingestion mode to Observatory without removing deterministic synthetic/replay mode.
- Add calibration/configuration for radar pose and coordinate transform.
- Capture raw-enough normalized observations for deterministic replay and comparison against synthetic data.
- Add sensor-health/error visibility for disconnected, malformed, stale and recovering radar streams.

## Non-goals

- RFID integration.
- Multi-vendor radar fusion beyond keeping the interface vendor-neutral.
- Production CV.
- Payment/POS.
- Custom TI DSP or raw ADC processing.
- DCA1000 support.
- Cloud deployment or message-broker infrastructure.

## First supported hardware

- TI IWR6843ISK as the primary reference board.
- Point-cloud / people-tracking output over the supported serial/USB path.
- Existing Infineon hardware remains a later benchmark, not part of this PR.

## Architecture rules

1. No TI native identifier becomes canonical shopper identity.
2. No vendor coordinate reaches `FusionEngine` directly.
3. Hardware adapters emit the same normalized contracts used by mocks/replay.
4. Hardware disconnect/reconnect must not crash the pipeline.
5. Device timestamps must be normalized to UTC/monotonic ordering policy before ingestion.
6. Missing/invalid frames are observable and safely rejected.
7. Existing synthetic scenarios and replay tests remain green.
8. Observatory must clearly distinguish LIVE from REPLAY mode.

## Definition of done

- A connected TI IWR6843 board can produce normalized person observations.
- One moving person can be visualized live in Observatory on the store map.
- Radar pose/world transform is configurable and tested.
- Disconnect/reconnect and malformed-frame behavior is covered by tests.
- A short captured normalized session can replay deterministically.
- No real hardware is required for CI; adapter parser/transport tests use fixtures/mocks.
- Root lint, typecheck, test, build and secret-scan commands remain green.

## Review gate

This PR should stay focused on the first real sensor vertical slice. RFID and cross-modal attribution start only after live radar tracking is stable and measurable.
