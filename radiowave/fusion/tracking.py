"""Canonical person and item track maintenance.

Person tracks: sensor-native track ids are only continuity hints. A canonical
``track_id`` (``P0001``...) is assigned here, may be fed by several sensors and
survives short dropouts through gated re-acquisition.

Item tracks: keyed by EPC; the RFID location estimate is smoothed with an EMA at
the adapter's stated accuracy. No motion is inferred from stale positions.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from radiowave.contracts.geometry import SpatialUncertainty, Vector3D, Velocity, WorldCoordinate
from radiowave.contracts.observations import NATIVE_TRACK_KEY, ItemObservation, PersonObservation
from radiowave.contracts.store import EPC
from radiowave.contracts.tracks import (
    ItemState,
    ItemTrack,
    PersonTrack,
    PersonTrackState,
    TrackPoint,
)
from radiowave.fusion.config import ItemTrackingConfig, PersonTrackingConfig


def _seconds(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


def _mean_position(points: list[TrackPoint]) -> Vector3D:
    total = Vector3D()
    for point in points:
        total = total + point.coordinate.as_vector()
    return total.scale(1.0 / len(points))


def _mean_time(points: list[TrackPoint]) -> datetime:
    first = points[0].timestamp
    offsets = [_seconds(p.timestamp, first) for p in points]
    return first + timedelta(seconds=sum(offsets) / len(offsets))


@dataclass
class PersonState:
    track_id: str
    created_at: datetime
    updated_at: datetime
    position: WorldCoordinate
    velocity: Velocity
    uncertainty: SpatialUncertainty
    confidence: float
    state: PersonTrackState = PersonTrackState.ACTIVE
    observation_count: int = 0
    sensor_last_update: dict[str, datetime] = field(default_factory=dict)
    history: deque[TrackPoint] = field(default_factory=deque)
    session_id: str | None = None

    def position_at(self, when: datetime) -> WorldCoordinate | None:
        """Closest historical position to ``when`` (None if track started later)."""
        if not self.history or self.history[0].timestamp > when:
            return None
        best = min(self.history, key=lambda p: abs(_seconds(p.timestamp, when)))
        return best.coordinate

    def predicted_position(
        self, when: datetime, max_horizon_s: float | None = None
    ) -> WorldCoordinate:
        """Constant-velocity extrapolation, optionally capped at ``max_horizon_s``."""
        dt = max(0.0, _seconds(when, self.updated_at))
        if max_horizon_s is not None:
            dt = min(dt, max_horizon_s)
        return self.position.displaced(self.velocity.as_vector().scale(dt))

    def snapshot(self) -> PersonTrack:
        return PersonTrack(
            track_id=self.track_id,
            state=self.state,
            created_at=self.created_at,
            updated_at=self.updated_at,
            position=self.position,
            velocity=self.velocity,
            uncertainty=self.uncertainty,
            confidence=self.confidence,
            observation_count=self.observation_count,
            contributing_sensor_ids=sorted(self.sensor_last_update),
            session_id=self.session_id,
            history=list(self.history),
        )


class PersonTrackManager:
    def __init__(self, config: PersonTrackingConfig) -> None:
        self._config = config
        self._tracks: dict[str, PersonState] = {}
        self._native_map: dict[tuple[str, str], str] = {}
        self._counter = 0
        self.rejected_low_confidence = 0

    @property
    def active(self) -> list[PersonState]:
        return [t for t in self._tracks.values() if t.state == PersonTrackState.ACTIVE]

    @property
    def all(self) -> list[PersonState]:
        return list(self._tracks.values())

    def get(self, track_id: str) -> PersonState | None:
        return self._tracks.get(track_id)

    def snapshots(self) -> list[PersonTrack]:
        return [t.snapshot() for t in self._tracks.values()]

    def ingest(self, observation: PersonObservation) -> PersonState | None:
        """Update or create the canonical track; returns None for a rejected sample."""
        if observation.confidence < self._config.min_observation_confidence:
            self.rejected_low_confidence += 1
            return None
        native_hint = observation.metadata.get(NATIVE_TRACK_KEY)
        key = (observation.sensor_id, str(native_hint)) if native_hint is not None else None
        track = self._resolve(key, observation)
        self._update(track, observation)
        return track

    def _resolve(self, key: tuple[str, str] | None, observation: PersonObservation) -> PersonState:
        # A native track id is only a continuity hint: a recycled id must still pass the
        # same spatial gate as a fresh one, otherwise a different shopper far away would
        # inherit the previous shopper's canonical identity (and cart).
        if key is not None:
            mapped = self._native_map.get(key)
            if mapped is not None:
                track = self._tracks[mapped]
                if (
                    track.state != PersonTrackState.ENDED
                    and self._gate_radius(track, observation, continuity=True) is not None
                ):
                    return track
                del self._native_map[key]
        # Without a hint, consecutive samples from one sensor must still continue the
        # same track, so the same-sensor exclusion does not apply.
        match = self._gate(observation, allow_same_sensor=key is None)
        if match is not None:
            if key is not None:
                self._native_map[key] = match.track_id
            return match
        self._counter += 1
        track = PersonState(
            track_id=f"P{self._counter:04d}",
            created_at=observation.timestamp,
            updated_at=observation.timestamp,
            position=observation.coordinate,
            velocity=observation.velocity or Velocity(),
            uncertainty=observation.uncertainty,
            confidence=observation.confidence,
        )
        self._tracks[track.track_id] = track
        if key is not None:
            self._native_map[key] = track.track_id
        return track

    def _gate_radius(
        self, track: PersonState, observation: PersonObservation, continuity: bool = False
    ) -> float | None:
        """Distance from the track's prediction if it is inside the gate, else None.

        ``continuity`` is used for a sample carrying the native id already bound to the
        track: the gate grows with the time since the last sample (frame gaps, turns),
        instead of the cross-sensor merge radius meant for simultaneous observations.
        """
        cfg = self._config
        now = observation.timestamp
        silent_for = max(0.0, _seconds(now, track.updated_at))
        if track.state != PersonTrackState.ACTIVE and silent_for > cfg.reacquire_window_s:
            return None
        if continuity or track.state != PersonTrackState.ACTIVE:
            base = cfg.merge_radius_m if continuity else cfg.reacquire_base_radius_m
            radius = base + cfg.reacquire_growth_m_per_s * silent_for
        else:
            radius = cfg.merge_radius_m
        distance = track.predicted_position(now).horizontal_distance_to(observation.coordinate)
        return distance if distance <= radius else None

    def _gate(
        self, observation: PersonObservation, allow_same_sensor: bool = False
    ) -> PersonState | None:
        """Find an existing canonical track this observation most likely belongs to."""
        cfg = self._config
        now = observation.timestamp
        best: PersonState | None = None
        best_distance = float("inf")
        for track in self._tracks.values():
            if track.state == PersonTrackState.ENDED:
                continue
            last_same_sensor = track.sensor_last_update.get(observation.sensor_id)
            if last_same_sensor is not None:
                if allow_same_sensor and last_same_sensor >= now:
                    continue  # already claimed by another hint-less sample of this frame
                if not allow_same_sensor and (
                    _seconds(now, last_same_sensor) < cfg.same_sensor_exclusion_s
                ):
                    continue  # this sensor already reports a different native track here
            distance = self._gate_radius(track, observation)
            if distance is not None and distance < best_distance:
                best, best_distance = track, distance
        return best

    def _update(self, track: PersonState, observation: PersonObservation) -> None:
        cfg = self._config
        if observation.timestamp < track.updated_at:
            return  # out-of-order sample; keep the newer state
        track.state = PersonTrackState.ACTIVE
        track.position = observation.coordinate
        track.uncertainty = observation.uncertainty
        track.confidence = observation.confidence
        track.updated_at = observation.timestamp
        track.observation_count += 1
        track.sensor_last_update[observation.sensor_id] = observation.timestamp
        if observation.velocity is not None:
            track.velocity = observation.velocity
        else:
            track.velocity = self._derive_velocity(track, observation)
        track.history.append(
            TrackPoint(
                timestamp=observation.timestamp,
                coordinate=observation.coordinate,
                velocity=track.velocity,
            )
        )
        while len(track.history) > cfg.history_length:
            track.history.popleft()

    def _derive_velocity(self, track: PersonState, observation: PersonObservation) -> Velocity:
        for point in track.history:
            dt = _seconds(observation.timestamp, point.timestamp)
            if 0.0 < dt <= self._config.velocity_window_s:
                delta = observation.coordinate.as_vector() - point.coordinate.as_vector()
                return Velocity.from_vector(delta.scale(1.0 / dt))
        return track.velocity

    def step(self, now: datetime) -> None:
        cfg = self._config
        for track in self._tracks.values():
            if track.state == PersonTrackState.ENDED:
                continue
            silent_for = _seconds(now, track.updated_at)
            if track.state == PersonTrackState.ACTIVE and silent_for > cfg.lost_after_s:
                track.state = PersonTrackState.LOST
            if track.state == PersonTrackState.LOST and silent_for > cfg.end_after_s:
                track.state = PersonTrackState.ENDED
                for key, mapped in list(self._native_map.items()):
                    if mapped == track.track_id:
                        del self._native_map[key]


@dataclass
class ItemTrackState:
    epc: EPC
    gtin: str | None
    home_fixture_id: str | None
    state: ItemState = ItemState.UNKNOWN
    state_since: datetime | None = None
    position: WorldCoordinate | None = None
    uncertainty: SpatialUncertainty | None = None
    zone_id: str | None = None
    rest_position: WorldCoordinate | None = None
    rest_state: ItemState = ItemState.ON_FIXTURE
    movement_start_at: datetime | None = None
    last_seen_at: datetime | None = None
    carrier_track_id: str | None = None
    observation_count: int = 0
    last_localized_at: datetime | None = None
    localized_count: int = 0
    localized_since_reset: int = 0
    continuity_lost: bool = False
    zone_history: deque[tuple[datetime, str | None]] = field(
        default_factory=lambda: deque(maxlen=16)
    )
    reads_beyond_threshold: int = 0
    localized_at_last_evaluation: int = 0
    at_rest_since: datetime | None = None
    attribution_unresolved: bool = False
    carry_announced: bool = False
    history: deque[TrackPoint] = field(default_factory=deque)

    def is_stale(self, now: datetime, stale_after_s: float) -> bool:
        """True when no *localized* read arrived within ``stale_after_s``."""
        return (
            self.last_localized_at is None or _seconds(now, self.last_localized_at) > stale_after_s
        )

    def has_fresh_evidence(self) -> bool:
        """True once at least one localized read arrived since the last evaluation."""
        return self.localized_count != self.localized_at_last_evaluation

    def displacement_from_rest(self) -> float:
        if self.position is None or self.rest_position is None:
            return 0.0
        return self.position.horizontal_distance_to(self.rest_position)

    def _window_halves(self, window_s: float) -> tuple[list[TrackPoint], list[TrackPoint]] | None:
        """Split the trailing window into an older and a newer half (None if not populated)."""
        if len(self.history) < 4:
            return None
        latest = self.history[-1].timestamp
        window = [p for p in self.history if _seconds(latest, p.timestamp) <= window_s]
        if len(window) < 4 or _seconds(latest, window[0].timestamp) < 0.6 * window_s:
            return None
        mid = len(window) // 2
        return window[:mid], window[mid:]

    def velocity(self, window_s: float) -> Velocity:
        """Velocity of the smoothed estimate: newer-half mean minus older-half mean.

        Averaging halves suppresses the estimate noise that a two-point
        difference would amplify at the configured RFID accuracy.
        """
        halves = self._window_halves(window_s)
        if halves is None:
            return Velocity()
        older, newer = halves
        dt = _seconds(_mean_time(newer), _mean_time(older))
        if dt <= 0.0:
            return Velocity()
        delta = _mean_position(newer) - _mean_position(older)
        return Velocity.from_vector(delta.scale(1.0 / dt))

    def rest_displacement(self, window_s: float) -> float | None:
        """Distance between the older-half and newer-half mean positions (None if unknown)."""
        halves = self._window_halves(window_s)
        if halves is None:
            return None
        older, newer = halves
        return (_mean_position(newer) - _mean_position(older)).horizontal_norm

    def position_at(self, when: datetime) -> WorldCoordinate | None:
        if not self.history or self.history[0].timestamp > when:
            return None
        best = min(self.history, key=lambda p: abs(_seconds(p.timestamp, when)))
        return best.coordinate

    def snapshot(self) -> ItemTrack:
        return ItemTrack(
            epc=self.epc,
            gtin=self.gtin,
            state=self.state,
            state_since=self.state_since,
            home_fixture_id=self.home_fixture_id,
            position=self.position,
            uncertainty=self.uncertainty,
            zone_id=self.zone_id,
            rest_position=self.rest_position,
            movement_start_at=self.movement_start_at,
            last_seen_at=self.last_seen_at,
            carrier_track_id=self.carrier_track_id,
            observation_count=self.observation_count,
            history=list(self.history),
        )


class ItemTrackManager:
    def __init__(self, config: ItemTrackingConfig) -> None:
        self._config = config
        self._tracks: dict[EPC, ItemTrackState] = {}
        self.rejected_low_confidence = 0

    @property
    def all(self) -> list[ItemTrackState]:
        return list(self._tracks.values())

    def get(self, epc: EPC) -> ItemTrackState | None:
        return self._tracks.get(epc)

    def snapshots(self) -> list[ItemTrack]:
        return [t.snapshot() for t in self._tracks.values()]

    def register(self, epc: EPC, gtin: str | None, home_fixture_id: str | None) -> ItemTrackState:
        track = self._tracks.get(epc)
        if track is None:
            track = ItemTrackState(epc=epc, gtin=gtin, home_fixture_id=home_fixture_id)
            self._tracks[epc] = track
        return track

    def ingest(self, observation: ItemObservation) -> ItemTrackState | None:
        """Update (or create) the item track; None when the read is too weak to count."""
        cfg = self._config
        if observation.confidence < cfg.min_read_confidence:
            self.rejected_low_confidence += 1
            return None  # too weak to count as identity, freshness or location evidence
        track = self.register(observation.epc, None, None)
        if track.last_seen_at is not None and observation.timestamp < track.last_seen_at:
            return track
        track.last_seen_at = observation.timestamp
        track.observation_count += 1
        track.zone_history.append((observation.timestamp, observation.zone_id))
        if observation.zone_id is not None:
            track.zone_id = observation.zone_id
        if observation.coordinate is None:
            return track  # zone-only read: identity evidence, not location evidence
        gap = (
            _seconds(observation.timestamp, track.last_localized_at)
            if track.last_localized_at is not None
            else None
        )
        track.last_localized_at = observation.timestamp
        track.localized_count += 1
        if gap is not None and gap > cfg.reset_after_s:
            self._break_continuity(track)
        if track.position is None:
            track.position = observation.coordinate
        else:
            a = cfg.smoothing_alpha
            blended = track.position.as_vector().scale(
                1 - a
            ) + observation.coordinate.as_vector().scale(a)
            track.position = WorldCoordinate(x=blended.x, y=blended.y, z=blended.z)
        track.uncertainty = observation.uncertainty
        track.history.append(TrackPoint(timestamp=observation.timestamp, coordinate=track.position))
        while len(track.history) > cfg.history_length:
            track.history.popleft()
        track.localized_since_reset += 1
        if track.rest_position is None and track.localized_since_reset >= cfg.rest_init_reads:
            # Only the rest position is known here; the fusion engine classifies it
            # (ON_FIXTURE vs MISPLACED) against the twin, so state stays UNKNOWN.
            track.rest_position = track.position
        return track

    @staticmethod
    def _break_continuity(track: ItemTrackState) -> None:
        """Localization was lost for too long: whatever happened meanwhile was unobserved.

        The smoother, history and movement counters restart. An item that was at rest
        re-initializes its rest position from fresh reads (the engine re-classifies it),
        so a relocation during the blackout is never mistaken for an observed PICK.
        A CARRIED item stays CARRIED with its committed carrier.
        """
        track.position = None
        track.history.clear()
        track.reads_beyond_threshold = 0
        track.localized_since_reset = 0
        track.at_rest_since = None
        track.continuity_lost = True
        if track.state not in (ItemState.CARRIED, ItemState.EXITED):
            track.rest_position = None
            track.movement_start_at = None
            track.state = ItemState.UNKNOWN


__all__ = ["ItemTrackManager", "ItemTrackState", "PersonState", "PersonTrackManager"]
