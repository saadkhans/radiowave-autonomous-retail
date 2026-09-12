# Hardware Experiment Template

Copy this file to `YYYY-MM-DD-<short-slug>.md` in `docs/experiments/` for each hardware
characterization experiment and fill in every section.

**Never commit raw production captures or private data.** Record synthetic/lab data only under
`data/` paths that are git-ignored (see `.gitignore`: `data/raw/`, `data/captures/`,
`data/private/`) unless the data has been explicitly redacted and reviewed for customer video,
biometric data, payment data, or other private information. Reference such files by path/format
in this record rather than embedding raw data inline. This applies to all sensors (mmWave, RFID,
CV) per the data/security rules in `CLAUDE.md` and `README.md`.

---

## experiment_id
<!-- Short, unique, stable identifier, e.g. mmwave-crossing-001 -->

## Date / time (UTC)
<!-- ISO 8601, e.g. 2026-01-15T14:30:00Z -->

## Software commit SHA
<!-- Exact commit of this repo used to run the experiment -->

## Sensor
- **Model:**
- **Firmware version:**
- **Configuration:** <!-- profile/config file or inline parameters (scan rate, frequency, power, filters, etc.) -->
- **Mounting position:** <!-- physical location/height/orientation description -->

## Coordinate calibration
Sensor pose in store (world) frame:
- **Position (m):** x=, y=, z=
- **Yaw (rad):**
- **Pitch (rad):**
- **Roll (rad):**
- **Calibration method:** <!-- e.g. survey, fiducial markers, manual measurement -->

## Item / tag
<!-- Omit this section for people-tracking-only (mmWave-only) experiments -->
- **EPC:**
- **GTIN:**
- **Tag inlay:**
- **Placement:** <!-- on-item location, orientation -->

## Scenario
<!-- Reference to simulator scenario id if this experiment mirrors/validates a synthetic scenario, e.g. scenario-01. "N/A" if purely exploratory. -->

## Ground truth
- **Method:** <!-- e.g. manual annotation, reference camera, marked walk path -->
- **File:** <!-- path to ground truth data -->

## Raw observations
- **Path:**
- **Format:**

## Normalized observations
- **Path:**
- **Format:** <!-- e.g. radiowave contracts JSONL/parquet, per radiowave/contracts -->

## Expected result
<!-- What the experiment was designed to show/confirm -->

## Actual result
<!-- What was actually observed -->

## Metrics
<!-- Fill in what's applicable; add rows as needed -->
- Attribution accuracy:
- False pick rate:
- PICK latency:
- Track continuity:
- Other:

## Notes
<!-- Free-form observations, anomalies, environmental factors -->

## Failure classification
<!-- If the experiment did not meet expected result, select one or more; otherwise mark "N/A" -->
- [ ] SENSOR_DROPOUT
- [ ] CALIBRATION_ERROR
- [ ] ASSOCIATION_AMBIGUITY
- [ ] STATE_MACHINE_THRESHOLD
- [ ] TAG_READ_FAILURE
- [ ] OCCLUSION
- [ ] MULTIPATH
- [ ] OTHER (describe):
