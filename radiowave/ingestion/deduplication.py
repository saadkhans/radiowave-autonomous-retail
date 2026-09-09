"""Idempotent observation intake.

Replayed recordings, retried transports and overlapping readers all produce
duplicate observations. Duplicates are recognised by ``observation_id`` first and
by the full normalized content second, so a re-issued id with the same content
is still dropped while observations that differ in any field are kept.
"""

from __future__ import annotations

import json
from collections import OrderedDict

from radiowave.contracts.observations import SensorObservation


class ObservationDeduplicator:
    def __init__(self, capacity: int = 200_000) -> None:
        self._capacity = capacity
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.dropped = 0
        self.accepted = 0

    @staticmethod
    def content_key(observation: SensorObservation) -> str:
        """Every normalized field except the id, so distinct evidence is never collapsed."""
        payload = observation.model_dump(mode="json", exclude={"observation_id"})
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

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
