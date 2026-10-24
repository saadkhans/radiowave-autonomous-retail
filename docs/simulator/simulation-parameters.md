# Simulation Parameters and Configuration

Every tunable parameter in the Virtual Store Lab is an **explicit assumption**, not a measurement of real hardware. None has been calibrated against a real IWR6843, real RAIN RFID reader, or production store environment.

This page documents every configurable knob, its default value, and what it models. For more context, see `radiowave/simulator/lab/scenarios.py`, `sensors/ti_radar.py`, and `sensors/rfid.py`.

## Calibration doctrine

**Before hardware arrives:**
- Parameters are assumptions, chosen for plausibility.
- Scenarios exercise the algorithm under these noise/fault distributions.
- Metric changes are attributed to the algorithm alone (truth is fixed, noise varies).

**After hardware arrives:**
1. Record real sensor captures (UART bytes, RFID reads) in a variety of environments.
2. Measure empirical distributions: position noise, dropout rates, latency, ID stability.
3. Fit these parameters to the measured distributions.
4. Rerun the deterministic scenario matrix against calibrated parameters.
5. Compare simulated vs. real behavior for each scenario.
6. Use mismatch as an engineering signal: find the model gap.

Simulator output is **never** validation of real-world accuracy. It is a tool for algorithm development against explicit assumptions that will be validated and refined when hardware is available.

## World configuration

Defined in `radiowave.simulator.lab.world.WorldConfig`.

| Parameter | Default | Type | Meaning |
|---|---|---|---|
| `tick_hz` | 20.0 | float | Simulation tick rate (Hz). Fixed-timestep physics runs at this cadence; `dt = 1.0 / tick_hz`. |
| `seed` | 7 | int | Master seed for all randomness in the world (actor jitter, RNG stream seeding). Determines noise for a given scenario but not truth. |

## TI mmWave radar: FOV configuration

Defined in `radiowave.simulator.lab.sensors.ti_radar.TiRadarFovConfig`.

| Parameter | Default | Unit | Meaning |
|---|---|---|---|
| `max_range_m` | 12.0 | metres | Maximum horizontal (sensor-frame hypot(x, y)) range at which targets are reported. ASSUMPTION: IWR6843 people-counting demo real azimuth FOV varies by antenna config. |
| `min_range_m` | 0.0 | metres | Minimum horizontal range; targets closer than this (near-field blind spot) are dropped. ASSUMPTION. |
| `horizontal_half_angle_rad` | 1.047 (60°) | radians | Azimuth half-angle from boresight. Targets outside this cone are not visible. ASSUMPTION. |

**Implementation note:** FOV membership is evaluated in the sensor frame (forward/left axes) based on the target's position relative to the sensor. Targets outside the FOV have their native radar IDs retired, exactly as a real tracker would drop a target that walked out of range.

## TI mmWave radar: noise and fault injection

Defined in `radiowave.simulator.lab.sensors.ti_radar.TiRadarNoiseConfig`.

| Parameter | Default | Meaning |
|---|---|---|
| `position_sigma_m` | 0.05 | 1-sigma Gaussian noise (metres) added to each TI-native x/y/z position per emission. ASSUMPTION. |
| `velocity_sigma_m_s` | 0.05 | 1-sigma Gaussian noise (m/s) added to each TI-native vx/vy/vz per emission. ASSUMPTION. |
| `missed_frame_probability` | 0.0 | Probability a whole TI frame (with incremented frame counter and live tracker state) is lost in UART transit. Produces a dropped packet, not a paused tracker. ASSUMPTION. |
| `target_dropout_probability` | 0.0 | Per-target, per-frame probability a visible target is skipped in this frame's TLV but retains its native ID. Resumes next frame under the same ID (transient miss). ASSUMPTION. |
| `false_target_rate` | 0.0 | Per-frame probability one spurious clutter target (with random in-FOV position, zero velocity) is added. ASSUMPTION. |
| `native_id_switch_probability` | 0.0 | Per-track, per-frame probability the track's native ID is deliberately retired and replaced mid-track (firmware track-loss/reacquire while the person keeps walking). ASSUMPTION. |
| `native_id_reuse_probability` | 0.0 | Probability a newly assigned native ID is drawn from recently-retired IDs rather than a never-before-used one. Exercises normalizer stream-generation scoping against genuinely reused (not monotonically growing) IDs. ASSUMPTION. |
| `frame_jitter_max_skip` | 0 | Upper bound on random jump added to frame counter per emission, simulating firmware frame numbers not exactly one apart. Frame counter always remains strictly non-decreasing (only `reset_stream()` can reverse it). ASSUMPTION. |

**Implementation notes:**
- Position and velocity noise are applied independently per axis.
- When a frame is missed, the emulator's internal frame counter and tracker state still advance (mirroring real firmware); only the bytes are dropped.
- Native ID retirement only happens when a track leaves the FOV (geometric) or when `native_id_switch_probability` triggers (fault).
- Recently-retired IDs are pooled (max 16) for reuse; this keeps "reuse" a plausible fault, not an artifact of an unbounded pool.

## RFID reader: antenna zone configuration

Defined in `radiowave.simulator.lab.sensors.rfid.RfidAntennaZoneConfig`.

Each logical antenna zone represents one read point. Multiple zones can be configured (e.g., one per merchandise rack or exit portal).

| Parameter | Default | Meaning |
|---|---|---|
| `sensor_id` | (required) | Must match a registered RFID `Sensor` in the store so reads normalize through the real adapter path. |
| `position` | (required) | Antenna's world-frame position (metres). |
| `antenna_port` | "1" | Vendor label for this antenna/port (e.g., "1", "2", "exit_1"). Metadata only; never used as identity. |
| `max_range_m` | 6.0 | Coverage radius (metres). ASSUMPTION: a flat simplification of real antenna gain patterns. |
| `tx_gain_db` | 0.0 | Per-antenna RSSI offset (dB). Used to model heterogeneous antennas (e.g., higher-gain exit antenna) without a full gain pattern. ASSUMPTION. |

## RFID reader: noise and fault injection

Defined in `radiowave.simulator.lab.sensors.rfid.RfidReadNoiseConfig`.

| Parameter | Default | Meaning |
|---|---|---|
| `read_rate_hz` | 2.0 | Reported poll rate (Hz), metadata only. Callers drive actual polling cadence themselves via how often they call `RfidReaderEmulator.reads()`. ASSUMPTION. |
| `base_read_probability` | 0.85 | Read probability at zero range (before range falloff). ASSUMPTION. |
| `min_read_probability` | 0.05 | Floor read probability at the edge of coverage (max_range_m). ASSUMPTION. |
| `rssi_sigma_db` | 2.0 | 1-sigma Gaussian noise (dB) added to each read's RSSI. ASSUMPTION. |
| `phase_noise_rad` | 0.3 | 1-sigma Gaussian noise (radians) added on top of each read's random phase. RFID phase is not modeled as a true function of distance (tag/cable-length model out of scope). ASSUMPTION. |
| `missed_read_probability` | 0.0 | Per-attempt probability of an independent reader-side miss on top of distance-based detection. Produces dropout gaps. ASSUMPTION. |
| `duplicate_read_probability` | 0.0 | Probability a successful read is reported a second time verbatim (same RSSI/phase, fresh sequence number). Models un-deduplicated reader buffers. ASSUMPTION. |
| `burst_probability` | 0.0 | Probability a successful read additionally triggers a burst of extra, independently-noisy reads of the same tag within the same poll. ASSUMPTION. |
| `burst_extra_reads_max` | 0 | Upper bound on how many extra reads a triggered burst produces (drawn uniformly from [1, burst_extra_reads_max]). ASSUMPTION. |
| `bleed_probability` | 0.0 | Probability an antenna reads a tag beyond its own coverage (inside `bleed_range_multiplier * max_range_m`). Models RF leakage/reflection. ASSUMPTION. |
| `bleed_range_multiplier` | 2.0 | How far past max_range_m a bleed read can occur (multiplier). ASSUMPTION. |
| `disappearance_probability` | 0.0 | Per-attempt probability an in-coverage tag starts a temporary disappearance window (see disappearance_ticks). Models orientation nulls/multipath fade. ASSUMPTION. |
| `disappearance_ticks` | 0 | Length (in calls to `RfidReaderEmulator.reads()`) of a triggered disappearance window. Tag is unreadable at that antenna for exactly this many subsequent calls. ASSUMPTION. |
| `reader_restart_probability` | 0.0 | Per-antenna, per-call probability of a deliberate reader restart, resetting that antenna's read-sequence counter to zero. ASSUMPTION. |
| `delayed_delivery_probability` | 0.0 | Probability a generated read is held back rather than returned immediately. Simulates reader/network buffering. ASSUMPTION. |
| `max_delivery_delay_calls` | 0 | Upper bound (inclusive) on how many later `RfidReaderEmulator.reads()` calls a delayed read is held for (drawn uniformly [1, max_delivery_delay_calls]). ASSUMPTION. |
| `estimate_enabled` | False | Opt-in only: when True, successful non-bleed reads carry a SIMULATED coarse location estimate. Never claimed as real localization. |
| `estimate_sigma_m` | 1.5 | Deliberately coarse 1-sigma (metres) of the optional estimate above. Never claim finer accuracy. |
| `localization_enabled` | False | Opt-in: when True, reads carry a SIMULATED tag localization — the item's true position blurred by localization_sigma_m. This is the class of evidence Foundation's fusion is built around. Set by the lab engine (always True in phase 4 runs). |
| `localization_sigma_m` | 0.5 | 1-sigma (metres) of the simulated localization. Must stay honest at rack scale to preserve ambiguity between neighboring fixtures. Shrinking it toward zero hands fusion the answer key. ASSUMPTION, matches Foundation's synthetic RFID generator. |

**Implementation notes:**

- Read probability has a linear falloff from `base_read_probability` at zero range to `min_read_probability` at the coverage edge.
- RSSI is calculated as a simple log-distance path-loss formula with per-antenna gain offset: `rssi_dbm = -40.0 - 20.0 * log10(distance) + tx_gain_db` plus Gaussian noise.
- Phase is not modeled as a function of distance (that requires tag/cable-length knowledge). Each read gets a random phase plus noise.
- Bleed reads (out-of-zone reads) never carry a location estimate, even if `estimate_enabled` is true; a read already admitted to be outside plausible coverage should not also claim a location.
- Localization (when enabled) blurs the item's true position by `localization_sigma_m`. This simulates the evidence quality a real phase-based reader deployment is expected to deliver in Phase 5. It is NOT a real localization algorithm and must never be shrunk toward zero (that would hand fusion a perfect answer key).

## RFID scenario-level reliability

Defined in `radiowave.simulator.lab.scenarios.RfidReliabilityConfig`.

Scenarios can override these four high-level knobs without modifying the full noise config:

| Parameter | Default | Meaning |
|---|---|---|
| `read_probability` | 0.95 | Probability a tag physically inside a read point's zone is read this cycle. ASSUMPTION. |
| `missed_read_rate` | 0.05 | Rate of a genuinely present tag producing no read at all this cycle. ASSUMPTION. |
| `bleed_rate` | 0.02 | Probability a read point reports a tag actually resting at a neighbouring zone (antenna cross-talk). ASSUMPTION. |
| `false_read_rate` | 0.0 | Probability of a spurious read with no corresponding tag. ASSUMPTION. |

These are convenience knobs mapped into `RfidReadNoiseConfig` by the lab engine. They simplify scenario authoring when full control is not needed.

## Example: customizing a scenario

```python
import dataclasses

from radiowave.simulator.lab.engine import run_lab_scenario
from radiowave.simulator.lab.scenarios import load_scenario

# Sensor configs are frozen dataclasses, so a variant is made with
# dataclasses.replace; the scenario itself is a Pydantic model, so it uses
# model_copy. Mixing the two up is the easy mistake here.
scenario = load_scenario("01_normal_purchase")

noisier_radar = dataclasses.replace(
    scenario.sensors.radar_noise,
    position_sigma_m=0.15,
    velocity_sigma_m_s=0.10,
    target_dropout_probability=0.05,
)
unreliable_rfid = scenario.sensors.rfid.model_copy(
    update={"read_probability": 0.80, "bleed_rate": 0.05}
)
variant = scenario.model_copy(
    update={
        "sensors": scenario.sensors.model_copy(
            update={"radar_noise": noisier_radar, "rfid": unreliable_rfid}
        )
    }
)

baseline = run_lab_scenario(scenario)
degraded = run_lab_scenario(variant)
print("baseline person observations:", baseline.person_observations)
print("degraded person observations:", degraded.person_observations)
```

## Why these defaults matter

Every parameter above was chosen to be:

1. **Plausible**: roughly aligned with published specs and field experience from similar platforms.
2. **Stable**: tweakable without causing wildly different algorithm behavior for tiny changes.
3. **Exercisable**: the default scenarios stress the algorithm in meaningful ways under these assumptions.

After hardware arrives and real captures are measured, these values will be replaced with calibrated distributions. Until then, they are the ground truth for algorithm development.

**Do not** present simulator metrics as real-world accuracy. Do present algorithm improvements measured within the simulator as evidence that refinement is working, and differences between simulated and real behavior (after calibration) as engineering signals that need investigation.
