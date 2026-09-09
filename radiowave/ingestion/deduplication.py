"""Idempotent observation intake.

Replayed recordings, retried transports and overlapping readers all produce
duplicate observations. Duplicates are recognised by ``observation_id`` first and
by a content key (sensor, timestamp, subject) second, so a re-issued id with the
same content is still dropped.
"""

from __future__ import annotations

from collections import OrderedDict

from radiowave.contracts.observations import ItemObservation, SensorObservation


class ObservationDeduplicator:
    def __init__(self, capacity: int = 200_000) -> None:
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.dropped = 0
        self.accepted = 0

    @staticmethod
    def content_key(observation: SensorObservation) -> str:
        subject = observation.epc.value if isinstance(observation, ItemObservation) else ""
        return (
            f"{observation.source_type}|{observation.sensor_id}|"
            f"{observation.timestamp.isoformat()}|{subject}|"
            f"{observation.coordinate.model_dump() if observation.coordinate else ''}"
        )

    def accept(self, observation: SensorObservation) -> bool:
        """Return True if the observation is new; False (and drop) if seen before."""
        keys = (f"id:{observation.observation_id}", f"content:{self.content_key(observation)}")
        if any(key in self._seen for key in keys):
            self.dropped += 1
            return False
        for key in keys:
            self._seen[key] = None
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        self.accepted += 1
        return True
