"""Regression tests for TI adapter config validators: confidence bounds and the
packet/buffer size relationship in the parser limits."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from radiowave.adapters.mmwave.ti.config import (
    TiLiveConfig,
    TiObservationPolicy,
    TiParserLimitsConfig,
    profile_supports_people_tracking,
)
from tests.unit.mmwave_ti.support import live_config

EXAMPLE_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "configs" / "examples" / "ti-iwr6843-lab.json"
)


# ---------------------------------------------------------------------------
# TiObservationPolicy: confidence values must be strictly below 1.0, and
# baseline_confidence must not exceed confidence_ceiling.
# ---------------------------------------------------------------------------
def test_defaults_are_valid() -> None:
    policy = TiObservationPolicy()
    assert policy.baseline_confidence == 0.6
    assert policy.confidence_ceiling == 0.9


def test_baseline_confidence_of_one_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TiObservationPolicy(baseline_confidence=1.0)


def test_confidence_ceiling_of_one_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TiObservationPolicy(confidence_ceiling=1.0)


def test_confidence_of_0_99_is_accepted() -> None:
    policy = TiObservationPolicy(baseline_confidence=0.99, confidence_ceiling=0.99)
    assert policy.baseline_confidence == 0.99
    assert policy.confidence_ceiling == 0.99


def test_baseline_above_ceiling_is_rejected() -> None:
    with pytest.raises(ValidationError, match="baseline_confidence"):
        TiObservationPolicy(baseline_confidence=0.95, confidence_ceiling=0.9)


# ---------------------------------------------------------------------------
# TiParserLimitsConfig: a packet larger than the buffer could never complete.
# ---------------------------------------------------------------------------
def test_parser_limits_defaults_are_valid() -> None:
    limits = TiParserLimitsConfig()
    assert limits.max_packet_bytes <= limits.max_buffer_bytes


def test_packet_larger_than_buffer_is_rejected() -> None:
    with pytest.raises(ValidationError, match="max_packet_bytes"):
        TiParserLimitsConfig(max_packet_bytes=500_000, max_buffer_bytes=1024)


def test_example_lab_config_still_validates() -> None:
    config = TiLiveConfig.load(EXAMPLE_CONFIG_PATH)
    limits = config.adapter.parser_limits
    assert config.adapter.observation.baseline_confidence < 1.0
    assert config.adapter.observation.confidence_ceiling < 1.0
    assert limits.max_packet_bytes <= limits.max_buffer_bytes


def test_example_lab_config_pins_the_target_record_layout() -> None:
    config = TiLiveConfig.load(EXAMPLE_CONFIG_PATH)
    assert config.adapter.target_record_layout == "3d_v2"


# ---------------------------------------------------------------------------
# TiLiveConfig: only firmware capable of target-list people tracking may back the
# Phase-3 PeopleTracker adapter; ti-oob-sdk3 is point-cloud-only.
# ---------------------------------------------------------------------------
def test_profile_supports_people_tracking_helper() -> None:
    assert profile_supports_people_tracking("ti-3d-people-counting") is True
    assert profile_supports_people_tracking("ti-oob-sdk3") is False


def test_live_config_with_oob_firmware_profile_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ti-oob-sdk3 is point-cloud-only"):
        live_config(firmware_profile="ti-oob-sdk3")


def test_live_config_with_people_counting_firmware_profile_validates() -> None:
    config = live_config(firmware_profile="ti-3d-people-counting")
    assert config.adapter.firmware_profile == "ti-3d-people-counting"
