import { describe, expect, it } from "vitest";

import { filterEvents, initialState, reducer } from "@/state/store";
import { EVENTS, runState, SCENARIO_DETAIL } from "@/test/fixtures";

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
