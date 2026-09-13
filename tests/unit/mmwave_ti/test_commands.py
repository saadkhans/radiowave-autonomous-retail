"""CLI: the example config loads, ``mmwave ti probe`` and ``capture`` run on a fixture
stream (no serial port), and their outputs are usable for bring-up and replay."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from radiowave.adapters.mmwave.ti import commands
from radiowave.adapters.mmwave.ti.config import TiLiveConfig
from radiowave.adapters.mmwave.ti.session import TiLiveSession
from radiowave.cli import build_parser, main
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
    assert "id=7" in text
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
