"""Clocks and pacing for replay.

* :class:`SimulatedClock` - time only moves when told to; used by tests and the pipeline.
* :class:`ReplayPacer` - converts recording timestamps into wall-clock waits at a
  configurable rate (``0`` = as fast as possible, ``1`` = real time, ``10`` = 10x).
  The sleep function is injected so tests never actually sleep.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SimulatedClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            msg = "SimulatedClock requires a timezone-aware start"
            raise ValueError(msg)
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        if seconds < 0:
            msg = "SimulatedClock cannot move backwards"
            raise ValueError(msg)
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, when: datetime) -> None:
        if when < self._now:
            msg = "SimulatedClock cannot move backwards"
            raise ValueError(msg)
        self._now = when.astimezone(UTC)


class ReplayPacer:
    """Waits between entries so that playback runs at ``rate`` times recorded speed."""

    def __init__(
        self,
        rate: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        max_wait_s: float = 60.0,
    ) -> None:
        if rate < 0:
            msg = "rate must be >= 0 (0 means no pacing)"
            raise ValueError(msg)
        self.rate = rate
        self._sleep = sleep
        self._max_wait_s = max_wait_s
        self._previous: datetime | None = None
        self.total_waited_s = 0.0

    def wait_for(self, timestamp: datetime) -> float:
        """Sleep as needed before emitting an entry stamped ``timestamp``.

        Returns the seconds waited.
        """
        waited = 0.0
        if self.rate > 0 and self._previous is not None:
            gap = (timestamp - self._previous).total_seconds()
            if gap > 0:
                waited = min(gap / self.rate, self._max_wait_s)
                self._sleep(waited)
        self._previous = timestamp
        self.total_waited_s += waited
        return waited
