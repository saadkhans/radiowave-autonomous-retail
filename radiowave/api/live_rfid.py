"""Operator-driven simulated RFID for LIVE runs: real radar people, button-press items.

This exists so the radar + RFID + fusion + cart path can be exercised with a real
mmWave sensor before any RFID reader is bought. The operator declares what happens to
an item ("picked up", "put back"); this bench turns that declaration into reads from a
VIRTUAL reader -- the same :class:`RfidReaderEmulator` the Virtual Store Lab uses --
and the reads go through the production ``ObservationNormalizer`` into the live
pipeline, on the same ordered clock as the radar.

What it does NOT test, and must never be presented as testing: who took the item.
Fusion's PICK needs a *localized* item position that moves with a shopper (zone-only
reads never leave ON_FIXTURE; see ``fusion/state_machine.py``), and no real reader is
localizing anything here. So after "pick up" the item's position is taken from the
radar person nearest its shelf at that moment -- the attribution is the operator's
input, not fusion's inference. What it does test is the plumbing around it: a live
canonical track, item tracks, PICK/CARRY/EXIT events, the confidence engine, carts,
capture and replay, and the Observatory, all fed by a real radar.

Only RFID sensors the twin marks ``vendor_metadata["simulated"] == "true"`` are
emulated, so a real reader added to the twin later is never impersonated.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from radiowave.adapters.rfid.base import NativeRfidRead
from radiowave.contracts.geometry import WorldCoordinate
from radiowave.contracts.store import SourceType
from radiowave.contracts.tracks import PersonTrack, PersonTrackState
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.simulator.lab.sensors.rfid import (
    RfidAntennaZoneConfig,
    RfidReaderEmulator,
    RfidReaderEmulatorConfig,
    RfidReadNoiseConfig,
    TaggedItemState,
)

SIMULATED_FLAG = "simulated"
#: A pick is refused unless someone is at least this close to the item (metres).
PICK_RADIUS_M = 2.5
#: How long a carried item keeps answering at its carrier's last position after the
#: carrier's track stops being ACTIVE (walked out of view / through the door).
CARRIER_GONE_GRACE_S = 5.0
#: Item height above the floor, on a shelf or in a hand (metres).
ITEM_HEIGHT_M = 1.0


class OperatorRfidError(ValueError):
    """An operator action that cannot be applied (unknown item, nobody nearby, ...)."""


class BenchItemState(StrEnum):
    ON_SHELF = "ON_SHELF"
    CARRIED = "CARRIED"
    GONE = "GONE"


@dataclass(slots=True)
class _Item:
    epc: str
    gtin: str
    home_fixture_id: str
    home: WorldCoordinate
    state: BenchItemState = BenchItemState.ON_SHELF
    carrier_track_id: str | None = None
    position: WorldCoordinate | None = None
    carrier_gone_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BenchItemView:
    epc: str
    gtin: str
    home_fixture_id: str
    state: BenchItemState
    carrier_track_id: str | None


def _fixture_centre(registry: StoreRegistry, fixture_id: str) -> WorldCoordinate:
    b = registry.fixture(fixture_id).bounds
    return WorldCoordinate(
        x=(b.min_x + b.max_x) / 2.0, y=(b.min_y + b.max_y) / 2.0, z=ITEM_HEIGHT_M
    )


def _distance_xy(a: WorldCoordinate, b: WorldCoordinate) -> float:
    return float(((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5)


class OperatorRfidBench:
    """Virtual reader + operator-declared item states for one live run.

    Not thread-safe on its own: every call is made under the owning run's lock.
    """

    def __init__(
        self,
        registry: StoreRegistry,
        *,
        seed: int = 0,
        poll_interval_s: float = 0.1,
        noise: RfidReadNoiseConfig | None = None,
    ) -> None:
        # 10 polls/s (about 5 reads/s at room range with the lab's detection model).
        # Real fixed UHF readers report a tag many times per second; at the lab's 2 Hz
        # the item's movement is noticed seconds after the pick, by when the shopper
        # has left the shelf's zone and fusion's PICK confidence never reaches COMMIT.
        # Measured on the scripted walk in tests/api/test_live_sim_rfid.py.
        self._poll_interval_s = poll_interval_s
        antennas = [
            RfidAntennaZoneConfig(
                sensor_id=s.sensor_id,
                position=s.pose.position,
                pose=s.pose,
                antenna_port=s.vendor_metadata.get("port", "1"),
                max_range_m=float(s.vendor_metadata.get("max_range_m", "6.0")),
            )
            for s in registry.sensors
            if s.modality == SourceType.RFID
            and s.vendor_metadata.get(SIMULATED_FLAG, "").lower() == "true"
        ]
        self._items: dict[str, _Item] = {}
        for item in registry.items:
            fixture_id = registry.home_fixture_id(item.epc)
            if fixture_id is None:
                continue  # an item with no shelf has nowhere to start or be put back
            self._items[item.epc.value] = _Item(
                epc=item.epc.value,
                gtin=item.gtin,
                home_fixture_id=fixture_id,
                home=_fixture_centre(registry, fixture_id),
            )
        self.enabled = bool(antennas) and bool(self._items)
        self._emulator: RfidReaderEmulator | None = None
        if self.enabled:
            # Localization ON, with the lab's honest 0.5 m blur: fusion's state machine
            # only moves an item off its shelf on localized reads (see module docstring).
            noise_cfg = replace(
                noise or RfidReadNoiseConfig(read_rate_hz=1.0 / poll_interval_s),
                localization_enabled=True,
            )
            self._emulator = RfidReaderEmulator(
                RfidReaderEmulatorConfig(antennas=antennas, noise=noise_cfg, seed=seed)
            )
        self._last_poll: datetime | None = None

    # ------------------------------------------------------------ operator API
    def items(self) -> list[BenchItemView]:
        return [
            BenchItemView(i.epc, i.gtin, i.home_fixture_id, i.state, i.carrier_track_id)
            for i in self._items.values()
        ]

    def _item(self, epc: str) -> _Item:
        item = self._items.get(epc)
        if item is None:
            msg = f"unknown item {epc!r}: not in this store's simulated RFID bench"
            raise OperatorRfidError(msg)
        return item

    def pick(self, epc: str, persons: list[PersonTrack]) -> str:
        """Attach the item to the ACTIVE person nearest its shelf; returns their track id."""
        item = self._item(epc)
        if item.state != BenchItemState.ON_SHELF:
            msg = f"item {epc!r} is not on its shelf ({item.state.value})"
            raise OperatorRfidError(msg)
        active = [p for p in persons if p.state == PersonTrackState.ACTIVE]
        if not active:
            msg = "nobody is being tracked by the radar right now"
            raise OperatorRfidError(msg)
        nearest = min(active, key=lambda p: _distance_xy(p.position, item.home))
        distance = _distance_xy(nearest.position, item.home)
        if distance > PICK_RADIUS_M:
            msg = (
                f"nobody is near {item.home_fixture_id}: the closest tracked person "
                f"({nearest.track_id}) is {distance:.1f} m away"
            )
            raise OperatorRfidError(msg)
        item.state = BenchItemState.CARRIED
        item.carrier_track_id = nearest.track_id
        item.position = item.home
        item.carrier_gone_at = None
        return nearest.track_id

    def put_back(self, epc: str) -> None:
        item = self._item(epc)
        item.state = BenchItemState.ON_SHELF
        item.carrier_track_id = None
        item.position = None
        item.carrier_gone_at = None

    # --------------------------------------------------------------- driving
    def reads(self, now: datetime, persons: list[PersonTrack]) -> list[NativeRfidRead]:
        """One poll of the virtual reader at ``now``, if a poll is due.

        ``now`` must be the live run's ordered clock, so every read is stamped no later
        than the advance that follows it.
        """
        if not self.enabled or self._emulator is None:
            return []
        if (
            self._last_poll is not None
            and (now - self._last_poll).total_seconds() < self._poll_interval_s
        ):
            return []
        self._last_poll = now
        by_id = {p.track_id: p for p in persons}
        states: list[TaggedItemState] = []
        for item in self._items.values():
            if item.state == BenchItemState.ON_SHELF:
                states.append(TaggedItemState(item.epc, item.home, None))
                continue
            if item.state == BenchItemState.GONE:
                continue  # left the store: no antenna can see it any more
            carrier = by_id.get(item.carrier_track_id or "")
            if carrier is not None and carrier.state == PersonTrackState.ACTIVE:
                item.position = WorldCoordinate(
                    x=carrier.position.x, y=carrier.position.y, z=ITEM_HEIGHT_M
                )
                item.carrier_gone_at = None
            else:
                if item.carrier_gone_at is None:
                    item.carrier_gone_at = now
                if (now - item.carrier_gone_at).total_seconds() > CARRIER_GONE_GRACE_S:
                    item.state = BenchItemState.GONE
                    continue
            assert item.position is not None
            states.append(TaggedItemState(item.epc, item.position, item.carrier_track_id))
        return self._emulator.reads(states, now)
