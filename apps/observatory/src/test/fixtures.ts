import type {
  Item,
  LiveAvailability,
  LiveStatus,
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

/** A live store twin: one sensor, no fixtures/products/items (mirrors a real live config). */
export const LIVE_STORE: ObservatoryStore = {
  store_id: "live-store",
  name: "Live store",
  units: "m",
  floor: { min_x: 0, min_y: 0, max_x: 10, max_y: 5 },
  zones: [],
  fixtures: [],
  boundaries: [],
  sensors: [{ sensor_id: "radar-1", modality: "MMWAVE", x: 5, y: 5, z: 2.5, yaw: 0, name: "Test radar" }],
  products: [],
  items: [],
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

/** A virtual store lab scenario (GET /sim/scenarios); same shape as ScenarioSummary. */
export const SIM_SCENARIO_SUMMARY: ScenarioSummary = {
  scenario_id: "acceptance_60s",
  name: "Phase-4 acceptance gate",
  description: "3 shoppers, 30 items, 3 racks, 60 s.",
  duration_s: 60,
  seed: 7,
  shopper_count: 3,
  item_count: 30,
  vision_enabled: false,
  radar_dropouts: 0,
  rfid_dropouts: 0,
  ground_truth: [
    { t_s: 6, event_type: "PICK", epc: "3034F1A00000000000000001", shopper_label: "GT-PERSON-001", counterpart_label: null },
    { t_s: 20, event_type: "EXIT_WITH_ITEM", epc: "3034F1A00000000000000001", shopper_label: "GT-PERSON-001", counterpart_label: null },
  ],
};

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
    mode: "REPLAY",
    live: null,
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

export function liveStatus(overrides: Partial<LiveStatus> = {}): LiveStatus {
  return {
    sensor_id: "radar-1",
    sensor_name: "Test radar",
    state: "STREAMING",
    message: null,
    generation: 1,
    frames_received: 0,
    frames_parsed: 0,
    frames_rejected: 0,
    frames_duplicate: 0,
    observations_emitted: 0,
    observations_dropped_overflow: 0,
    reconnect_count: 0,
    last_frame_age_s: 0.1,
    frame_rate_hz: 10,
    observation_rate_hz: 10,
    capture_path: null,
    started_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

export function liveAvailability(overrides: Partial<LiveAvailability> = {}): LiveAvailability {
  return {
    configured: true,
    config_path: "/config/live.json",
    sensor_id: "radar-1",
    sensor_name: "Test radar",
    data_port: "/dev/ttyUSB0",
    serial_support: true,
    active_run_id: null,
    reason: null,
    ...overrides,
  };
}

/** A LIVE run's initial RunState: scenario_id "live", empty timeline ground truth, live status attached. */
export function liveRunState(overrides: Partial<RunState> = {}): RunState {
  return runState({
    run_id: "live-0001",
    mode: "LIVE",
    scenario_id: "live",
    scenario_name: "Test radar",
    seed: 0,
    duration_s: 0,
    observations_total: 0,
    live: liveStatus(),
    ...overrides,
  });
}

type Handler = (url: URL, init?: RequestInit) => unknown;

interface RunRecord {
  time: number;
  steps: number;
}

interface LiveRunRecord {
  time: number;
  generation: number;
  reconnects: number;
  finished: boolean;
  state: LiveStatus["state"];
  /** Present exactly for SIM runs (POST /runs/sim); drives the `simulated*` LiveStatus fields. */
  simulated?: { scenarioId: string; seed: number };
}

/** Thrown by a route handler to produce a non-2xx response (e.g. 409 on start-live). */
class RouteError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
  }
}

/**
 * Minimal fake of the Observatory API behind global fetch. Simulated time only
 * advances through /advance, /step, /seek and /reset, exactly like the server.
 * LIVE runs advance their own wall-clock-style time by one tick per poll of
 * /runs/{id}/snapshot, mirroring how the real server's live edge grows.
 */
export function installFakeApi() {
  const runs = new Map<string, RunRecord>();
  const liveRuns = new Map<string, LiveRunRecord>();
  let runSeq = 0;
  let liveRunSeq = 0;
  let simRunSeq = 0;
  const calls: string[] = [];
  const failures = new Set<string>();
  const holdQueues = new Map<string, Array<{ promise: Promise<void>; release: () => void }>>();
  const flags = { exitHold: false, unresolved: false };
  const live = { availability: liveAvailability() };

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

  // A SIM run keeps a fixed scenario duration (like the real server) rather
  // than growing with elapsed time the way a hardware LIVE run's does.
  const liveState = (runId: string, record: LiveRunRecord): RunState =>
    liveRunState({
      run_id: runId,
      time_s: record.time,
      duration_s: record.simulated ? SIM_SCENARIO_SUMMARY.duration_s : record.time,
      finished: record.finished,
      scenario_id: record.simulated?.scenarioId ?? "live",
      seed: record.simulated?.seed ?? 0,
      observations_total: Math.round(record.time * 10),
      live: liveStatus({
        state: record.state,
        generation: record.generation,
        reconnect_count: record.reconnects,
        frames_received: Math.round(record.time * 10),
        frames_parsed: Math.round(record.time * 10),
        observations_emitted: Math.round(record.time * 10),
        last_frame_age_s: record.finished ? null : 0.1,
        frame_rate_hz: record.finished ? null : 10,
        observation_rate_hz: record.finished ? null : 10,
        simulated: record.simulated !== undefined,
        simulated_scenario_id: record.simulated?.scenarioId ?? null,
        simulated_seed: record.simulated?.seed ?? null,
      }),
    });
  const liveSnapshot = (runId: string, record: LiveRunRecord) => ({
    state: liveState(runId, record),
    events: { run_id: runId, epoch: 0, events: [], next_seq: 0, total: 0 },
    timeline: timeline({
      run_id: runId,
      time_s: record.time,
      duration_s: record.simulated ? SIM_SCENARIO_SUMMARY.duration_s : record.time,
      ground_truth: record.simulated ? SIM_SCENARIO_SUMMARY.ground_truth : [],
    }),
  });
  /** Each poll of a live run's snapshot simulates one wall-clock tick of new frames. */
  const tickLive = (record: LiveRunRecord): LiveRunRecord => {
    if (!record.finished) record.time = Math.round((record.time + 0.25) * 100) / 100;
    return record;
  };

  const routes: Array<[RegExp, string, Handler, number?]> = [
    [/^\/api\/health$/, "GET", () => ({ status: "ok", engine: "foundation-v0", version: "0.1.0" })],
    [/^\/api\/scenarios$/, "GET", () => [SCENARIO_SUMMARY, { ...SCENARIO_SUMMARY, scenario_id: "12", name: "ambiguous two-shopper pickup" }]],
    [/^\/api\/scenarios\/01$/, "GET", () => SCENARIO_DETAIL],
    [/^\/api\/scenarios\/12$/, "GET", () => ({ ...SCENARIO_DETAIL, scenario_id: "12", name: "ambiguous two-shopper pickup", description: "Two shoppers reach for the same shirt." })],
    [/^\/api\/sim\/scenarios$/, "GET", () => [SIM_SCENARIO_SUMMARY]],
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
      /^\/api\/runs\/sim$/,
      "POST",
      (_url, init) => {
        simRunSeq += 1;
        const runId = `sim-${String(simRunSeq).padStart(4, "0")}`;
        const body = init?.body
          ? (JSON.parse(String(init.body)) as { scenario_id?: string | null; seed?: number | null })
          : {};
        const scenarioId = body.scenario_id ?? SIM_SCENARIO_SUMMARY.scenario_id;
        const seed = body.seed ?? SIM_SCENARIO_SUMMARY.seed;
        const record: LiveRunRecord = {
          time: 0,
          generation: 1,
          reconnects: 0,
          finished: false,
          state: "STREAMING",
          simulated: { scenarioId, seed },
        };
        liveRuns.set(runId, record);
        return liveSnapshot(runId, record);
      },
      201,
    ],
    [
      /^\/api\/runs\/[^/]+\/reset$/,
      "POST",
      (url) => {
        const runId = runIdFromPath(url.pathname);
        const liveRecord = liveRuns.get(runId);
        if (liveRecord) {
          // SIM reset rebuilds from the same scenario/seed - bit-identical by
          // design, so only the clock and finished flag move.
          liveRecord.time = 0;
          liveRecord.finished = false;
          liveRecord.state = "STREAMING";
          return liveSnapshot(runId, liveRecord);
        }
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
        const liveRecord = liveRuns.get(runId);
        if (liveRecord) {
          // Mirrors SimRuntime.seconds_per_advance (0.5s) on the real server.
          if (!liveRecord.finished) liveRecord.time = Math.round((liveRecord.time + 0.5) * 100) / 100;
          return liveSnapshot(runId, liveRecord);
        }
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
        const body = JSON.parse(String(init?.body)) as { seconds: number };
        const liveRecord = liveRuns.get(runId);
        if (liveRecord) {
          if (!liveRecord.finished) liveRecord.time = Math.round((liveRecord.time + body.seconds) * 100) / 100;
          return liveSnapshot(runId, liveRecord);
        }
        const record = recordFor(runId);
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
    [
      /^\/api\/runs\/[^/]+\/state$/,
      "GET",
      (url) => {
        const runId = runIdFromPath(url.pathname);
        const liveRecord = liveRuns.get(runId);
        return liveRecord ? liveState(runId, liveRecord) : state(runId, recordFor(runId));
      },
    ],
    [
      /^\/api\/runs\/[^/]+\/events$/,
      "GET",
      (url) => {
        const runId = runIdFromPath(url.pathname);
        if (liveRuns.has(runId)) return { run_id: runId, epoch: 0, events: [], next_seq: 0, total: 0 };
        return events(runId, recordFor(runId));
      },
    ],
    [
      /^\/api\/runs\/[^/]+\/timeline$/,
      "GET",
      (url) => {
        const runId = runIdFromPath(url.pathname);
        const liveRecord = liveRuns.get(runId);
        if (liveRecord) {
          return timeline({ run_id: runId, time_s: liveRecord.time, duration_s: liveRecord.time, ground_truth: [] });
        }
        return timeline({ run_id: runId, time_s: recordFor(runId).time });
      },
    ],
    [
      /^\/api\/runs\/[^/]+\/snapshot$/,
      "GET",
      (url) => {
        const runId = runIdFromPath(url.pathname);
        const liveRecord = liveRuns.get(runId);
        // Every poll of a live run's snapshot simulates one wall-clock tick of new frames.
        return liveRecord ? liveSnapshot(runId, tickLive(liveRecord)) : snapshot(runId, recordFor(runId));
      },
    ],
    [
      /^\/api\/runs\/[^/]+\/store$/,
      "GET",
      (url) => (liveRuns.has(runIdFromPath(url.pathname)) ? LIVE_STORE : STORE),
    ],
    [/^\/api\/live\/status$/, "GET", () => ({ ...live.availability })],
    [
      /^\/api\/runs\/live$/,
      "POST",
      (_url, init) => {
        if (live.availability.reason) throw new RouteError(409, live.availability.reason);
        liveRunSeq += 1;
        const runId = `live-${String(liveRunSeq).padStart(4, "0")}`;
        const body = init?.body ? (JSON.parse(String(init.body)) as { capture?: boolean }) : {};
        const record: LiveRunRecord = { time: 0, generation: 1, reconnects: 0, finished: false, state: "STREAMING" };
        liveRuns.set(runId, record);
        live.availability = {
          ...live.availability,
          active_run_id: runId,
          reason: `live run ${runId} is already using the sensor; stop it first`,
        };
        const snap = liveSnapshot(runId, record);
        if (body.capture) snap.state.live = { ...snap.state.live!, capture_path: `data/captures/${runId}.jsonl` };
        return snap;
      },
      201,
    ],
    [
      /^\/api\/runs\/[^/]+\/stop$/,
      "POST",
      (url) => {
        const runId = runIdFromPath(url.pathname);
        const record = liveRuns.get(runId);
        if (!record) throw new RouteError(409, `run ${runId} is not a LIVE run`);
        record.finished = true;
        record.state = "DISCONNECTED";
        if (live.availability.active_run_id === runId) {
          live.availability = { ...live.availability, active_run_id: null, reason: null };
        }
        return liveSnapshot(runId, record);
      },
    ],
    [
      /^\/api\/runs\/[^/]+\/reconnect$/,
      "POST",
      (url) => {
        const runId = runIdFromPath(url.pathname);
        const record = liveRuns.get(runId);
        if (!record) throw new RouteError(409, `run ${runId} is not a LIVE run`);
        record.generation += 1;
        record.reconnects += 1;
        record.state = "STREAMING";
        record.finished = false;
        return liveSnapshot(runId, record);
      },
    ],
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
    let body: unknown;
    try {
      body = route[2](url, init);
    } catch (error) {
      if (error instanceof RouteError) {
        return new Response(JSON.stringify({ detail: error.detail }), { status: error.status, headers: { "Content-Type": "application/json" } });
      }
      throw error;
    }
    const status = route[3] ?? (method === "POST" && url.pathname === "/api/runs" ? 201 : 200);
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  };

  const original = globalThis.fetch;
  globalThis.fetch = fetchMock as typeof fetch;
  return {
    calls,
    time: (runId = "run-0001") => runs.get(runId)?.time ?? 0,
    /** Elapsed time of a LIVE run, advanced by every /snapshot poll (defaults to the first live run). */
    liveTime: (runId = "live-0001") => liveRuns.get(runId)?.time ?? 0,
    /** Patch the /live/status response, e.g. to set an unavailability `reason` before a test starts a run. */
    setLiveAvailability: (overrides: Partial<LiveAvailability>) => {
      live.availability = { ...live.availability, ...overrides };
    },
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
