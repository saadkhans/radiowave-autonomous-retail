import type {
  Item,
  ObservatoryEvent,
  ObservatoryStore,
  Person,
  RunState,
  ScenarioDetail,
  ScenarioSummary,
  Timeline,
} from "@/types/api";

/** A small synthetic twin: 10 m × 5 m floor, one fixture, entry west, exit east. */
export const STORE: ObservatoryStore = {
  store_id: "test-store",
  name: "Test store",
  units: "m",
  floor: { min_x: 0, min_y: 0, max_x: 10, max_y: 5 },
  zones: [
    { zone_id: "floor", name: "Floor", kind: "SALES_FLOOR", bounds: { min_x: 0, min_y: 0, max_x: 10, max_y: 5 } },
    { zone_id: "entry", name: "Entry", kind: "ENTRY", bounds: { min_x: 0, min_y: 2, max_x: 1, max_y: 3 } },
    { zone_id: "exit", name: "Exit", kind: "EXIT", bounds: { min_x: 9, min_y: 2, max_x: 10, max_y: 3 } },
    { zone_id: "zone-f1", name: "Fixture 1", kind: "FIXTURE", bounds: { min_x: 4, min_y: 3, max_x: 6, max_y: 5 } },
  ],
  fixtures: [{ fixture_id: "F1", zone_id: "zone-f1", name: "Table", bounds: { min_x: 4.5, min_y: 3.5, max_x: 5.5, max_y: 4.5 } }],
  boundaries: [
    { boundary_id: "entry", kind: "ENTRY", bounds: { min_x: 0, min_y: 2, max_x: 1, max_y: 3 } },
    { boundary_id: "exit", kind: "EXIT", bounds: { min_x: 9, min_y: 2, max_x: 10, max_y: 3 } },
  ],
  sensors: [{ sensor_id: "radar-1", modality: "MMWAVE", x: 5, y: 5, z: 2.5, yaw: 0, name: null }],
  products: [{ gtin: "06281234567890", name: "Black shirt", sku: "SHIRT-BLK", home_fixture_id: "F1" }],
  items: [{ epc: "3034F0000000000000A001", short_epc: "00A001", gtin: "06281234567890", product_name: "Black shirt", home_fixture_id: "F1" }],
};

export const SCENARIO_SUMMARY: ScenarioSummary = {
  scenario_id: "01",
  name: "one shopper picks one item",
  description: "A enters, walks to F1, picks shirt A and carries it to the middle of the floor.",
  duration_s: 18,
  seed: 7,
  shopper_count: 1,
  item_count: 1,
  vision_enabled: false,
  radar_dropouts: 0,
  rfid_dropouts: 0,
  ground_truth: [{ t_s: 6, event_type: "PICK", epc: "3034F0000000000000A001", shopper_label: "A", counterpart_label: null }],
};

export const SCENARIO_DETAIL: ScenarioDetail = { ...SCENARIO_SUMMARY, store: STORE };

export function person(overrides: Partial<Person> = {}): Person {
  return {
    track_id: "P0001",
    session_id: "S-P0001",
    state: "ACTIVE",
    x: 5,
    y: 2.5,
    vx: 1,
    vy: 0,
    speed: 1,
    heading_deg: 0,
    confidence: 0.9,
    sigma_m: 0.2,
    observation_count: 10,
    created_s: 0,
    updated_s: 4,
    sensor_ids: ["radar-1"],
    cart_id: null,
    carried_epcs: [],
    trail: [
      { t_s: 3, x: 4, y: 2.5 },
      { t_s: 4, x: 5, y: 2.5 },
    ],
    ...overrides,
  };
}

export function item(overrides: Partial<Item> = {}): Item {
  return {
    epc: "3034F0000000000000A001",
    short_epc: "00A001",
    gtin: "06281234567890",
    product_name: "Black shirt",
    sku: "SHIRT-BLK",
    home_fixture_id: "F1",
    state: "ON_FIXTURE",
    state_since_s: 0,
    x: 5,
    y: 4,
    sigma_m: 0.3,
    zone_id: "zone-f1",
    carrier_track_id: null,
    movement_start_s: null,
    episode_start_s: null,
    last_seen_s: 4,
    observation_count: 8,
    candidates: [],
    decision: null,
    trail: [],
    ...overrides,
  };
}

export function runState(overrides: Partial<RunState> = {}): RunState {
  return {
    run_id: "run-0001",
    revision: 0,
    epoch: 0,
    scenario_id: "01",
    scenario_name: "one shopper picks one item",
    seed: 7,
    time_s: 0,
    duration_s: 18,
    step_interval_s: 0.25,
    steps: 0,
    finished: false,
    observations_cursor: 0,
    observations_total: 100,
    events_total: 0,
    persons: [],
    items: [],
    carts: [],
    unresolved: [],
    sessions: [],
    counters: {
      accepted: 0,
      dropped_duplicates: 0,
      out_of_order: 0,
      rejected_unknown_sensor: 0,
      rejected_low_confidence: 0,
      rejected_foreign_scenario: 0,
      rejected_spatially_inconsistent: 0,
    },
    ...overrides,
  };
}

export function event(overrides: Partial<ObservatoryEvent> = {}): ObservatoryEvent {
  return {
    seq: 0,
    t_s: 0,
    kind: "RETAIL_EVENT",
    label: "PICK",
    epc: "3034F0000000000000A001",
    shopper_track_id: "P0001",
    counterpart_track_id: null,
    confidence: 0.8,
    margin: 1,
    decision: "COMMIT",
    reason: "",
    event_id: "evt-1",
    from_state: null,
    to_state: null,
    ...overrides,
  };
}

export const EVENTS: ObservatoryEvent[] = [
  event({ seq: 0, t_s: 0, kind: "PERSON_TRACK", label: "PERSON_TRACK_CREATED", epc: null, confidence: 0.9, margin: null, decision: null, event_id: null }),
  event({ seq: 1, t_s: 7.25, kind: "ITEM_TRANSITION", label: "INTERACTION_CANDIDATE", shopper_track_id: null, confidence: null, margin: null, decision: null, event_id: null, from_state: "ON_FIXTURE", to_state: "INTERACTION_CANDIDATE" }),
  event({ seq: 2, t_s: 8, label: "PICK", decision: "WAIT", confidence: 0.66, reason: "confidence 0.66 below commit" }),
  event({ seq: 3, t_s: 8.5, label: "PICK", decision: "COMMIT", confidence: 0.78 }),
  event({ seq: 4, t_s: 9.5, label: "CARRY", decision: "COMMIT", confidence: 0.81 }),
  event({ seq: 5, t_s: 12, label: "HANDOFF", shopper_track_id: "P0001", counterpart_track_id: "P0002", decision: "COMMIT", confidence: 0.77 }),
  event({ seq: 6, t_s: 15, label: "EXIT_WITH_ITEM", shopper_track_id: "P0002", decision: "COMMIT", confidence: 0.9 }),
];

export function timeline(overrides: Partial<Timeline> = {}): Timeline {
  return { run_id: "run-0001", time_s: 0, duration_s: 18, markers: [], ground_truth: SCENARIO_SUMMARY.ground_truth, ...overrides };
}

type Handler = (url: URL, init?: RequestInit) => unknown;

interface RunRecord {
  time: number;
  steps: number;
}

/**
 * Minimal fake of the Observatory API behind global fetch. Simulated time only
 * advances through /advance, /step, /seek and /reset, exactly like the server.
 */
export function installFakeApi() {
  const runs = new Map<string, RunRecord>();
  let runSeq = 0;
  const calls: string[] = [];
  const failures = new Set<string>();
  const holdQueues = new Map<string, Array<{ promise: Promise<void>; release: () => void }>>();
  const flags = { exitHold: false, unresolved: false };

  const runIdFromPath = (pathname: string): string => pathname.match(/^\/api\/runs\/([^/]+)/)?.[1] ?? "";
  const recordFor = (runId: string): RunRecord => {
    const existing = runs.get(runId);
    if (existing) return existing;
    const created: RunRecord = { time: 0, steps: 0 };
    runs.set(runId, created);
    return created;
  };

  const state = (runId: string, record: RunRecord): RunState =>
    runState({
      run_id: runId,
      time_s: record.time,
      steps: record.steps,
      finished: record.time >= 18,
      persons: record.time > 0 ? [person({ x: Math.min(1 + record.time, 9), updated_s: record.time })] : [],
      items: [item({ state: record.time >= 8.5 ? "CARRIED" : "ON_FIXTURE", carrier_track_id: record.time >= 8.5 ? "P0001" : null })],
      carts:
        record.time >= 8.5
          ? [
              {
                cart_id: "cart-P0001",
                shopper_track_id: "P0001",
                session_id: "S-P0001",
                status: "OPEN",
                exited_s: null,
                lines: [{ epc: "3034F0000000000000A001", short_epc: "00A001", gtin: "06281234567890", product_name: "Black shirt", added_s: 8.5, final_ownership_candidate: flags.exitHold, exit_event_s: flags.exitHold ? 8.75 : null }],
              },
            ]
          : [],
      unresolved:
        flags.unresolved && record.time >= 8.5
          ? [{ epc: "3034F0000000000000A001", short_epc: "00A001", gtin: "06281234567890", product_name: "Black shirt", reason: "PICK without an attributed shopper", source_event_id: "evt-1", t_s: 8.5 }]
          : [],
    });
  const events = (runId: string, record: RunRecord) => ({
    run_id: runId,
    epoch: 0,
    events: EVENTS.filter((entry) => entry.t_s <= record.time),
    next_seq: 0,
    total: 0,
  });
  const snapshot = (runId: string, record: RunRecord) => ({
    state: state(runId, record),
    events: events(runId, record),
    timeline: timeline({ run_id: runId, time_s: record.time }),
  });

  const routes: Array<[RegExp, string, Handler]> = [
    [/^\/api\/health$/, "GET", () => ({ status: "ok", engine: "foundation-v0", version: "0.1.0" })],
    [/^\/api\/scenarios$/, "GET", () => [SCENARIO_SUMMARY, { ...SCENARIO_SUMMARY, scenario_id: "12", name: "ambiguous two-shopper pickup" }]],
    [/^\/api\/scenarios\/01$/, "GET", () => SCENARIO_DETAIL],
    [/^\/api\/scenarios\/12$/, "GET", () => ({ ...SCENARIO_DETAIL, scenario_id: "12", name: "ambiguous two-shopper pickup", description: "Two shoppers reach for the same shirt." })],
    [
      /^\/api\/runs$/,
      "POST",
      () => {
        runSeq += 1;
        const runId = `run-${String(runSeq).padStart(4, "0")}`;
        const record = recordFor(runId);
        return snapshot(runId, record);
      },
    ],
    [
      /^\/api\/runs\/[^/]+\/reset$/,
      "POST",
      (url) => {
        const runId = runIdFromPath(url.pathname);
        const record = recordFor(runId);
        record.time = 0;
        record.steps = 0;
        return snapshot(runId, record);
      },
    ],
    [
      /^\/api\/runs\/[^/]+\/step$/,
      "POST",
      (url) => {
        const runId = runIdFromPath(url.pathname);
        const record = recordFor(runId);
        record.time = Math.min(record.time + 0.25, 18);
        record.steps += 1;
        return snapshot(runId, record);
      },
    ],
    [
      /^\/api\/runs\/[^/]+\/advance$/,
      "POST",
      (url, init) => {
        const runId = runIdFromPath(url.pathname);
        const record = recordFor(runId);
        const body = JSON.parse(String(init?.body)) as { seconds: number };
        record.time = Math.min(record.time + body.seconds, 18);
        record.steps += 1;
        return snapshot(runId, record);
      },
    ],
    [
      /^\/api\/runs\/[^/]+\/seek$/,
      "POST",
      (url, init) => {
        const runId = runIdFromPath(url.pathname);
        const record = recordFor(runId);
        const body = JSON.parse(String(init?.body)) as { time_s: number };
        record.time = Math.min(body.time_s, 18);
        return snapshot(runId, record);
      },
    ],
    [/^\/api\/runs\/[^/]+\/state$/, "GET", (url) => state(runIdFromPath(url.pathname), recordFor(runIdFromPath(url.pathname)))],
    [/^\/api\/runs\/[^/]+\/events$/, "GET", (url) => events(runIdFromPath(url.pathname), recordFor(runIdFromPath(url.pathname)))],
    [/^\/api\/runs\/[^/]+\/timeline$/, "GET", (url) => timeline({ run_id: runIdFromPath(url.pathname), time_s: recordFor(runIdFromPath(url.pathname)).time })],
    [/^\/api\/runs\/[^/]+\/snapshot$/, "GET", (url) => snapshot(runIdFromPath(url.pathname), recordFor(runIdFromPath(url.pathname)))],
  ];

  const fetchMock = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    const key = `${method} ${url.pathname}`;
    calls.push(key);
    const queue = holdQueues.get(key);
    if (queue && queue.length > 0) {
      const pending = queue.shift()!;
      await pending.promise;
    }
    if (failures.delete(key)) {
      return new Response(JSON.stringify({ detail: "injected failure" }), { status: 500, headers: { "Content-Type": "application/json" } });
    }
    const route = routes.find(([pattern, verb]) => verb === method && pattern.test(url.pathname));
    if (!route) {
      return new Response(JSON.stringify({ detail: `no route ${method} ${url.pathname}` }), { status: 404, headers: { "Content-Type": "application/json" } });
    }
    const body = route[2](url, init);
    return new Response(JSON.stringify(body), { status: method === "POST" && url.pathname === "/api/runs" ? 201 : 200, headers: { "Content-Type": "application/json" } });
  };

  const original = globalThis.fetch;
  globalThis.fetch = fetchMock as typeof fetch;
  return {
    calls,
    time: (runId = "run-0001") => runs.get(runId)?.time ?? 0,
    /** Make the next request matching "METHOD /path" fail with 500. */
    failNext: (route: string) => failures.add(route),
    /**
     * Arm a FIFO hold for the next request(s) matching "METHOD /path": the
     * call is still logged in `calls` immediately, but the response is not
     * computed/returned until the returned release function is invoked.
     * Multiple holds for the same route queue in FIFO order.
     */
    hold: (route: string): (() => void) => {
      let release: () => void = () => {};
      const promise = new Promise<void>((resolve) => {
        release = resolve;
      });
      const queue = holdQueues.get(route) ?? [];
      queue.push({ promise, release });
      holdQueues.set(route, queue);
      return release;
    },
    /** Serve the cart line as an exit hold (candidate + exit event) on an OPEN cart. */
    set exitHold(value: boolean) {
      flags.exitHold = value;
    },
    /** Serve an unresolved cart mutation once the pick would have committed. */
    set unresolved(value: boolean) {
      flags.unresolved = value;
    },
    restore: () => {
      globalThis.fetch = original;
    },
  };
}
