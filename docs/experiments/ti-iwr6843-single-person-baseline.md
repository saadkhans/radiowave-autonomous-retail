# Hardware Experiment: TI IWR6843 Single-Person Baseline

Copied from `docs/experiments/experiment-template.md`. Every measured field below is `TBD` until
this experiment is actually run against hardware; do not fill in plausible-looking numbers without
running it.

**Never commit raw production captures or private data.** Record synthetic/lab data only under
`data/` paths that are git-ignored (see `.gitignore`: `data/raw/`, `data/captures/`,
`data/private/`) unless the data has been explicitly redacted and reviewed for customer video,
biometric data, payment data, or other private information. Reference such files by path/format in
this record rather than embedding raw data inline.

---

## experiment_id
`mmwave-ti-single-person-001`

## Date / time (UTC)
TBD

## Software commit SHA
TBD

## Sensor
- **Model:** TI IWR6843ISK
- **Firmware version:** TBD (TI mmWave SDK 3.x / Industrial Toolbox "3D people counting" demo;
  record the exact demo/SDK version flashed via UniFlash)
- **Configuration:** TBD (path to the `.cfg` sent over the CLI UART, including the
  `sensorPosition <height> <azimuthTilt> <elevationTilt>` line; plus the Radiowave
  `TiLiveConfig` JSON used, e.g. a copy of `configs/examples/ti-iwr6843-lab.json`)
- **Mounting position:** TBD (physical location/height/orientation; must match the
  `sensorPosition` sent to the firmware and the pose entered in the `TiLiveConfig`)

## Coordinate calibration
Sensor pose in store (world) frame, as entered in the `TiLiveConfig`'s `store.sensors[0].pose`:
- **Position (m):** x=TBD, y=TBD, z=TBD
- **Yaw (rad):** TBD
- **Pitch (rad):** TBD
- **Roll (rad):** TBD
- **Calibration method:** TBD (e.g. tape measure against the store frame's documented origin,
  compass/protractor for yaw)

## Item / tag
N/A — people-tracking-only (mmWave) experiment.

## Scenario
N/A — hardware bring-up, not a synthetic scenario replay.

## Ground truth
- **Method:** TBD (e.g. manual annotation of timestamps/positions against a marked walk path, or
  a reference camera)
- **File:** TBD

## Raw observations
- **Path:** TBD, e.g. `data/captures/mmwave-ti-single-person-001-raw.bin` (raw UART capture via
  `--raw-capture`, git-ignored)
- **Format:** Raw TI UART byte stream (magic word + 40-byte frame header + TLV records; see
  `radiowave/adapters/mmwave/ti/protocol.py`)

## Normalized observations
- **Path:** TBD, e.g. `data/captures/mmwave-ti-single-person-001.jsonl` (recorded via
  `python -m radiowave.cli mmwave ti capture` or the Observatory's capture checkbox; git-ignored)
- **Format:** Radiowave format-v2 normalized JSONL (`PersonObservation` records; replayable with
  `python -m radiowave.cli replay <path>`)

## Expected result
A single person walking through the field of view produces one continuous canonical person track
with stable position/velocity, correct lateral and forward direction on entry/movement, clean
disappearance on exit, and re-acquisition on re-entry — establishing baseline measurements for the
metrics below before any multi-person or cross-modal work.

## Actual result
TBD

## Metrics
Every value below is `TBD` until measured from this experiment's normalized/raw capture.

- **Detection range (m):** TBD — nearest/farthest distance at which the person is reliably
  detected as a target.
- **Lateral error (m):** TBD — measured lateral position vs. ground-truth walk path.
- **Range error (m):** TBD — measured range (forward-axis) position vs. ground truth.
- **Track continuity (%):** TBD — fraction of the walk duration with an active canonical track,
  excluding intended exits.
- **ID switch count:** TBD — number of times the canonical track id changed during one continuous
  walk (should be 0 within a single continuous presence).
- **Re-acquisition behaviour:** TBD — time and track-id continuity/discontinuity after leaving and
  re-entering the field of view.
- **Detection latency (s):** TBD — time from physical entry into the field of view to first
  emitted observation/track.
- **Track termination latency (s):** TBD — time from physical exit to track disappearance; compare
  against the configured `observation.stale_after_s`.
- **Frame rate (Hz):** TBD — from `probe`/`capture` diagnostics (`fps`).
- **Dropout behaviour / duration:** TBD — any `STALE`/`DISCONNECTED` periods during the run, their
  duration and cause.
- **Static-person behaviour:** TBD — track stability (position jitter, confidence) while the
  person stands still.
- **Walking-speed behaviour:** TBD — track stability across slow/normal/fast walking speeds.
- **Position RMSE (m):** TBD — root-mean-square error of the full track vs. ground-truth path.
- Other: TBD

## Notes
TBD — free-form observations, anomalies, environmental factors (e.g. multipath surfaces, other
moving objects, RF interference).

## Failure classification
- [ ] SENSOR_DROPOUT
- [ ] CALIBRATION_ERROR
- [ ] ASSOCIATION_AMBIGUITY
- [ ] STATE_MACHINE_THRESHOLD
- [ ] TAG_READ_FAILURE
- [ ] OCCLUSION
- [ ] MULTIPATH
- [ ] OTHER (describe):

Not applicable until the experiment is run and `Actual result` is compared against `Expected
result`.
