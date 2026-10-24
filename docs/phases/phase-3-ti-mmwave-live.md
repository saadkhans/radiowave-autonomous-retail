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

## Status

PR #3 implements the TI IWR6843 adapter as a package under `radiowave/adapters/mmwave/ti/`:

- `transport.py` / `serial_transport.py` — byte-stream abstraction and the pyserial-backed data
  UART transport (optional `hardware-ti` extra; deferred import so the package stays importable
  without pyserial).
- `protocol.py` — TI mmWave SDK 3.x / Industrial Toolbox "3D people counting" UART constants only
  (magic word, frame header, TLV types, target-record layouts); no parsing logic.
- `parser.py` — stateful frame parser built on those constants, with bounded buffers/limits
  (`TiParserLimitsConfig`) so a corrupt or hostile byte stream cannot grow memory.
- `models.py` — `TiFrame`/`TiTarget`, the parser's native (still TI-shaped) output.
- `adapter.py` — the vendor boundary: normalizes `TiFrame`/`TiTarget` into store-frame
  `PersonObservation` values via the same `NativeRadarSample`/`ObservationNormalizer` contracts the
  synthetic radar uses.
- `config.py` — `TiLiveConfig` (store twin + serial ports + adapter policy), `TiSerialConfig`,
  `TiCoordinateConvention`, `TiObservationPolicy`, `TiReconnectPolicy`, `TiRawCaptureConfig`,
  `TiParserLimitsConfig`.
- `health.py` — `TiStreamState` (DISCONNECTED/CONNECTING/STREAMING/STALE/ERROR) and
  `TiAdapterDiagnostics`, mapped to the shared `SensorHealthStatus` (OK/DEGRADED/OFFLINE).
- `session.py` — `TiLiveSession`: one reader thread, one bounded observation queue, no broker;
  owns reconnect/backoff and stream-generation bookkeeping.
- `commands.py` — `radiowave mmwave ti probe|capture` CLI commands, built on the same session.

Also added: the live API (`radiowave/api/live.py`, `radiowave/api/routes/live.py`,
`GET /api/live/status`, `POST /api/runs/live`, `POST /api/runs/{id}/stop`,
`POST /api/runs/{id}/reconnect`), an Observatory LIVE mode (radar status badge, Start/Stop/
Reconnect, capture toggle), format-v2 capture/replay support, and the example config
`configs/examples/ti-iwr6843-lab.json`.

### Architecture decisions

- **Timestamps.** Every observation is stamped with the host's timezone-aware UTC receive time.
  TI frame numbers and CPU cycle counters are not a synchronized clock and are retained only as
  native metadata (`native_frame_number`, `native_time_cpu_cycles`).
- **Native ids are hints, not identity.** The TI tracker id is kept as `native_track_id`; fusion
  assigns the canonical `P0001`-style identity and owns continuity/re-acquisition.
- **Stream generations.** A frame number going backwards, or a reconnect, opens a new stream
  generation so `(generation, frame)` stays a unique key and stale/re-read frames from a previous
  connection can never collide with new ones. A duplicate frame within one generation is dropped
  and counted.
- **Coordinate convention.** TI's reported axes (`X` lateral, `Y` forward/range, `Z` up from the
  configured floor by default) are mapped explicitly into Radiowave's sensor frame (x forward, y
  left, z up, origin at the sensor) via `TiCoordinateConvention.to_sensor_frame`; the store twin's
  `SensorPose` then places the result in the world frame. Every axis assumption is configurable
  (`forward_axis`, `lateral_positive`, `z_origin`) and must be verified per-install (see
  `docs/hardware/ti-iwr6843-first-bringup.md`).
- **Uncertainty / confidence baselines.** Until calibration data exists, the adapter reports a
  conservative configured isotropic sigma (`observation.baseline_sigma_m`, default 0.35 m) and
  never reports full confidence: firmware confidence is used only when present and in `[0, 1]`,
  and is always capped at `confidence_ceiling` (default 0.9); otherwise `baseline_confidence`
  (default 0.6) is used.
- **Bounded queue and backoff.** `TiLiveSession` reads on one daemon thread into a fixed-size
  queue that drops the oldest observations (and counts the drops) if the consumer falls behind, so
  the reader never blocks and never grows memory. Reconnection uses bounded exponential backoff
  (`reconnect.initial_delay_s`, `max_delay_s`, `max_attempts`); a stream is reported `STALE` after
  `observation.stale_after_s` without a parsed frame, and a transport error never crashes the API.

### Known limitations

- Radiowave does not flash or configure TI firmware; the board must already be running the "3D
  people counting" demo with a chirp/tracker config sent externally before Radiowave connects.
- The CLI/configuration UART is unused by this repository (`serial.cli_port` is retained only for
  possible future automation).
- Uncertainty and confidence are configuration-level baselines, not measured/calibrated values —
  see `docs/experiments/ti-iwr6843-single-person-baseline.md`.
- Only a single radar is supported per `TiLiveConfig`; multi-radar fusion is out of scope for this
  phase.
- The compressed point cloud (TLV 1020) and presence indication (TLV 1021) are parsed for
  debugging/diagnostics only and are not part of the normalized `PersonObservation` contract.
- Hardware acceptance (the manual test plan in `docs/hardware/ti-iwr6843-first-bringup.md`) has not
  been run against a physical board yet; all such checks are marked NOT RUN.
