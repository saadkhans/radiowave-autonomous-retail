"""Hardware bring-up commands for the TI adapter: ``probe`` and headless ``capture``.

Both run the same :class:`TiLiveSession` the Observatory uses, print concise
per-second summaries, and never require the frontend. ``probe`` is for the first
minutes with a new board (are frames arriving? which axes? which native ids?);
``capture`` records the normalized stream to a format-v2 JSONL recording that
``radiowave replay`` can consume without the radar.
"""

from __future__ import annotations

import argparse
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from radiowave.adapters.mmwave.ti.adapter import NATIVE_TI_POSITION_KEY
from radiowave.adapters.mmwave.ti.config import (
    TiAdapterConfig,
    TiLiveConfig,
    TiRawCaptureConfig,
    TiSerialConfig,
)
from radiowave.adapters.mmwave.ti.session import TiLiveSession
from radiowave.contracts.observations import NATIVE_POSITION_KEY, NATIVE_TRACK_KEY
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.pipeline import FoundationPipeline
from radiowave.replay.recorder import JsonlRecorder

Printer = Callable[[str], None]


def load_live_config(args: argparse.Namespace) -> TiLiveConfig:
    """The config file, with command-line overrides for the ports and raw capture."""
    config = TiLiveConfig.load(args.config)
    serial = config.serial
    if getattr(args, "data_port", None):
        serial = TiSerialConfig(**{**serial.model_dump(), "data_port": args.data_port})
    adapter: TiAdapterConfig = config.adapter
    if getattr(args, "raw_capture", None):
        adapter = TiAdapterConfig(
            **{
                **adapter.model_dump(),
                "raw_capture": TiRawCaptureConfig(path=args.raw_capture).model_dump(),
            }
        )
    return TiLiveConfig(store=config.store, serial=serial, adapter=adapter)


def _fmt(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _describe(session: TiLiveSession, observations: int, out: Printer) -> None:
    d = session.diagnostics()
    out(
        f"[{d.state.value:<12}] gen={d.generation} frames={d.frames_parsed} "
        f"rejected={d.frames_rejected} dup={d.frames_duplicate} "
        f"fps={_fmt(d.frame_rate_hz, 1)} obs/s={_fmt(d.observation_rate_hz, 1)} "
        f"age={_fmt(d.last_frame_age_s)}s observations={observations} "
        f"reconnects={d.reconnect_count}"
        + (f"  {d.message}" if d.message else "")
    )


def cmd_probe(args: argparse.Namespace, out: Printer = print) -> int:
    """Connect, print one line per second and every target seen in that second."""
    config = load_live_config(args)
    registry = StoreRegistry(config.store)
    session = TiLiveSession(config, registry)
    sensor = registry.sensor(config.adapter.sensor_id)
    out(
        f"sensor {sensor.sensor_id} ({sensor.name or 'unnamed'}) on {config.serial.data_port} "
        f"@ {config.serial.data_baud_rate} baud, profile {config.adapter.firmware_profile}, "
        f"pose x={sensor.pose.position.x} y={sensor.pose.position.y} "
        f"z={sensor.pose.position.z} yaw={sensor.pose.yaw:.3f} rad"
    )
    out(
        "columns: native id | native TI xyz (m) | sensor-frame xyz (m) | world xyz (m) | "
        "confidence"
    )
    session.start()
    total = 0
    deadline = None if args.seconds <= 0 else time.monotonic() + args.seconds
    try:
        while deadline is None or time.monotonic() < deadline:
            time.sleep(1.0)
            batch = session.drain()
            total += len(batch)
            _describe(session, total, out)
            for observation in batch[-args.max_targets :]:
                native = observation.metadata.get(NATIVE_TI_POSITION_KEY, {})
                local = observation.metadata.get(NATIVE_POSITION_KEY, {})
                coordinate = observation.coordinate
                out(
                    f"  id={observation.metadata.get(NATIVE_TRACK_KEY)!s:<4} "
                    f"ti=({_fmt(native.get('x'))}, {_fmt(native.get('y'))}, "
                    f"{_fmt(native.get('z'))}) "
                    f"sensor=({_fmt(local.get('x'))}, {_fmt(local.get('y'))}, "
                    f"{_fmt(local.get('z'))}) "
                    f"world=({coordinate.x:.2f}, {coordinate.y:.2f}, {coordinate.z:.2f}) "
                    f"conf={observation.confidence:.2f}"
                )
    except KeyboardInterrupt:
        out("interrupted")
    finally:
        diagnostics = session.diagnostics()  # health as it was while probing, before stop()
        session.stop()
    out(f"health: {diagnostics.to_sensor_health(datetime.now(UTC)).model_dump_json()}")
    return 0 if diagnostics.frames_parsed > 0 else 1


def cmd_capture(args: argparse.Namespace, out: Printer = print) -> int:
    """Run the live pipeline headless and record the normalized stream (format v2)."""
    config = load_live_config(args)
    registry = StoreRegistry(config.store)
    path = Path(args.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    recorder = JsonlRecorder(path)
    pipeline = FoundationPipeline(registry, recorder=recorder)
    session = TiLiveSession(config, registry)
    stop = threading.Event()
    session.start()
    accepted = 0
    deadline = None if args.seconds <= 0 else time.monotonic() + args.seconds
    out(f"recording normalized observations to {path} (Ctrl+C to stop)")
    try:
        while not stop.is_set() and (deadline is None or time.monotonic() < deadline):
            time.sleep(0.2)
            for observation in session.drain():
                if pipeline.ingest(observation):
                    accepted += 1
            pipeline.advance_to(datetime.now(UTC))
    except KeyboardInterrupt:
        out("interrupted")
    finally:
        session.stop()
        for observation in session.drain():
            if pipeline.ingest(observation):
                accepted += 1
        result = pipeline.finish(advance=False)
        recorder.close()
    _describe(session, accepted, out)
    out(f"person tracks   : {[t.track_id for t in result.person_tracks]}")
    out(f"recording       : {path}")
    out(f"replay with     : python -m radiowave.cli replay {path}")
    return 0


def add_mmwave_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register ``radiowave mmwave ti probe|capture`` on the top-level subparsers."""
    mmwave = sub.add_parser("mmwave", help="mmWave radar hardware commands")
    vendors = mmwave.add_subparsers(dest="vendor", required=True)
    ti = vendors.add_parser("ti", help="TI IWR6843-class boards")
    actions = ti.add_subparsers(dest="action", required=True)

    def common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--config",
            required=True,
            help="TiLiveConfig JSON (see configs/examples/ti-iwr6843-lab.json)",
        )
        parser.add_argument(
            "--data-port", help="override the data UART, e.g. COM6 or /dev/ttyUSB1"
        )
        parser.add_argument(
            "--raw-capture",
            help="also dump raw UART bytes to this file (parser debugging; keep under "
            "data/captures/)",
        )

    probe = actions.add_parser("probe", help="connect and print frames, targets and health")
    common(probe)
    probe.add_argument(
        "--seconds", type=float, default=15.0, help="how long to probe (0 = until Ctrl+C)"
    )
    probe.add_argument(
        "--max-targets", type=int, default=8, help="targets printed per second at most"
    )
    probe.set_defaults(func=cmd_probe)

    capture = actions.add_parser(
        "capture", help="record the normalized observation stream without the Observatory"
    )
    common(capture)
    capture.add_argument("--out", required=True, help="JSONL recording path (git-ignored dir)")
    capture.add_argument(
        "--seconds", type=float, default=0.0, help="how long to capture (0 = until Ctrl+C)"
    )
    capture.set_defaults(func=cmd_capture)
