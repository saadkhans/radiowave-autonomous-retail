"""Tests for the virtual store lab's RFID reader emulator.

Covers: real-contract validation + clean normalization, EPC identity
preservation across a shared GTIN, determinism, each named fault mode, the
default no-position-estimate stance (and the opt-in coarse estimate), and
carried-item co-motion evidence without carrier-id leakage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from radiowave.adapters.rfid.base import NativeRfidRead
from radiowave.contracts.geometry import WorldCoordinate
from radiowave.contracts.store import Box2D, Sensor, SensorPose, SourceType, Store
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.ingestion.deduplication import ObservationDeduplicator
from radiowave.ingestion.normalization import ObservationNormalizer
from radiowave.simulator.lab.sensors.rfid import (
    RfidAntennaZoneConfig,
    RfidReaderEmulator,
    RfidReaderEmulatorConfig,
    RfidReadNoiseConfig,
    TaggedItemState,
)
from radiowave.simulator.lab.store import (
    EPCS_RACK_A,
    GTIN_TSHIRT_BLACK_M,
    RFID_RACK_A_ID,
    build_virtual_lab_store,
)

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
SENSOR_ID = "rfid-lab-01"


def _tick(t: float) -> datetime:
    return T0 + timedelta(seconds=t)


def _store() -> Store:
    """A minimal single-antenna store for isolated fault-model tests."""
    return Store(
        store_id="rfid-lab-test",
        name="RFID lab test store",
        floor_bounds=Box2D(min_x=-10.0, min_y=-10.0, max_x=10.0, max_y=10.0),
        sensors=[
            Sensor(
                sensor_id=SENSOR_ID,
                modality=SourceType.RFID,
                pose=SensorPose(position=WorldCoordinate(x=0.0, y=0.0, z=2.6), pitch=-1.5708),
            )
        ],
    )


def _antenna(
    *, max_range_m: float = 3.0, position: WorldCoordinate | None = None
) -> RfidAntennaZoneConfig:
    return RfidAntennaZoneConfig(
        sensor_id=SENSOR_ID,
        position=position or WorldCoordinate(x=0.0, y=0.0, z=2.6),
        max_range_m=max_range_m,
    )


def _item(
    epc: str = "3034F1A0000000000001", *, x: float = 1.0, carrier_id: str | None = None
) -> TaggedItemState:
    return TaggedItemState(
        epc=epc, position=WorldCoordinate(x=x, y=0.0, z=1.0), carrier_id=carrier_id
    )


_ALWAYS_READ = RfidReadNoiseConfig(base_read_probability=1.0, min_read_probability=1.0)


# ------------------------------------------------------------- contract/normalization


def test_reads_validate_as_real_native_rfid_read_and_normalize_cleanly() -> None:
    registry = StoreRegistry(build_virtual_lab_store())
    antenna_pose = registry.sensor(RFID_RACK_A_ID).pose.position
    antenna = RfidAntennaZoneConfig(
        sensor_id=RFID_RACK_A_ID, position=antenna_pose, max_range_m=4.0
    )
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[antenna], noise=_ALWAYS_READ, seed=1)
    )
    item = TaggedItemState(
        epc=EPCS_RACK_A[0],
        position=WorldCoordinate(x=antenna_pose.x, y=antenna_pose.y, z=1.0),
        carrier_id=None,
    )

    reads = emulator.reads([item], T0)

    assert len(reads) == 1
    read = reads[0]
    assert isinstance(read, NativeRfidRead)
    # Round-trips through pydantic validation unchanged -- a real contract instance.
    assert NativeRfidRead.model_validate(read.model_dump()) == read

    observation = ObservationNormalizer(registry, "rfid-lab-test").item(read)
    assert observation.epc.value == EPCS_RACK_A[0]
    assert observation.sensor_id == RFID_RACK_A_ID


def test_shared_gtin_epcs_never_collapse_into_one_identity() -> None:
    """The store's whole reason to exist: EPCS_RACK_A[0] and [1] share a GTIN but
    are distinct physical items, so their reads/observations must stay distinct.
    """
    registry = StoreRegistry(build_virtual_lab_store())
    antenna_pose = registry.sensor(RFID_RACK_A_ID).pose.position
    antenna = RfidAntennaZoneConfig(
        sensor_id=RFID_RACK_A_ID, position=antenna_pose, max_range_m=4.0
    )
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[antenna], noise=_ALWAYS_READ, seed=2)
    )
    epc_a, epc_b = EPCS_RACK_A[0], EPCS_RACK_A[1]
    assert registry.item(registry.store.items[0].epc)  # sanity: registry is wired
    items = [
        TaggedItemState(
            epc=epc_a,
            position=WorldCoordinate(x=antenna_pose.x, y=antenna_pose.y, z=1.0),
            carrier_id=None,
        ),
        TaggedItemState(
            epc=epc_b,
            position=WorldCoordinate(x=antenna_pose.x, y=antenna_pose.y, z=1.0),
            carrier_id=None,
        ),
    ]

    reads = emulator.reads(items, T0)
    epcs_read = {r.epc_hex for r in reads}
    assert epcs_read == {epc_a, epc_b}

    normalizer = ObservationNormalizer(registry)
    observations = [normalizer.item(r) for r in reads]
    epcs_observed = {o.epc.value for o in observations}
    assert epcs_observed == {epc_a, epc_b}
    assert len(epcs_observed) == 2  # never collapsed into one identity

    # Both really do share one GTIN -- that's what makes the distinctness above matter.
    from radiowave.contracts.store import EPC

    gtin_a = registry.item(EPC(value=epc_a)).gtin
    gtin_b = registry.item(EPC(value=epc_b)).gtin
    assert gtin_a == gtin_b == GTIN_TSHIRT_BLACK_M


# ----------------------------------------------------------------------- determinism


def test_same_seed_produces_identical_reads() -> None:
    antenna = _antenna()
    item = _item()
    emulator_a = RfidReaderEmulator(RfidReaderEmulatorConfig(antennas=[antenna], seed=7))
    emulator_b = RfidReaderEmulator(RfidReaderEmulatorConfig(antennas=[antenna], seed=7))

    reads_a = [emulator_a.reads([item], _tick(t)) for t in range(5)]
    reads_b = [emulator_b.reads([item], _tick(t)) for t in range(5)]

    assert reads_a == reads_b


def test_different_seed_produces_different_reads() -> None:
    antenna = _antenna()
    item = _item()
    emulator_a = RfidReaderEmulator(RfidReaderEmulatorConfig(antennas=[antenna], seed=7))
    emulator_b = RfidReaderEmulator(RfidReaderEmulatorConfig(antennas=[antenna], seed=8))

    reads_a = [emulator_a.reads([item], _tick(t)) for t in range(5)]
    reads_b = [emulator_b.reads([item], _tick(t)) for t in range(5)]

    assert reads_a != reads_b


# ------------------------------------------------------------------------- estimate


def test_default_config_emits_no_position_estimate() -> None:
    antenna = _antenna()
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[antenna], noise=_ALWAYS_READ, seed=1)
    )

    reads = emulator.reads([_item()], T0)

    assert len(reads) == 1
    assert reads[0].estimate is None
    assert reads[0].estimate_sigma_m is None


def test_opt_in_estimate_is_coarse_and_normalizes_to_the_antenna_position() -> None:
    registry = StoreRegistry(build_virtual_lab_store())
    antenna_pose = registry.sensor(RFID_RACK_A_ID).pose.position
    noise = RfidReadNoiseConfig(
        base_read_probability=1.0,
        min_read_probability=1.0,
        estimate_enabled=True,
        estimate_sigma_m=1.5,
    )
    antenna = RfidAntennaZoneConfig(
        sensor_id=RFID_RACK_A_ID, position=antenna_pose, max_range_m=4.0
    )
    emulator = RfidReaderEmulator(RfidReaderEmulatorConfig(antennas=[antenna], noise=noise, seed=1))
    item = TaggedItemState(
        epc=EPCS_RACK_A[0],
        position=WorldCoordinate(x=antenna_pose.x, y=antenna_pose.y, z=1.0),
        carrier_id=None,
    )

    read = emulator.reads([item], T0)[0]
    assert read.estimate is not None
    assert read.estimate_sigma_m == 1.5

    observation = ObservationNormalizer(registry).item(read)
    assert observation.coordinate is not None
    assert observation.coordinate.x == antenna_pose.x
    assert observation.coordinate.y == antenna_pose.y
    assert observation.uncertainty is not None
    assert observation.uncertainty.sigma_x == 1.5


# ------------------------------------------------------------------------ fault: dropout


def test_missed_read_probability_produces_a_dropout_gap() -> None:
    noise = RfidReadNoiseConfig(
        base_read_probability=1.0, min_read_probability=1.0, missed_read_probability=1.0
    )
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[_antenna()], noise=noise, seed=2)
    )
    item = _item()

    for t in range(3):
        assert emulator.reads([item], _tick(t)) == []


# ------------------------------------------------------------------ fault: duplicate/burst


def test_duplicate_read_probability_reports_the_same_read_twice() -> None:
    """A duplicate-delivery fault must re-emit the exact same measurement --
    same sequence, RSSI, phase, timestamp, and estimate -- not a freshly,
    independently-noised one.

    NOTE: this test previously asserted ``reads[0].sequence != reads[1].sequence``,
    encoding the (wrong) old behaviour where the "duplicate" was actually a second,
    independently-noised read with its own sequence number. That defeated the
    pipeline's idempotent duplicate-delivery handling (see
    ``test_duplicate_read_normalizes_to_the_same_observation_the_pipeline_dedupes``
    below for the real-dedup-path regression test) and is fixed here.
    """
    noise = RfidReadNoiseConfig(
        base_read_probability=1.0, min_read_probability=1.0, duplicate_read_probability=1.0
    )
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[_antenna()], noise=noise, seed=4)
    )
    item = _item()

    reads = emulator.reads([item], T0)

    assert len(reads) == 2
    assert reads[0].epc_hex == reads[1].epc_hex == item.epc
    # Verbatim redelivery: the exact same measurement, not a re-noised one.
    assert reads[0] == reads[1]
    assert reads[0].sequence == reads[1].sequence


def test_duplicate_read_normalizes_to_the_same_observation_the_pipeline_dedupes() -> None:
    """The whole point of ``duplicate_read_probability`` is to exercise the
    pipeline's idempotent handling of a duplicate-delivered read: normalizing
    both copies must produce an ``ItemObservation`` the real
    ``ObservationDeduplicator`` recognizes as already-seen (by observation id
    AND by full normalized content) and drops -- not independent evidence
    that inflates confidence.
    """
    registry = StoreRegistry(build_virtual_lab_store())
    antenna_pose = registry.sensor(RFID_RACK_A_ID).pose.position
    antenna = RfidAntennaZoneConfig(
        sensor_id=RFID_RACK_A_ID, position=antenna_pose, max_range_m=4.0
    )
    noise = RfidReadNoiseConfig(
        base_read_probability=1.0, min_read_probability=1.0, duplicate_read_probability=1.0
    )
    emulator = RfidReaderEmulator(RfidReaderEmulatorConfig(antennas=[antenna], noise=noise, seed=4))
    item = TaggedItemState(
        epc=EPCS_RACK_A[0],
        position=WorldCoordinate(x=antenna_pose.x, y=antenna_pose.y, z=1.0),
        carrier_id=None,
    )

    reads = emulator.reads([item], T0)
    assert len(reads) == 2

    normalizer = ObservationNormalizer(registry, "rfid-lab-test")
    dedup = ObservationDeduplicator()
    first_observation = normalizer.item(reads[0])
    second_observation = normalizer.item(reads[1])

    assert dedup.accept(first_observation) is True  # genuinely new evidence
    assert dedup.accept(second_observation) is False  # recognized as a replay, dropped
    assert dedup.accepted == 1
    assert dedup.dropped == 1


def test_burst_probability_produces_extra_reads_of_the_same_tag() -> None:
    noise = RfidReadNoiseConfig(
        base_read_probability=1.0,
        min_read_probability=1.0,
        burst_probability=1.0,
        burst_extra_reads_max=3,
    )
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[_antenna()], noise=noise, seed=5)
    )
    item = _item()

    reads = emulator.reads([item], T0)

    # One base read plus at least one burst-triggered extra.
    assert len(reads) >= 2
    assert all(r.epc_hex == item.epc for r in reads)
    assert len({r.sequence for r in reads}) == len(reads)  # each burst read gets its own sequence


# --------------------------------------------------------------------- fault: antenna bleed


def test_bleed_produces_a_read_from_an_implausible_antenna() -> None:
    antenna = _antenna(max_range_m=3.0)
    noise = RfidReadNoiseConfig(bleed_probability=1.0, bleed_range_multiplier=3.0)
    emulator = RfidReaderEmulator(RfidReaderEmulatorConfig(antennas=[antenna], noise=noise, seed=3))
    far_item = _item(x=5.0)  # well beyond max_range_m=3.0, inside 3x bleed range

    reads = emulator.reads([far_item], T0)

    assert len(reads) == 1
    read = reads[0]
    assert read.sensor_id == SENSOR_ID
    assert read.epc_hex == far_item.epc
    distance = far_item.position.distance_to(antenna.position)
    assert distance > antenna.max_range_m  # confirms this really is an implausible antenna
    assert read.confidence == 0.25  # honestly low, per _confidence(bleed=True)
    assert read.estimate is None  # a bleed read never claims a location


def test_no_bleed_when_item_is_far_beyond_the_bleed_range() -> None:
    antenna = _antenna(max_range_m=3.0)
    noise = RfidReadNoiseConfig(bleed_probability=1.0, bleed_range_multiplier=2.0)
    emulator = RfidReaderEmulator(RfidReaderEmulatorConfig(antennas=[antenna], noise=noise, seed=3))
    very_far_item = _item(x=50.0)

    assert emulator.reads([very_far_item], T0) == []


# ------------------------------------------------------------------- fault: disappearance


def test_disappearance_probability_turns_a_would_be_read_into_a_miss() -> None:
    baseline_noise = RfidReadNoiseConfig(base_read_probability=1.0, min_read_probability=1.0)
    trigger_noise = RfidReadNoiseConfig(
        base_read_probability=1.0,
        min_read_probability=1.0,
        disappearance_probability=1.0,
        disappearance_ticks=2,
    )
    item = _item()
    baseline = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[_antenna()], noise=baseline_noise, seed=9)
    )
    triggered = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[_antenna()], noise=trigger_noise, seed=9)
    )

    # Same seed, same everything else: only the disappearance fault differs.
    assert baseline.reads([item], T0) != []
    assert triggered.reads([item], T0) == []


def test_disappearance_gap_is_temporary_and_recovers() -> None:
    """A seed/probability found (by direct run, see worker notes) to trigger
    exactly one disappearance window of the configured length and then recover,
    demonstrating the window is bounded rather than a permanent miss.
    """
    noise = RfidReadNoiseConfig(
        base_read_probability=1.0,
        min_read_probability=1.0,
        disappearance_probability=0.2,
        disappearance_ticks=3,
    )
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[_antenna()], noise=noise, seed=4)
    )
    item = _item()

    counts = [len(emulator.reads([item], _tick(t))) for t in range(13)]

    assert counts[0:4] == [1, 1, 1, 1]  # reading normally
    assert counts[4:7] == [0, 0, 0]  # disappearance window: exactly disappearance_ticks misses
    assert counts[7:13] == [1, 1, 1, 1, 1, 1]  # recovered, reading normally again


# --------------------------------------------------------------------- fault: reader restart


def test_reader_restart_resets_the_sequence_counter() -> None:
    """Each restart opens a new generation, so the onboard counter restarts
    from a fresh per-generation baseline -- but that baseline must never
    repeat a value already emitted by an earlier generation.

    NOTE: this test previously asserted ``reads[0].sequence == 0`` on every
    call, encoding the (wrong) old behaviour where a restart reset the
    sequence to literal zero every time -- which meant every one of these
    five reads shared the SAME observation id and the pipeline's
    deduplicator would have wrongly dropped four of the five genuinely new
    measurements as replays. Fixed here: sequence values are still "reset"
    (to a per-generation baseline) on every restart, but are guaranteed
    distinct across restarts. See
    ``test_reader_restart_never_lets_a_post_restart_read_collide_with_an_earlier_one``
    below for the real-dedup-path regression test.
    """
    noise = RfidReadNoiseConfig(
        base_read_probability=1.0, min_read_probability=1.0, reader_restart_probability=1.0
    )
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[_antenna()], noise=noise, seed=2)
    )
    item = _item()

    sequences = []
    for t in range(5):
        reads = emulator.reads([item], _tick(t))
        assert len(reads) == 1
        sequences.append(reads[0].sequence)

    # Every call restarts before reading, so each read opens a fresh
    # generation -- but the emitted sequence values must all be distinct,
    # never colliding back to a value used by an earlier restart.
    assert len(set(sequences)) == len(sequences) == 5


def test_reader_restart_never_lets_a_post_restart_read_collide_with_an_earlier_one() -> None:
    """Regression for the reported bug: resetting the onboard sequence
    counter to literal zero on every restart made every post-restart read
    reuse an observation id already emitted earlier in the run, so the real
    ``ObservationDeduplicator`` silently dropped genuinely new measurements
    as replays. Verified against the real normalization + dedup path.
    """
    registry = StoreRegistry(build_virtual_lab_store())
    antenna_pose = registry.sensor(RFID_RACK_A_ID).pose.position
    antenna = RfidAntennaZoneConfig(
        sensor_id=RFID_RACK_A_ID, position=antenna_pose, max_range_m=4.0
    )
    noise = RfidReadNoiseConfig(
        base_read_probability=1.0, min_read_probability=1.0, reader_restart_probability=1.0
    )
    emulator = RfidReaderEmulator(RfidReaderEmulatorConfig(antennas=[antenna], noise=noise, seed=2))
    item = TaggedItemState(
        epc=EPCS_RACK_A[0],
        position=WorldCoordinate(x=antenna_pose.x, y=antenna_pose.y, z=1.0),
        carrier_id=None,
    )

    normalizer = ObservationNormalizer(registry, "rfid-lab-test")
    dedup = ObservationDeduplicator()

    # Every one of these calls restarts before reading (reader_restart_probability=1.0),
    # so every read is itself "the first read after a restart" -- exactly the
    # collision scenario reported. None should be wrongly dropped as a replay.
    for t in range(5):
        read = emulator.reads([item], _tick(t))[0]
        observation = normalizer.item(read)
        assert dedup.accept(observation) is True

    assert dedup.accepted == 5
    assert dedup.dropped == 0


def test_reader_restart_off_lets_sequence_increment_normally() -> None:
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[_antenna()], noise=_ALWAYS_READ, seed=2)
    )
    item = _item()

    sequences = [emulator.reads([item], _tick(t))[0].sequence for t in range(4)]

    assert sequences == [0, 1, 2, 3]


# ---------------------------------------------------------------- fault: out-of-order/delayed


def test_delayed_delivery_produces_genuine_out_of_order_arrival() -> None:
    """Config/seed found (by direct run) to deliver a held-back older read
    alongside a newer live read within the SAME call, in an order where the
    list's timestamps are not monotonically increasing -- real reordering, not
    just a delay.
    """
    noise = RfidReadNoiseConfig(
        base_read_probability=1.0,
        min_read_probability=1.0,
        delayed_delivery_probability=0.5,
        max_delivery_delay_calls=1,
    )
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[_antenna()], noise=noise, seed=1)
    )
    item = _item()

    results = [emulator.reads([item], _tick(t)) for t in range(2)]

    # Call index 1 delivers a live read captured this tick AND a held-back read
    # captured at call 0, in that order -- so the second entry's timestamp is
    # earlier than the first's: genuine out-of-order delivery.
    call1 = results[1]
    assert len(call1) == 2
    assert call1[0].timestamp > call1[1].timestamp
    assert call1[1].timestamp == _tick(0)
    assert call1[0].timestamp == _tick(1)


def test_delayed_read_is_not_delivered_before_its_delay_elapses() -> None:
    noise = RfidReadNoiseConfig(
        base_read_probability=1.0,
        min_read_probability=1.0,
        delayed_delivery_probability=1.0,
        max_delivery_delay_calls=3,
    )
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[_antenna()], noise=noise, seed=1)
    )
    item = _item()

    first_call = emulator.reads([item], T0)
    assert first_call == []  # captured, but held back -- not delivered immediately

    delivered_by = [len(emulator.reads([item], _tick(t))) for t in range(1, 4)]
    assert sum(delivered_by) >= 1  # eventually delivered within the configured delay bound


# ------------------------------------------------------------------------- co-motion


def test_carried_item_reads_reflect_its_position_and_never_leak_carrier_id() -> None:
    carrier_id = "GT-PERSON-007"
    antenna = _antenna(max_range_m=6.0)
    emulator = RfidReaderEmulator(
        RfidReaderEmulatorConfig(antennas=[antenna], noise=_ALWAYS_READ, seed=1)
    )

    # Tick 1: the carried item is close to the antenna (as if just picked up nearby).
    near = TaggedItemState(
        epc="3034F1A0000000000009",
        position=WorldCoordinate(x=0.5, y=0.0, z=1.0),
        carrier_id=carrier_id,
    )
    # Tick 2: the carrier has walked away with it -- position moved, same EPC/carrier.
    far = TaggedItemState(
        epc="3034F1A0000000000009",
        position=WorldCoordinate(x=4.0, y=0.0, z=1.0),
        carrier_id=carrier_id,
    )

    read_near = emulator.reads([near], _tick(0))[0]
    read_far = emulator.reads([far], _tick(1))[0]

    # Co-motion evidence: RSSI tracks the changing distance to the antenna (weaker
    # as the carried item moves farther away), purely from ``position``.
    assert read_far.rssi_dbm < read_near.rssi_dbm

    for read in (read_near, read_far):
        dumped = read.model_dump_json()
        assert carrier_id not in dumped
        assert "carrier" not in dumped.lower()
