"""CLI: the example config loads, ``mmwave ti probe`` and ``capture`` run on a fixture
stream (no serial port), and their outputs are usable for bring-up and replay."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from radiowave.adapters.mmwave.ti import commands
from radiowave.adapters.mmwave.ti.config import TiLiveConfig, TiRawCaptureConfig
from radiowave.adapters.mmwave.ti.session import SessionTiming, TiLiveSession
from radiowave.cli import build_parser, main
from radiowave.contracts.recording import validate_recording
from radiowave.pipeline import FoundationPipeline
from radiowave.replay.reader import JsonlReplaySource
from tests.fixtures.ti_mmwave.builder import build_target_frame
from tests.unit.mmwave_ti.support import HoldOpenStream, live_config

EXAMPLE = Path(__file__).resolve().parents[3] / "configs" / "examples" / "ti-iwr6843-lab.json"


def test_example_config_is_valid_and_uses_placeholder_ports() -> None:
    config = TiLiveConfig.load(EXAMPLE)
    assert config.adapter.sensor_id == "radar-ti-01"
    assert config.serial.data_port in {"COM6", "/dev/ttyUSB1"}  # a placeholder, not a machine
    assert config.adapter.observation.baseline_sigma_m > 0.0
    assert config.adapter.observation.confidence_ceiling < 1.0
    sensor = next(s for s in config.store.sensors if s.sensor_id == "radar-ti-01")
    assert sensor.pose.position.z > 0.0


def test_parser_registers_the_mmwave_commands() -> None:
    args = build_parser().parse_args(
        ["mmwave", "ti", "probe", "--config", "cfg.json", "--data-port", "COM9", "--seconds", "2"]
    )
    assert args.func is commands.cmd_probe
    assert args.data_port == "COM9"
    args = build_parser().parse_args(
        ["mmwave", "ti", "capture", "--config", "cfg.json", "--out", "x.jsonl"]
    )
    assert args.func is commands.cmd_capture


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(live_config().model_dump(mode="json")), encoding="utf-8")
    return path


def _patch_session(monkeypatch: pytest.MonkeyPatch, frames: list[bytes]) -> None:
    """Make every session the commands build read the fixture instead of a serial port."""

    def build(config: TiLiveConfig, registry: Any, **kwargs: Any) -> TiLiveSession:
        return TiLiveSession(
            config, registry, stream_factory=lambda: HoldOpenStream(frames), **kwargs
        )

    monkeypatch.setattr(commands, "TiLiveSession", build)


def test_probe_prints_health_targets_and_world_coordinates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = [
        build_target_frame(n, [{"tid": 7, "x": 1.0, "y": 3.0, "z": 1.0, "confidence": 0.8}])
        for n in range(1, 4)
    ]
    _patch_session(monkeypatch, frames)
    lines: list[str] = []
    code = commands.cmd_probe(
        build_parser().parse_args(
            ["mmwave", "ti", "probe", "--config", str(_config_file(tmp_path)), "--seconds", "1"]
        ),
        out=lines.append,
    )
    assert code == 0
    text = "\n".join(lines)
    assert "frames=3" in text
    assert "ti_id=7" in text  # raw TI id, provenance only (never the continuity key)
    assert "hint=g1:t7" in text  # generation-scoped continuity hint fusion actually uses
    assert "world=(7.00, 3.00, 1.00)" in text  # radar at (6,0) facing north
    assert "sensor=(3.00, -1.00, -1.40)" in text
    assert '"status":"OK"' in text.replace(" ", "")


def test_probe_reports_failure_when_no_frames_arrive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_session(monkeypatch, [b"\x00" * 64])
    lines: list[str] = []
    code = commands.cmd_probe(
        build_parser().parse_args(
            ["mmwave", "ti", "probe", "--config", str(_config_file(tmp_path)), "--seconds", "1"]
        ),
        out=lines.append,
    )
    assert code == 1
    assert "frames=0" in "\n".join(lines)


def test_capture_writes_a_replayable_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = [
        build_target_frame(n, [{"tid": 2, "x": 0.0, "y": 3.0 - 0.1 * n, "z": 1.0}])
        for n in range(1, 6)
    ]
    _patch_session(monkeypatch, frames)
    out_path = tmp_path / "captures" / "walk.jsonl"
    lines: list[str] = []
    code = commands.cmd_capture(
        build_parser().parse_args(
            [
                "mmwave",
                "ti",
                "capture",
                "--config",
                str(_config_file(tmp_path)),
                "--out",
                str(out_path),
                "--seconds",
                "0.6",
            ]
        ),
        out=lines.append,
    )
    assert code == 0
    assert out_path.exists()
    entries = list(JsonlReplaySource(out_path).entries())
    assert entries[-1].kind.value == "RUN_END"
    assert sum(1 for e in entries if e.kind.value == "OBSERVATION") == 5
    assert "person tracks   : ['P0001']" in "\n".join(lines)
    # The ordinary replay command consumes it.
    assert main(["replay", str(out_path)]) == 0


def test_capture_never_uses_the_plain_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1: ``cmd_capture`` must ingest/advance via ``drain_with_clock()`` only.
    ``drain()`` is diagnostics-only (see its docstring); it is never atomic with a
    clock read, so a consumer that used it to advance a pipeline could have an
    observation stamped before the read clock still arrive on the queue afterwards.
    """
    frames = [
        build_target_frame(n, [{"tid": 3, "x": 0.0, "y": 2.0, "z": 1.0}]) for n in range(1, 4)
    ]
    _patch_session(monkeypatch, frames)

    def boom(self: TiLiveSession, max_items: int | None = None) -> list[Any]:
        raise AssertionError("cmd_capture must not call the plain, non-atomic drain()")

    monkeypatch.setattr(TiLiveSession, "drain", boom)
    out_path = tmp_path / "captures" / "no_plain_drain.jsonl"
    lines: list[str] = []
    code = commands.cmd_capture(
        build_parser().parse_args(
            [
                "mmwave",
                "ti",
                "capture",
                "--config",
                str(_config_file(tmp_path)),
                "--out",
                str(out_path),
                "--seconds",
                "0.4",
            ]
        ),
        out=lines.append,
    )
    assert code == 0
    assert out_path.exists()


def test_capture_drains_and_samples_the_clock_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1: ``drain_with_clock()`` drains the queue and reads the session clock in one
    critical section. A wrapper simulates the reader thread enqueuing one more
    observation, stamped at the session clock, right at the moment of the first drain
    (the race the atomic primitive exists to close): that observation must land in the
    very same batch, never be counted out of order, and still be recorded/replayable.
    """
    frames = [
        build_target_frame(n, [{"tid": 9, "x": 0.0, "y": 2.0 - 0.05 * n, "z": 1.0}])
        for n in range(1, 6)
    ]
    _patch_session(monkeypatch, frames)

    pipelines: list[FoundationPipeline] = []
    real_pipeline_cls = commands.FoundationPipeline

    def build_pipeline(*args: Any, **kwargs: Any) -> FoundationPipeline:
        pipeline = real_pipeline_cls(*args, **kwargs)
        pipelines.append(pipeline)
        return pipeline

    monkeypatch.setattr(commands, "FoundationPipeline", build_pipeline)

    real_drain_with_clock = TiLiveSession.drain_with_clock
    injected = {"done": False}

    def racing_drain_with_clock(self: TiLiveSession) -> tuple[list[Any], Any]:
        if not injected["done"]:
            with self._lock:
                sample = self._queue[-1] if self._queue else None
            if sample is not None:
                injected["done"] = True
                extra = sample.model_copy(
                    update={
                        "observation_id": f"{sample.observation_id}-race",
                        "timestamp": self.clock_now(),
                    }
                )
                with self._lock:
                    self._queue.append(extra)
        return real_drain_with_clock(self)

    monkeypatch.setattr(TiLiveSession, "drain_with_clock", racing_drain_with_clock)

    out_path = tmp_path / "captures" / "race.jsonl"
    lines: list[str] = []
    code = commands.cmd_capture(
        build_parser().parse_args(
            [
                "mmwave",
                "ti",
                "capture",
                "--config",
                str(_config_file(tmp_path)),
                "--out",
                str(out_path),
                "--seconds",
                "0.6",
            ]
        ),
        out=lines.append,
    )
    assert code == 0
    assert injected["done"]  # the race was actually exercised, not skipped
    entries = list(JsonlReplaySource(out_path).entries())
    assert sum(1 for e in entries if e.kind.value == "OBSERVATION") == len(frames) + 1
    assert pipelines[0].result().observations_out_of_order == 0
    assert main(["replay", str(out_path)]) == 0


def test_capture_survives_a_host_wall_clock_regression_and_still_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 9/13: the pipeline clock is the session's ordered clock, anchored once
    to a real UTC instant and advanced only from monotonic time afterwards, so a host
    wall-clock regression (NTP correction, DST, an operator adjusting the system clock)
    can never move it backwards. ``cmd_capture`` must therefore never see an
    out-of-order drop or a non-monotonic recording from such a regression, and the
    result must still validate as a v2 recording and replay cleanly."""
    frames = [
        build_target_frame(n, [{"tid": 5, "x": 0.0, "y": 2.0 - 0.05 * n, "z": 1.0}])
        for n in range(1, 6)
    ]

    def regressing_utc_now() -> datetime:
        # The session anchors its clock lazily, on its very first read: stepping the
        # host wall clock backwards an hour right there is the worst case (no prior
        # anchor to be consistent with) and must still never surface as a negative or
        # out-of-order timestamp downstream, since every later value comes from this
        # one anchor plus elapsed monotonic time, never a fresh wall-clock read.
        return datetime.now(UTC) - timedelta(hours=1)

    timing = SessionTiming(
        utc_now=regressing_utc_now,
        monotonic=time.monotonic,
        wait=lambda event, seconds: event.wait(seconds),
    )

    def build(config: TiLiveConfig, registry: Any, **kwargs: Any) -> TiLiveSession:
        return TiLiveSession(
            config,
            registry,
            stream_factory=lambda: HoldOpenStream(frames),
            timing=timing,
            **kwargs,
        )

    monkeypatch.setattr(commands, "TiLiveSession", build)
    out_path = tmp_path / "captures" / "wall_regression.jsonl"
    lines: list[str] = []
    code = commands.cmd_capture(
        build_parser().parse_args(
            [
                "mmwave",
                "ti",
                "capture",
                "--config",
                str(_config_file(tmp_path)),
                "--out",
                str(out_path),
                "--seconds",
                "0.6",
            ]
        ),
        out=lines.append,
    )
    assert code == 0
    entries = list(JsonlReplaySource(out_path).entries())
    list(validate_recording(entries))  # raises on any ordering/version violation
    observation_timestamps = [e.timestamp for e in entries if e.kind.value == "OBSERVATION"]
    assert observation_timestamps == sorted(observation_timestamps)
    assert len(observation_timestamps) == len(frames)
    assert main(["replay", str(out_path)]) == 0


def test_data_port_override_and_raw_capture_flags(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "mmwave",
            "ti",
            "probe",
            "--config",
            str(_config_file(tmp_path)),
            "--data-port",
            "/dev/ttyUSB1",
            "--raw-capture",
            str(tmp_path / "raw.bin"),
        ]
    )
    config = commands.load_live_config(args)
    assert config.serial.data_port == "/dev/ttyUSB1"
    assert config.adapter.raw_capture.path == str(tmp_path / "raw.bin")


def test_raw_capture_override_preserves_configured_safety_limits(tmp_path: Path) -> None:
    """E2: ``--raw-capture`` must set only ``path``, never reset ``max_bytes`` (or any
    other adapter field) to the ``TiRawCaptureConfig`` default."""
    config = live_config(raw_capture=TiRawCaptureConfig(max_bytes=1_048_576))
    config_path = tmp_path / "cfg.json"
    config_path.write_text(json.dumps(config.model_dump(mode="json")), encoding="utf-8")
    raw_path = tmp_path / "raw.bin"

    with_override = commands.load_live_config(
        build_parser().parse_args(
            [
                "mmwave",
                "ti",
                "probe",
                "--config",
                str(config_path),
                "--raw-capture",
                str(raw_path),
            ]
        )
    )
    assert with_override.adapter.raw_capture.path == str(raw_path)
    assert with_override.adapter.raw_capture.max_bytes == 1_048_576

    without_override = commands.load_live_config(
        build_parser().parse_args(["mmwave", "ti", "probe", "--config", str(config_path)])
    )
    assert without_override.adapter.raw_capture.path is None
    assert without_override.adapter.raw_capture.max_bytes == 1_048_576
