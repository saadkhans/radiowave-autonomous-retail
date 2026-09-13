"""Codex P2 finding: a run must not be discoverable before its initial snapshot.

`RunManager.create()` used to register a run and release the manager lock before a
separate `snapshot()` call took the run lock. A concurrent client that discovers or
predicts the sequential id could step/seek/reset the run in that window, so `POST
/runs` could return a non-initial revision despite its atomic-initial-snapshot
contract. `RunManager._build` now captures the snapshot before publishing the run.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from radiowave.api.app import create_app
from radiowave.api.runs import ObservatoryRun, RunManager


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_run_is_not_discoverable_before_its_initial_snapshot_is_captured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = RunManager()
    original_snapshot = ObservatoryRun.snapshot
    entered = threading.Event()
    release = threading.Event()
    calls = {"count": 0}

    def wrapper(self: ObservatoryRun):
        first = calls["count"] == 0
        calls["count"] += 1
        if first:
            entered.set()
            assert release.wait(timeout=5), "release event was never set"
        return original_snapshot(self)

    monkeypatch.setattr(ObservatoryRun, "snapshot", wrapper)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(manager.create_with_snapshot, "01")
        assert entered.wait(timeout=5), "snapshot() was never entered"
        # The next id is predictable (sequential, "run-0001"), but the run must not be
        # discoverable - and therefore not mutable by a second client - until its
        # initial snapshot has actually been captured.
        assert manager.ids() == []
        assert manager.get("run-0001") is None
        release.set()
        snapshot = future.result(timeout=5)

    # `ObservatoryRun.__init__` starts revision/epoch at 0 and then calls `reset()`,
    # which bumps both to 1 for the first (and only) build of the pipeline. So a fresh
    # run's initial revision and epoch are 1, not 0.
    assert snapshot.state.time_s == 0
    assert snapshot.state.steps == 0
    assert snapshot.state.revision == 1
    assert snapshot.state.epoch == 1
    # At t=0 no observation has been ingested yet, so there are no lifecycle rows at all
    # (no PERSON_TRACK_CREATED, no ITEM_TRANSITION) - in particular no RETAIL_EVENT.
    assert snapshot.events.events == []
    assert not any(event.kind == "RETAIL_EVENT" for event in snapshot.events.events)
    assert manager.ids() == ["run-0001"]


def test_post_runs_returns_the_initial_revision_under_concurrent_mutation(
    client: TestClient,
) -> None:
    stop = threading.Event()

    def hammer() -> None:
        deadline = time.monotonic() + 2.0
        while not stop.is_set() and time.monotonic() < deadline:
            for index in range(1, 9):
                # 404s are expected: most of these ids do not exist yet, or ever.
                client.post(f"/api/runs/run-{index:04d}/step")

    hammer_thread = threading.Thread(target=hammer)
    hammer_thread.start()
    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            responses = list(
                pool.map(
                    lambda _: client.post("/api/runs", json={"scenario_id": "01"}),
                    range(5),
                )
            )
    finally:
        stop.set()
        hammer_thread.join(timeout=5)

    for response in responses:
        assert response.status_code == 201, response.text
        state = response.json()["state"]
        assert state["time_s"] == 0
        assert state["steps"] == 0
        assert state["revision"] == 1


def test_create_still_returns_a_run_and_publishes_it() -> None:
    manager = RunManager()
    run = manager.create("01")
    assert isinstance(run, ObservatoryRun)
    assert run.run_id == "run-0001"
    assert manager.get("run-0001") is run
