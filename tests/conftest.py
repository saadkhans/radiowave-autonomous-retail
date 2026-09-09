from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from radiowave.contracts.store import Store
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.simulator.stores import build_lab_store

T0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


@pytest.fixture(scope="session")
def lab_store() -> Store:
    return build_lab_store()


@pytest.fixture(scope="session")
def registry(lab_store: Store) -> StoreRegistry:
    return StoreRegistry(lab_store)
