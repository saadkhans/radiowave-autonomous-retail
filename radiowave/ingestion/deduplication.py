"""Idempotent observation intake.

Replayed recordings, retried transports and overlapping readers all produce
duplicate observations. Duplicates are recognised by ``observation_id`` first and
by the full normalized content second, so a re-issued id with the same content
is still dropped while observations that differ in any field are kept.
"""

from __future__ import annotations

import hashlib
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
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.blake2b(canonical.encode("utf-8"), digest_size=16).hexdigest()

    def _keys(self, observation: SensorObservation) -> tuple[str, str]:
        return (f"id:{observation.observation_id}", f"content:{self.content_key(observation)}")

    def is_duplicate(self, observation: SensorObservation) -> bool:
        """Pure peek: True if already seen. Never mutates the cache or the counters."""
        return any(key in self._seen for key in self._keys(observation))

    def commit(self, observation: SensorObservation) -> None:
        """Record the observation as seen and count it accepted. Caller must have already
        confirmed (via ``is_duplicate``) that it is new."""
        for key in self._keys(observation):
            self._seen[key] = None
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        self.accepted += 1

    def accept(self, observation: SensorObservation) -> bool:
        """Return True if the observation is new; False (and drop) if seen before."""
        if self.is_duplicate(observation):
            self.dropped += 1
            return False
        self.commit(observation)
        return True
