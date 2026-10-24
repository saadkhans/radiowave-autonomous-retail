# Hardware Experiment: TI IWR6843 Single-Person Baseline

Copied from `docs/experiments/experiment-template.md`. Fields that were not measured stay `TBD`
with the reason; do not fill in plausible-looking numbers without running the measurement.

**Never commit raw production captures or private data.** Record synthetic/lab data only under
`data/` paths that are git-ignored (see `.gitignore`: `data/raw/`, `data/captures/`,
`data/private/`) unless the data has been explicitly redacted and reviewed for customer video,
biometric data, payment data, or other private information. Reference such files by path/format in
this record rather than embedding raw data inline.

---

## experiment_id
`mmwave-ti-single-person-001`

## Date / time (UTC)
- 2026-10-20 11:31 — first probe, board on a desk (raw capture).
- 2026-10-20 11:52 — Walk 01 (free walk, 119 s, desk stand at 1.40 m).
- 2026-10-20 13:31 — Walk 02 (scripted walk, 72 s).
- 2026-10-22 12:51 — Observatory live run `run-0004` (35 min, room-limited firmware config,
  walk-out/walk-back and USB unplug tests; the unplug test ran in the preceding `run-0001`).

## Software commit SHA
`856d44da4a6bd15dad81f9ff770c4dfdeef9060c` (`claude/ti-mmwave-live-v0`). The fixes found during
this experiment (capture exit code, BOM-tolerant config loading) landed after it and do not change
parsing or normalization.

## Sensor
- **Model:** TI IWR6843ISK, standalone (no MMWAVEICBOOST), TI part PROC073. USB bridge: Silicon
  Labs CP2105 (CLI = Enhanced COM port, data = Standard COM port). USB-powered.
- **Firmware version:** Radar Toolbox `4.20.00.07`,
  `People_Tracking/3D_People_Tracking/prebuilt_binaries/3D_people_track_6843_demo.bin`, flashed
  with UniFlash 9.7.
- **Configuration:** TI `chirp_configs/ISK_6m_default.cfg` with two edits:
  `sensorPosition 1.40 0 0` (Walks 01–02 and run-0004) and, from 2026-10-22, the three boundary
  boxes limited to the room (`boundaryBox -2.40 1.29 0.30 3.90 -1.00 2.50`, static and presence
  boxes `-2.40 1.29 0.50 3.80 -1.00 2.50`). Radiowave `TiLiveConfig`: a git-ignored copy of
  `configs/examples/ti-iwr6843-lab.json` with the ports and pose below; layout pinned to `3d_v2`.
- **Mounting position:** on a desk stand at the front of a 3.89 m × 4.0 m office, board upright
  (antenna patches at the top), boresight horizontal (checked with a phone level), 1.40 m above
  the floor, 2.50 m from the left wall and 1.39 m from the right wall.

## Coordinate calibration
Sensor pose in the store (world) frame; origin at the front-left corner, x toward the right wall,
y into the room:
- **Position (m):** x=2.50, y=0.00, z=1.40
- **Yaw (rad):** 1.5708 (π/2, boresight along +y)
- **Pitch (rad):** 0
- **Roll (rad):** 0
- **Calibration method:** tape measure for height and both side walls; back wall depth from the
  operator; yaw by eye (board square to the front wall). Walks 01–02 used an earlier, narrower
  twin (radar at x=1.5 in a 3 m-wide box); their world x values are offset by 1.0 m from this
  frame.

## Item / tag
N/A — people-tracking-only (mmWave) experiment.

## Scenario
N/A — hardware bring-up, not a synthetic scenario replay.

## Ground truth
- **Method:** operator-described walk scripts with 5 s holds (Walk 02), plus an operator account of
  who was in the room. No marked floor path and no reference camera.
- **File:** none. Position-error metrics below are therefore not measured.

## Raw observations
- **Path:** `data/captures/raw-bringup-01.bin` (15 s, board on a desk, git-ignored)
- **Format:** Raw TI UART byte stream (magic word + 40-byte frame header + TLV records; see
  `radiowave/adapters/mmwave/ti/protocol.py`). TLVs present: 1010, 1011, 1012, 1020, 1021.

## Normalized observations
All git-ignored, format-v2 JSONL, replayable with `python -m radiowave.cli replay <path>`:
- `data/captures/walk-01.jsonl` — free walk, 2,173 frames, 0 rejected.
- `data/captures/walk-02-lateral.jsonl` — scripted walk, 1,363 frames, 0 rejected.
- `data/captures/live-radar-ti-01-run-0004-20261022T125152.717370Z.jsonl` — Observatory live
  capture, 2,100 s.

## Expected result
A single person walking through the field of view produces one continuous canonical person track
with stable position/velocity, correct lateral and forward direction on entry/movement, clean
disappearance on exit, and re-acquisition on re-entry — establishing baseline measurements for the
metrics below before any multi-person or cross-modal work.

## Actual result
All eight manual acceptance checks in `docs/hardware/ti-iwr6843-first-bringup.md` passed; the
Observatory display check was verified from the live map's drawn elements and is still to be
confirmed by eye. Axis conventions are right: walking toward the right wall raised world x,
walking left lowered it, and walking away raised y. With TI's default 8 m tracking boxes,
multipath ghosts became canonical person tracks (8 tracks for one walker and one seated person in
Walk 02). After limiting the firmware's boxes to the room, the first ~100 s of run-0004 that were
inspected showed no ghosts (5 tracks, all plausibly real). The rest of run-0004 (35 min) and the
earlier hour-long run-0001 (53 canonical tracks in a working office) have not been classified yet,
so a long-run ghost rate is not established.

## Metrics
- **Detection range (m):** walker tracked from y≈0.1 m to y≈4.4 m forward (Walk 01), i.e. to the
  back wall of this room; the maximum range of the sensor was not reached.
- **Lateral error (m):** TBD — needs a marked ground-truth path.
- **Range error (m):** TBD — needs a marked ground-truth path. Against the operator's paced
  estimates the radar read ~10–15% long (2 m → 2.3 m, 3 m → 3.4 m), which is within the estimate's
  own uncertainty.
- **Track continuity (%):** Walk 02: one canonical track for the walker's whole presence
  (9.2 s → 62.6 s), no gaps.
- **ID switch count:** Walk 02: 0 for the walker. Walk 01: the walker's TI track and its TV-wall mirror crossed at 98 s and their ids
  appear to have swapped (counts as 1).
- **Re-acquisition behaviour:** run-0004: each time the walker left view near the sensor's
  front-right and came back, the old canonical track ended and a new one was created (new id). No
  stuck or frozen tracks.
- **Detection latency (s):** TBD — entry time not ground-truthed.
- **Track termination latency (s):** TI track ended at the doorway exit (Walk 02, 62.6 s); the
  canonical track was ENDED by the end of the recording ~9 s later, consistent with fusion's
  `end_after_s` = 8 s.
- **Frame rate (Hz):** 18.0–18.4 (TI config frame period 55 ms ≈ 18.2 Hz).
- **Dropout behaviour / duration:** USB pulled mid-run (run-0001): API kept serving, status went
  ERROR after the reconnect budget. After re-plugging and re-sending the `.cfg`, Reconnect resumed
  STREAMING in the same run (`reconnects=1`, `generation=2`, 1 rejected frame at the cut).
- **Static-person behaviour:** a seated person was tracked continuously for the whole of Walk 01
  (119 s), position spread about ±0.4 m in x and ±0.2 m in y, firmware confidence ~0.9.
- **Walking-speed behaviour:** tracked at 0.4–1.5 m/s; not tested systematically.
- **Position RMSE (m):** TBD — needs ground truth.
- **Height:** reported z for people was mostly −0.5…+0.5 m on this 1.40 m, untilted mount, well
  below a standing person's centroid. Not yet explained; treated as uncalibrated.
- **Replay determinism:** two replays of Walk 02 in separate processes produced byte-identical
  pipeline results (SHA-256 over the full 540 KB `PipelineResult`).

## Notes
- Room: marble floor, two large flat TV panels on the right wall about 1.4 m from the sensor, an
  arched doorway in the right wall near the back, a metal equipment case on the floor, desks on
  the left with a seated person at times.
- The TV wall produced mirror tracks offset ~1.8 m to the right of the walker, moving in step with
  them. Other ghosts sat behind the back wall or below the floor (z down to −1.2 m).
- Reconfiguring the board while it was running hung it; power-cycle before sending a new `.cfg`.
- The board forgets its configuration on any power loss.

## Failure classification
- [ ] SENSOR_DROPOUT — only the deliberate USB unplug; handled.
- [ ] CALIBRATION_ERROR
- [ ] ASSOCIATION_AMBIGUITY
- [ ] STATE_MACHINE_THRESHOLD
- [ ] TAG_READ_FAILURE
- [ ] OCCLUSION
- [x] MULTIPATH — ghost tracks from the TV wall and beyond the room until the firmware's boundary
      boxes were limited to the room.
- [x] OTHER (describe): reported height reads low (see Metrics); height ids above 255 untested.
