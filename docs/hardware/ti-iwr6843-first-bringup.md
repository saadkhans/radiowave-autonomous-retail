# TI IWR6843 First Hardware Bring-Up

Procedure for connecting one TI IWR6843-class board (reference: IWR6843ISK) to Radiowave for the
first time. This is a checklist for a single radar in isolation, not a full store install.

Radiowave never flashes or configures the radar's firmware — see step 4. It only reads the
normalized TLV stream produced by TI's own "3D people counting" demo over the data UART.

## Checklist

### 1. Install the serial dependency

The adapter's serial transport uses `pyserial`, an optional dependency (`hardware-ti` extra).
Without it, live mode reports `TI serial support not installed; install
radiowave-autonomous-retail[hardware-ti]` and replay/synthetic mode keep working unaffected.

```sh
# from the repo root, with .venv active
uv pip install -e ".[dev,api,hardware-ti]"
# or
pip install -e ".[dev,api,hardware-ti]"
```

### 2. Connect the IWR6843ISK

Connect the board to the host over USB. The XDS110 debugger on the EVM enumerates two virtual
serial ports (UARTs): a configuration/CLI port and a data port. Wait a few seconds after plugging
in for both ports to enumerate before checking device lists.

### 3. Identify the CLI and data ports

**Windows** — Device Manager, under "Ports (COM & LPT)": look for two "XDS110 Class ..." entries,
e.g. "XDS110 Class Application/User UART" (CLI, COM5) and "XDS110 Class Auxiliary Data Port"
(data, COM6). Or enumerate from the venv:

```powershell
.venv\Scripts\python.exe -m serial.tools.list_ports -v
```

**Linux** — the CLI port is typically the lower-numbered device, the data port the next one:

```sh
ls /dev/ttyUSB* /dev/ttyACM*
.venv/bin/python -m serial.tools.list_ports -v
```

On Linux, add your user to the `dialout` group (or equivalent) if the ports are not readable, then
log out/in for the group change to take effect:

```sh
sudo usermod -a -G dialout "$USER"
```

Port numbering is not stable across machines or reboots — always re-check rather than assuming the
example config's `COM6`/`COM5` (or `/dev/ttyUSB1`/`/dev/ttyUSB0`) placeholders are correct for your
setup.

### 4. Flash and configure the firmware externally

**Radiowave does not flash firmware and does not send the chirp/tracker configuration.** Before
running anything in this repository:

1. Flash the board with TI's "3D people counting" demo (Industrial Toolbox) using TI UniFlash.
2. Configure it with a chirp/tracker `.cfg` file sent over the CLI UART, using TI's own CLI tool
   or Industrial Visualizer. The `.cfg` must include a `sensorPosition <height> <azimuthTilt>
   <elevationTilt>` line — this is what makes the firmware report height (`Z`) relative to the
   floor rather than the sensor.

Radiowave only opens the data UART after this is done; it never talks to the CLI UART itself.

### 5. Measure and enter the radar pose

Edit a copy of `configs/examples/ti-iwr6843-lab.json` (or your own config) so `store.sensors[0].pose`
matches the physically measured mount:

- **Position** (`pose.position.x/y/z`, metres, store frame: origin at a documented corner, x east,
  y north): measure the sensor's physical location in the store/room you've defined.
- **Height** (`pose.position.z`): the mount height above the floor. This must match the
  `sensorPosition` height sent to the firmware in step 4, since the coordinate convention's
  `z_origin: "floor"` default subtracts the *configured* sensor height, not a measured one.
- **Yaw** (`pose.yaw`, radians, about z, right-handed): the direction the boresight (front face)
  points, measured from the store frame's +x (east) axis toward +y (north). A radar facing due
  north (its boresight along +y) is `yaw = pi/2` (`1.5707963267948966`), as in the example config.
  Measure with a compass/protractor against your store frame's axes, not by eye.

When the TI firmware's `sensorPosition <height> <azimuthTilt> <elevationTilt>` configuration already gravity-aligns the output (the 3D people counting demo reports floor-referenced coordinates when this is set), `pose.pitch` and `pose.roll` must stay 0 — the firmware applies the tilt transformation and entering it again in the pose would apply it twice. Only yaw and position are entered in the twin for this firmware. If a firmware reports raw sensor-relative coordinates instead, set `z_origin: "sensor"` in the adapter config and then the measured tilt belongs in the pose.

Also update `serial.data_port` (and `serial.cli_port` if used later) to the ports found in step 3.

### 6. Run probe

```sh
# Windows
.venv\Scripts\python.exe -m radiowave.cli mmwave ti probe --config configs\examples\ti-iwr6843-lab.json --data-port COM6 --seconds 15

# POSIX
.venv/bin/python -m radiowave.cli mmwave ti probe --config configs/examples/ti-iwr6843-lab.json --data-port /dev/ttyUSB1 --seconds 15
```

Add `--raw-capture data/captures/raw.bin` to also dump raw UART bytes for parser debugging
(`data/captures/` is git-ignored). Omit `--data-port` to use the port already in the config file.
Once the probe output shows which layout the board emits (printed as part of the status), pin `adapter.target_record_layout` (`3d_v2`, `3d_v1` or `2d`) in the config — confirm it from the probe output rather than leaving it unset, since an unset layout is only auto-detected when exactly one candidate fits the record count; an ambiguous count is now rejected outright rather than guessed.

While a raw capture is open, also confirm the **target-height record (TLV 1012)** against real bytes: it must be 12 bytes per record (TI's C struct is padded to 4-byte alignment; a hand-packed 9-byte reading rejects every real one-target height TLV and drops the whole frame with it). The parser takes the id from the single byte at offset 0 and skips bytes 1..3 as padding, so a record whose padding carries stale bytes still decodes the right id. Check on real output whether those padding bytes are in fact zero: if the firmware turns out to use a full uint32 id there, ids above 255 would currently truncate. Resolve that before anything downstream consumes `TiTargetHeight.native_track_id`; today heights are carried on the frame for diagnostics and never feed a canonical observation.

### 7. Confirm frames are received

Healthy output looks like one summary line per second plus per-target lines:

```
[STREAMING   ] gen=1 frames=42 rejected=0 dup=0 fps=20.0 obs/s=18.5 age=0.05s observations=185 reconnects=0
  id=3    ti=(0.42, 2.10, 1.65) sensor=(2.10, -0.42, -0.35) world=(4.42, 2.10, 1.65) conf=0.72
```

`probe` exits 0 only if at least one frame was parsed over the run; otherwise it exits 1.

Common causes if no frames arrive, and what each state means:

| State / symptom | Likely cause |
| --- | --- |
| `CONNECTING` stays, never reaches `STREAMING` | Wrong data port, or firmware demo not started (step 4 not done) |
| Connects, then immediately garbled/rejects | Wrong baud rate (data port is 921600, not the CLI's 115200), or connected to the CLI port instead of the data port |
| `STALE` after initially streaming | Chirp config not (re-)sent after a board reset, sensor stopped, or radar physically powered down |
| `DISCONNECTED` / repeated reconnects | Board unplugged, USB power issue, or OS reclaimed/renamed the port |
| `ERROR` | Reconnect attempts exhausted (`reconnect.max_attempts`) or reconnect disabled; check the printed reason |
| `frames_rejected` growing | Corrupted bytes, unsupported target-record length, or wrong firmware profile (`adapter.firmware_profile`) |
| every frame rejected as `BAD_PACKET_ALIGNMENT` | The firmware does not pad packets to the 32-byte multiple the people-counting profile declares; confirm the firmware/profile pairing (the alignment is part of the profile, not a config knob) |
| frames rejected as `AMBIGUOUS_TARGET_RECORD` | The target list length matches more than one record layout; pin `adapter.target_record_layout` to the layout the probe reports |

### 8. Start the live Observatory

```powershell
# Windows PowerShell
$env:RADIOWAVE_TI_CONFIG="configs\examples\ti-iwr6843-lab.json"
pnpm run dev
```

```cmd
:: Windows cmd
set RADIOWAVE_TI_CONFIG=configs\examples\ti-iwr6843-lab.json
pnpm run dev
```

```sh
# POSIX
RADIOWAVE_TI_CONFIG=configs/examples/ti-iwr6843-lab.json pnpm run dev
```

Open the Observatory (http://localhost:5173 by default). It shows a LIVE badge and radar status
(STREAMING/STALE/DISCONNECTED/CONNECTING/ERROR). Use Start to call `POST /api/runs/live`, Stop to
call `POST /api/runs/{id}/stop`, and Reconnect to call `POST /api/runs/{id}/reconnect`. A capture
checkbox controls whether the run is also recorded (see step 11). If `RADIOWAVE_TI_CONFIG` is
unset, missing, or invalid, `GET /api/live/status` reports why live mode is unavailable and the
rest of the Observatory (replay/synthetic) keeps working.

### 9. Walk through the field of view

With the run started, walk through the radar's field of view at a few distances and angles.

### 10. Verify the canonical track and axis checks

Confirm on the map that a canonical `P0001`-style track appears and moves as you walk:

- **Walk away from the radar** — the track's world position should move farther along the radar's
  facing direction (the direction implied by the pose's yaw).
- **Walk to the radar's right, then left** (facing the radar from in front of it, or per the
  `lateral_positive` convention in the config) — the track's lateral position should change sign
  accordingly.

If either check is backwards, revisit `adapter.coordinates` (`forward_axis`, `lateral_positive`,
`z_origin`) before trusting any position data from this sensor.

### 11. Enable normalized capture

Either check the capture checkbox before starting the live run in the Observatory, or record
headless without the frontend:

```sh
# Windows
.venv\Scripts\python.exe -m radiowave.cli mmwave ti capture --config configs\examples\ti-iwr6843-lab.json --out data\captures\bringup-01.jsonl --seconds 60

# POSIX
.venv/bin/python -m radiowave.cli mmwave ti capture --config configs/examples/ti-iwr6843-lab.json --out data/captures/bringup-01.jsonl --seconds 60
```

This records the normalized observation stream (format v2), not raw UART bytes; note that `--out` names a single-shot path, so an existing file there is overwritten.

### 12. Stop the session

In the Observatory, press Stop (`POST /api/runs/{id}/stop`); this stops reading, finalizes the
pipeline and closes the capture file while leaving the run readable. For the headless `capture`
command, press Ctrl+C or let `--seconds` elapse.

### 13. Replay the capture without the radar

```sh
python -m radiowave.cli replay data/captures/bringup-01.jsonl --rate 0
```

The recording carries the twin and thresholds used at capture time, so replay does not require the
radar, the serial dependency, or the original config file.

## Manual acceptance test plan

These map to the Phase 3 acceptance criteria and must be exercised against real hardware. Mark
each as **NOT RUN** until performed; do not mark a check as passing from reasoning alone.

- [ ] NOT RUN — A person entering the field of view produces a target and a canonical person track
      is created.
- [ ] NOT RUN — Walking left/right changes the track's lateral (world) coordinate in the expected
      direction per the configured `lateral_positive` convention.
- [ ] NOT RUN — Walking forward/back changes the track's coordinate along the radar's facing axis
      as expected (verifies `forward_axis`).
- [ ] NOT RUN — The track disappears after the person leaves the field of view, within roughly
      `observation.stale_after_s` plus fusion's own track-timeout behavior.
- [ ] NOT RUN — Re-entry after leaving is treated as re-acquisition (a new or continued canonical
      track), not a crash or a stuck stale track.
- [ ] NOT RUN — The Observatory shows: a healthy radar status badge while streaming, a person
      marker on the map, a trajectory trail, an uncertainty indicator, and a velocity indicator.
- [ ] NOT RUN — Physically disconnecting the USB cable and reconnecting it does not crash the API;
      the session reconnects (or reports ERROR after exhausting attempts) and streaming can resume.
- [ ] NOT RUN — A capture recorded during a live session (step 11) replays deterministically (step
      13) and produces the same normalized observations/tracks on repeated replay.

## Data handling

Raw and normalized captures belong under `data/captures/`, which is git-ignored. Never commit
captures, and never capture real customer/biometric/payment data — lab/synthetic data only, per
`README.md` and `CLAUDE.md`.

The host receive time is stamped once per serial read, so several frames coalesced into one read share a single timestamp; inter-frame timing at high frame rates is therefore flattened, which is deterministic and documented as a Phase 3 limitation.
