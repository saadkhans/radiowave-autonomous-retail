"""World item ground truth -> RAIN UHF RFID reads, as :class:`NativeRfidRead`.

CRITICAL: this module never invents a parallel RFID contract. It emits only
:class:`~radiowave.adapters.rfid.base.NativeRfidRead` -- the exact adapter-native
sample the real :mod:`radiowave.ingestion.normalization` path already knows how
to turn into an :class:`~radiowave.contracts.observations.ItemObservation` (see
``ObservationNormalizer.item``). Building an ``ItemObservation`` here would be
a shortcut around that normalization boundary, so this module does not do it.

A real RFID reader does not measure position: it observes RF (an antenna zone,
RSSI, sometimes phase). Accordingly ``NativeRfidRead.estimate`` defaults to
absent for every read this module produces. A caller may opt into a coarse,
explicitly-labelled SIMULATED estimate (see ``RfidReadNoiseConfig.estimate_enabled``)
that approximates a tag's location as "near this antenna" at a configured,
honest accuracy -- never a real localization algorithm. Real localization is
Phase 5's job.

Vendor neutrality: nothing here is Impinj/Zebra-specific. ``antenna_port`` is
retained purely as vendor-label metadata, exactly as the real adapter contract
already treats it.

Model: several independent logical antenna zones (one per read-point
``Sensor`` the caller wants emulated), each with a world pose and a simple
coverage/gain approximation. Whether a tag is read at a given antenna, this
poll, is a function of distance, that antenna's coverage, and a seeded fault
model (see ``RfidReadNoiseConfig`` for the full, individually documented list).

Seam: the entry point is :meth:`RfidReaderEmulator.reads`, which only depends
on :class:`TaggedItemState` -- a minimal, frozen snapshot of one item's world
position and ground-truth carrier (carried strictly for provenance; it is
never read into an emitted sample). This module does not import
``radiowave.simulator.lab.world``, ``actors``, or ``interactions``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from radiowave.adapters.rfid.base import NativeRfidRead
from radiowave.contracts.geometry import SensorCoordinate, WorldCoordinate

# A new, previously-unused per-modality seed offset, following the convention
# established in radiowave/simulator/generators.py (_RADAR_SEED_OFFSET=101,
# _RFID_SEED_OFFSET=202, _VISION_SEED_OFFSET=303) and continued by
# radiowave/simulator/lab/sensors/ti_radar.py (_TI_RADAR_SEED_OFFSET=404).
# This lab RFID emulator is a distinct sensor from both of those, so it gets
# its own offset rather than reusing one, keeping every RNG stream independent
# even if all are ever seeded from the same scenario seed in one process.
_RFID_LAB_SEED_OFFSET = 505


@dataclass(frozen=True, slots=True)
class TaggedItemState:
    """One physical item's ground truth for one read attempt -- the seam this
    module depends on instead of importing the world/actors/interactions owner.
    """

    epc: str
    position: WorldCoordinate
    """True world position of the physical item."""
    carrier_id: str | None
    """Ground-truth carrier, provenance only -- NEVER encoded into a read."""


@dataclass(frozen=True, slots=True)
class RfidAntennaZoneConfig:
    """One logical antenna/read-point zone: a world pose (from the twin) plus a
    configurable coverage/gain approximation. One of these per read-point
    ``Sensor`` the caller wants this reader to emulate. ``sensor_id`` must
    match a registered RFID ``Sensor`` so a read normalizes through the real
    adapter path (``ObservationNormalizer.item``) in tests/downstream use.
    """

    sensor_id: str
    position: WorldCoordinate
    antenna_port: str = "1"
    """Vendor antenna/port label; metadata only, exactly as the real contract
    treats it -- never used as identity."""
    max_range_m: float = 6.0
    """ASSUMPTION: coverage radius, not a measured antenna gain pattern."""
    tx_gain_db: float = 0.0
    """ASSUMPTION: flat per-antenna RSSI offset, for modeling heterogeneous
    antennas (e.g. a higher-gain exit antenna) without a full gain pattern."""


@dataclass(frozen=True, slots=True)
class RfidReadNoiseConfig:
    """All noise/fault knobs, seeded and explicit. Every field here is an
    ASSUMED provenance value for lab testing (mirrors the stance
    ``TiRadarNoiseConfig`` takes for the mmWave lab sensor) -- not a
    measurement of a real RAIN RFID reader.
    """

    read_rate_hz: float = 2.0
    """ASSUMPTION: reported on every read as the antenna's configured poll
    rate; metadata only -- callers still drive actual polling cadence
    themselves via how often they call :meth:`RfidReaderEmulator.reads`."""
    base_read_probability: float = 0.85
    """ASSUMPTION: read probability at zero range, before range falloff."""
    min_read_probability: float = 0.05
    """ASSUMPTION: floor read probability at the edge of coverage."""
    rssi_sigma_db: float = 2.0
    """ASSUMPTION: 1-sigma Gaussian RSSI noise, dB."""
    phase_noise_rad: float = 0.3
    """ASSUMPTION: 1-sigma Gaussian noise added on top of each read's random
    phase (see ``_phase_rad``); RFID phase is not modeled as a true function
    of distance here -- that needs a tag/cable-length model out of scope for
    Foundation v0."""
    missed_read_probability: float = 0.0
    """ASSUMPTION: per-attempt probability of an independent reader-side miss
    on top of the distance-based detection roll -- produces a dropout gap."""
    duplicate_read_probability: float = 0.0
    """ASSUMPTION: probability a successful read is reported a second time
    verbatim (same RSSI/phase, a fresh sequence number) -- an
    un-deduplicated reader buffer, a common real RAIN RFID nuisance."""
    burst_probability: float = 0.0
    """ASSUMPTION: probability a successful read additionally triggers a burst
    of extra, independently-noisy reads of the same tag within the same
    poll (rapid re-interrogation within one antenna dwell)."""
    burst_extra_reads_max: int = 0
    """ASSUMPTION: upper bound (inclusive) on how many extra reads a
    triggered burst produces; drawn uniformly from ``[1, burst_extra_reads_max]``."""
    bleed_probability: float = 0.0
    """ASSUMPTION: probability an antenna reads a tag beyond its own coverage
    (``max_range_m``) but within ``bleed_range_multiplier * max_range_m`` --
    RF leakage/reflection letting an antenna "see" a tag it should not
    plausibly see."""
    bleed_range_multiplier: float = 2.0
    """ASSUMPTION: how far past ``max_range_m`` a bleed read can still occur."""
    disappearance_probability: float = 0.0
    """ASSUMPTION: per-attempt probability an in-coverage tag starts a
    temporary disappearance window (see ``disappearance_ticks``) instead of
    being read this poll -- orientation nulls/multipath fade, not a real
    departure."""
    disappearance_ticks: int = 0
    """ASSUMPTION: length, in calls to :meth:`RfidReaderEmulator.reads`, of a
    triggered disappearance window; the tag is unreadable at that antenna for
    exactly this many subsequent calls."""
    reader_restart_probability: float = 0.0
    """ASSUMPTION: per-antenna, per-call probability of a deliberate reader
    restart, resetting that antenna's read-sequence counter to zero -- the
    RFID analogue of ``TiRadarEmulator.reset_stream``."""
    delayed_delivery_probability: float = 0.0
    """ASSUMPTION: probability a generated read is held back rather than
    returned immediately (see ``max_delivery_delay_calls``), simulating
    reader/network buffering."""
    max_delivery_delay_calls: int = 0
    """ASSUMPTION: upper bound (inclusive) on how many later calls to
    :meth:`RfidReaderEmulator.reads` a delayed read is held for; drawn
    uniformly from ``[1, max_delivery_delay_calls]``. A read held back and
    then delivered alongside a later call's live reads is what produces
    genuine out-of-order arrival -- see ``RfidReaderEmulator.reads``."""
    estimate_enabled: bool = False
    """Opt-in only (default False): when True, every successful *non-bleed*
    read additionally carries a SIMULATED coarse location estimate at
    ``estimate_sigma_m`` -- see ``_estimate_for``. Bleed reads never carry an
    estimate regardless of this flag: a read already admitted to be outside
    the antenna's plausible coverage should not also claim a location."""
    estimate_sigma_m: float = 1.5
    """ASSUMPTION: honest (deliberately coarse) 1-sigma accuracy claimed for
    the SIMULATED estimate above; never claim finer accuracy than this."""


@dataclass(frozen=True, slots=True)
class RfidReaderEmulatorConfig:
    """Everything one virtual RFID reader needs: its antenna zones and fault model."""

    antennas: Sequence[RfidAntennaZoneConfig]
    noise: RfidReadNoiseConfig = field(default_factory=RfidReadNoiseConfig)
    seed: int = 0


@dataclass(slots=True)
class _PendingRead:
    """A read captured at an earlier call, held back to simulate delivery delay."""

    deliver_at_call: int
    read: NativeRfidRead


def _detection_probability(
    distance_m: float, antenna: RfidAntennaZoneConfig, noise: RfidReadNoiseConfig
) -> float:
    """ASSUMPTION: linear falloff from ``base_read_probability`` at zero range to
    ``min_read_probability`` at the coverage edge, zero beyond it. Mirrors the
    shape (not the exact constants) of the existing
    ``radiowave/simulator/generators.py`` ``RfidGenerator`` formula.
    """
    if distance_m > antenna.max_range_m or antenna.max_range_m <= 0.0:
        return 0.0
    falloff = 1.0 - distance_m / antenna.max_range_m
    return max(noise.min_read_probability, noise.base_read_probability * falloff)


def _rssi_dbm(
    distance_m: float, tx_gain_db: float, rng: np.random.Generator, sigma_db: float
) -> float:
    """ASSUMPTION: a simple log-distance path-loss shape, tuned only to be
    monotonically decreasing with range -- not a calibrated antenna pattern.
    Mirrors the existing ``RfidGenerator`` RSSI formula plus a per-antenna
    gain offset so heterogeneous antennas can be modeled.
    """
    distance = max(distance_m, 0.5)
    rssi = -40.0 - 20.0 * math.log10(distance) + tx_gain_db
    rssi += float(rng.normal(0.0, sigma_db))
    return round(rssi, 2)


def _wrap_phase(phase: float) -> float:
    """Wrap into ``(-pi, pi]``, matching ``NativeRfidRead``'s expected phase range."""
    return (phase + math.pi) % (2.0 * math.pi) - math.pi


def _phase_rad(rng: np.random.Generator, noise_rad: float) -> float:
    """ASSUMPTION: RFID phase is not modeled as a true function of distance here
    (that needs a tag-specific wavelength/cable-length model, out of scope for
    Foundation v0); each read instead gets an independent random phase plus
    configured noise, wrapped into range, so downstream code sees a
    realistically noisy signal without pretending to measure range from phase.
    """
    phase = float(rng.uniform(-math.pi, math.pi)) + float(rng.normal(0.0, noise_rad))
    return _wrap_phase(phase)


def _confidence(distance_m: float, antenna: RfidAntennaZoneConfig, *, bleed: bool) -> float:
    """ASSUMPTION: confidence decays with range within coverage; a bleed read
    (see ``RfidReadNoiseConfig.bleed_probability``) is always reported at a low,
    fixed confidence -- an honest signal that the read is implausible, never
    hidden from a consumer that inspects confidence.
    """
    if bleed:
        return 0.25
    ratio = min(1.0, distance_m / antenna.max_range_m) if antenna.max_range_m > 0.0 else 1.0
    return round(max(0.3, min(0.95, 0.95 - 0.5 * ratio)), 3)


def _estimate_for(
    antenna: RfidAntennaZoneConfig, noise: RfidReadNoiseConfig
) -> tuple[SensorCoordinate | None, float | None]:
    """SIMULATED, coarse, explicitly opt-in only (``estimate_enabled``):
    approximates the item's location as *this antenna's own mounted
    position*, at the configured accuracy. This is NOT a real localization
    algorithm (no trilateration, no RSSI-to-range inversion) -- real
    localization is Phase 5's job.

    ``SensorCoordinate(0, 0, 0)`` in the antenna's own frame always maps,
    through the twin's ``RigidTransform``, back to exactly the antenna's
    mounted world position, regardless of the antenna's yaw/pitch -- so this
    module can produce an honest "near this antenna" estimate without needing
    the transform itself.
    """
    if not noise.estimate_enabled:
        return None, None
    return SensorCoordinate(x=0.0, y=0.0, z=0.0, frame_id=antenna.sensor_id), noise.estimate_sigma_m


class RfidReaderEmulator:
    """Virtual RAIN RFID reader: world item truth -> zero or more
    :class:`NativeRfidRead` per logical antenna zone, per call to :meth:`reads`.

    One instance owns one seeded RNG stream, one read-sequence counter per
    antenna zone, and the fault-model bookkeeping (temporary-disappearance
    windows, the delayed-delivery queue) -- exactly as one physical reader
    connection would own its own read buffer and state, independent of any
    other reader in the store.
    """

    def __init__(self, config: RfidReaderEmulatorConfig) -> None:
        self._config = config
        self._rng = np.random.default_rng(config.seed + _RFID_LAB_SEED_OFFSET)
        self._sequence: dict[str, int] = {a.sensor_id: 0 for a in config.antennas}
        # (sensor_id, epc) -> call index at which the item becomes readable
        # again; used by the temporary-disappearance fault.
        self._disappeared_until: dict[tuple[str, str], int] = {}
        # Reads generated at some earlier call but held back to simulate
        # reader/network transit delay; delivered once ``call_index`` reaches
        # ``deliver_at_call``. See ``reads()`` for how this creates
        # out-of-order arrival.
        self._pending: list[_PendingRead] = []
        self._call_index = 0

    def reads(self, items: Sequence[TaggedItemState], sim_time: datetime) -> list[NativeRfidRead]:
        """One reader poll cycle: every antenna zone independently attempts a
        read of every item, subject to coverage and the configured fault model.

        Returned order is deliberately NOT sorted by timestamp: any reads
        flushed this call from the delayed-delivery queue (captured at an
        earlier ``sim_time``) are appended AFTER this call's live reads, so a
        caller that concatenates successive ``reads()`` results sees genuine
        out-of-order arrival -- exactly as a real reader's buffered
        network/UART delivery would produce.
        """
        cfg = self._config
        rng = self._rng
        call_index = self._call_index
        live: list[NativeRfidRead] = []

        for antenna in cfg.antennas:
            if rng.random() < cfg.noise.reader_restart_probability:
                # A restart clears only this antenna's read-sequence counter,
                # mirroring TiRadarEmulator.reset_stream -- physical item state
                # (owned elsewhere) is untouched.
                self._sequence[antenna.sensor_id] = 0
            for item in items:
                live.extend(self._attempt(antenna, item, sim_time, call_index))

        delivered_now = [p.read for p in self._pending if p.deliver_at_call <= call_index]
        self._pending = [p for p in self._pending if p.deliver_at_call > call_index]

        self._call_index += 1
        return live + delivered_now

    # ------------------------------------------------------------------ per-item

    def _attempt(
        self,
        antenna: RfidAntennaZoneConfig,
        item: TaggedItemState,
        sim_time: datetime,
        call_index: int,
    ) -> list[NativeRfidRead]:
        """One antenna's read attempt of one item this poll: coverage, then the
        fault model, in the order faults are documented on ``RfidReadNoiseConfig``.
        """
        noise = self._config.noise
        rng = self._rng
        key = (antenna.sensor_id, item.epc)

        if self._disappeared_until.get(key, -1) > call_index:
            # Still inside a temporary-disappearance window: a hard miss,
            # independent of every other roll below.
            return []

        distance = item.position.distance_to(antenna.position)
        if distance > antenna.max_range_m:
            return self._maybe_bleed(antenna, item, sim_time, distance, call_index)

        if noise.disappearance_ticks > 0 and rng.random() < noise.disappearance_probability:
            self._disappeared_until[key] = call_index + noise.disappearance_ticks
            return []

        if rng.random() > _detection_probability(distance, antenna, noise):
            return []
        if rng.random() < noise.missed_read_probability:
            # An independent reader-side miss on top of the distance-based
            # detection roll above: a read the signal budget should have
            # supported, lost to reader-side contention rather than weak RF.
            return []

        reads = [self._build_read(antenna, item, sim_time, distance, bleed=False)]

        if rng.random() < noise.duplicate_read_probability:
            reads.append(self._build_read(antenna, item, sim_time, distance, bleed=False))

        if noise.burst_extra_reads_max > 0 and rng.random() < noise.burst_probability:
            extra = int(rng.integers(1, noise.burst_extra_reads_max + 1))
            reads.extend(
                self._build_read(antenna, item, sim_time, distance, bleed=False)
                for _ in range(extra)
            )

        return self._deliver_or_queue(reads, call_index)

    def _maybe_bleed(
        self,
        antenna: RfidAntennaZoneConfig,
        item: TaggedItemState,
        sim_time: datetime,
        distance: float,
        call_index: int,
    ) -> list[NativeRfidRead]:
        """A tag out of ``antenna``'s normal coverage, occasionally still read.

        Antenna bleed (RF leakage/reflections letting an antenna "see" a tag it
        should not plausibly see) is a real, well-known RAIN RFID nuisance, not
        a bug in the coverage model above -- so it is a distinct, separately
        configured roll rather than a wider ``max_range_m``.
        """
        noise = self._config.noise
        if noise.bleed_probability <= 0.0:
            return []
        if distance > antenna.max_range_m * noise.bleed_range_multiplier:
            return []
        if self._rng.random() >= noise.bleed_probability:
            return []
        read = self._build_read(antenna, item, sim_time, distance, bleed=True)
        return self._deliver_or_queue([read], call_index)

    def _deliver_or_queue(
        self, reads: list[NativeRfidRead], call_index: int
    ) -> list[NativeRfidRead]:
        """Split freshly generated reads into "deliver this call" vs "hold for
        delayed delivery" (see ``RfidReadNoiseConfig.delayed_delivery_probability``).
        """
        noise = self._config.noise
        rng = self._rng
        delivered: list[NativeRfidRead] = []
        for read in reads:
            delayed = (
                noise.max_delivery_delay_calls > 0
                and rng.random() < noise.delayed_delivery_probability
            )
            if delayed:
                delay = int(rng.integers(1, noise.max_delivery_delay_calls + 1))
                self._pending.append(_PendingRead(deliver_at_call=call_index + delay, read=read))
            else:
                delivered.append(read)
        return delivered

    def _build_read(
        self,
        antenna: RfidAntennaZoneConfig,
        item: TaggedItemState,
        sim_time: datetime,
        distance: float,
        *,
        bleed: bool,
    ) -> NativeRfidRead:
        """Build one read record. ``item.carrier_id`` is never referenced here --
        co-motion with a carrier is entirely a consequence of ``item.position``
        already reflecting the carry (owned by the world model), never of this
        module encoding carrier identity into a read.
        """
        noise = self._config.noise
        rng = self._rng
        sequence = self._sequence[antenna.sensor_id]
        self._sequence[antenna.sensor_id] = sequence + 1
        estimate, estimate_sigma_m = (None, None) if bleed else _estimate_for(antenna, noise)
        return NativeRfidRead(
            sensor_id=antenna.sensor_id,
            sequence=sequence,
            timestamp=sim_time,
            epc_hex=item.epc,
            antenna_port=antenna.antenna_port,
            rssi_dbm=_rssi_dbm(distance, antenna.tx_gain_db, rng, noise.rssi_sigma_db),
            phase_rad=_phase_rad(rng, noise.phase_noise_rad),
            read_rate_hz=noise.read_rate_hz,
            confidence=_confidence(distance, antenna, bleed=bleed),
            estimate=estimate,
            estimate_sigma_m=estimate_sigma_m,
        )
