import { describe, expect, it } from "vitest";

import { filterEvents, initialState, isStaleSnapshot, reducer } from "@/state/store";
import { EVENTS, runState, SCENARIO_DETAIL, timeline } from "@/test/fixtures";
import type { Snapshot } from "@/types/api";

describe("filterEvents", () => {
  it("returns everything for ALL with no selection", () => {
    expect(filterEvents(EVENTS, "ALL", null)).toHaveLength(EVENTS.length);
  });

  it("filters by retail event type", () => {
    const picks = filterEvents(EVENTS, "PICK", null);
    expect(picks.map((event) => event.decision)).toEqual(["WAIT", "COMMIT"]);
    expect(filterEvents(EVENTS, "HANDOFF", null)).toHaveLength(1);
    expect(filterEvents(EVENTS, "PUTBACK", null)).toHaveLength(0);
  });

  it("LIFECYCLE hides retail decisions but keeps track and transition rows", () => {
    const rows = filterEvents(EVENTS, "LIFECYCLE", null);
    expect(rows.map((event) => event.label)).toEqual(["PERSON_TRACK_CREATED", "INTERACTION_CANDIDATE"]);
  });

  it("selection narrows to a shopper including handoff counterparts", () => {
    const rows = filterEvents(EVENTS, "ALL", { kind: "person", id: "P0002" });
    expect(rows.map((event) => event.label)).toEqual(["HANDOFF", "EXIT_WITH_ITEM"]);
  });

  it("selection narrows to an EPC", () => {
    const rows = filterEvents(EVENTS, "ALL", { kind: "item", id: "3034F0000000000000A001" });
    expect(rows.every((event) => event.epc === "3034F0000000000000A001")).toBe(true);
    expect(rows.some((event) => event.label === "PERSON_TRACK_CREATED")).toBe(false);
  });
});

describe("reducer", () => {
  it("selecting a scenario clears any previous run, events and selection", () => {
    const withRun = reducer(
      { ...initialState, run: runState(), events: EVENTS, selection: { kind: "person", id: "P0001" }, playing: true },
      { type: "scenario", scenarioId: "01", scenario: SCENARIO_DETAIL },
    );
    expect(withRun.run).toBeNull();
    expect(withRun.events).toEqual([]);
    expect(withRun.selection).toBeNull();
    expect(withRun.playing).toBe(false);
    expect(withRun.scenario?.store.floor.max_x).toBe(10);
  });

  it("toggles layers independently", () => {
    const next = reducer(initialState, { type: "layer", key: "sensors" });
    expect(next.layers.sensors).toBe(true);
    expect(next.layers.zones).toBe(true);
    expect(reducer(next, { type: "layer", key: "sensors", value: false }).layers.sensors).toBe(false);
  });
});

describe("isStaleSnapshot", () => {
  const snapshot: Snapshot = {
    state: runState({ run_id: "run-0001" }),
    events: { run_id: "run-0001", epoch: 0, events: [], next_seq: 0, total: 0 },
    timeline: timeline({ run_id: "run-0001" }),
  };

  it("is stale when the snapshot's run_id no longer matches the current run", () => {
    expect(isStaleSnapshot("run-0002", 1, snapshot, 1)).toBe(true);
  });

  it("is stale when the run_id matches but a newer generation has started", () => {
    expect(isStaleSnapshot("run-0001", 2, snapshot, 1)).toBe(true);
  });

  it("is not stale when both the run_id and the generation match", () => {
    expect(isStaleSnapshot("run-0001", 1, snapshot, 1)).toBe(false);
  });
});
