import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type ReactNode,
} from "react";

import { api, ApiError } from "@/lib/api";
import type { RetailEventType } from "@/lib/format";
import type {
  LiveAvailability,
  ObservatoryEvent,
  ObservatoryStore,
  RunState,
  ScenarioDetail,
  ScenarioSummary,
  Snapshot,
  Timeline,
} from "@/types/api";

export type Selection = { kind: "person"; id: string } | { kind: "item"; id: string } | null;

export type EventFilter = "ALL" | RetailEventType | "LIFECYCLE";

export interface Layers {
  grid: boolean;
  zones: boolean;
  fixtures: boolean;
  sensors: boolean;
  trails: boolean;
  shopperLabels: boolean;
  epcLabels: boolean;
  associations: boolean;
  confidenceLabels: boolean;
}

export const DEFAULT_LAYERS: Layers = {
  grid: true,
  zones: true,
  fixtures: true,
  sensors: false,
  trails: true,
  shopperLabels: true,
  epcLabels: true,
  associations: true,
  confidenceLabels: true,
};

export const SPEEDS = [0.25, 0.5, 1, 2, 5, 10] as const;
/** Wall-clock tick used only to pace requests; simulated time is owned by the API. */
export const TICK_MS = 200;
/** LIVE runs are polled (not advanced) at this interval; the server owns the live edge. */
export const LIVE_POLL_MS = 250;

export interface ObservatoryState {
  health: string | null;
  scenarios: ScenarioSummary[];
  scenarioId: string | null;
  scenario: ScenarioDetail | null;
  run: RunState | null;
  events: ObservatoryEvent[];
  timeline: Timeline | null;
  playing: boolean;
  speed: number;
  busy: boolean;
  error: string | null;
  selection: Selection;
  layers: Layers;
  eventFilter: EventFilter;
  /** Whether a LIVE run can be started now, and with which sensor; refreshed around live actions. */
  liveAvailability: LiveAvailability | null;
  /** The store twin for the active LIVE run (fetched once at start; a live twin may have empty fixtures/products/items). */
  liveStore: ObservatoryStore | null;
}

export const initialState: ObservatoryState = {
  health: null,
  scenarios: [],
  scenarioId: null,
  scenario: null,
  run: null,
  events: [],
  timeline: null,
  playing: false,
  speed: 1,
  busy: false,
  error: null,
  selection: null,
  layers: DEFAULT_LAYERS,
  eventFilter: "ALL",
  liveAvailability: null,
  liveStore: null,
};

export type Action =
  | { type: "health"; health: string | null }
  | { type: "scenarios"; scenarios: ScenarioSummary[] }
  | { type: "scenario"; scenarioId: string; scenario: ScenarioDetail | null }
  | { type: "run"; run: RunState | null }
  | { type: "snapshot"; run: RunState; events: ObservatoryEvent[]; timeline: Timeline }
  | { type: "events"; events: ObservatoryEvent[] }
  | { type: "timeline"; timeline: Timeline | null }
  | { type: "playing"; playing: boolean }
  | { type: "speed"; speed: number }
  | { type: "busy"; busy: boolean }
  | { type: "error"; error: string | null }
  | { type: "select"; selection: Selection }
  | { type: "layer"; key: keyof Layers; value?: boolean }
  | { type: "eventFilter"; filter: EventFilter }
  | { type: "liveAvailability"; availability: LiveAvailability | null }
  | { type: "liveStore"; store: ObservatoryStore | null };

export function reducer(state: ObservatoryState, action: Action): ObservatoryState {
  switch (action.type) {
    case "health":
      return { ...state, health: action.health };
    case "scenarios":
      return { ...state, scenarios: action.scenarios };
    case "scenario":
      return {
        ...state,
        scenarioId: action.scenarioId,
        scenario: action.scenario,
        run: null,
        events: [],
        timeline: null,
        playing: false,
        selection: null,
      };
    case "run":
      return { ...state, run: action.run };
    case "snapshot":
      return { ...state, run: action.run, events: action.events, timeline: action.timeline };
    case "events":
      return { ...state, events: action.events };
    case "timeline":
      return { ...state, timeline: action.timeline };
    case "playing":
      return { ...state, playing: action.playing };
    case "speed":
      return { ...state, speed: action.speed };
    case "busy":
      return { ...state, busy: action.busy };
    case "error":
      return { ...state, error: action.error };
    case "select":
      return { ...state, selection: action.selection };
    case "layer":
      return {
        ...state,
        layers: { ...state.layers, [action.key]: action.value ?? !state.layers[action.key] },
      };
    case "eventFilter":
      return { ...state, eventFilter: action.filter };
    case "liveAvailability":
      return { ...state, liveAvailability: action.availability };
    case "liveStore":
      return { ...state, liveStore: action.store };
  }
}

export interface ObservatoryActions {
  selectScenario(scenarioId: string): Promise<void>;
  startRun(): Promise<void>;
  play(): void;
  pause(): void;
  step(): Promise<void>;
  reset(): Promise<void>;
  seek(timeS: number): Promise<void>;
  setSpeed(speed: number): void;
  select(selection: Selection): void;
  toggleLayer(key: keyof Layers): void;
  setEventFilter(filter: EventFilter): void;
  refreshLiveAvailability(): Promise<void>;
  startLiveRun(capture: boolean): Promise<void>;
  stopLiveRun(): Promise<void>;
  reconnectLiveRun(): Promise<void>;
}

const StateContext = createContext<ObservatoryState>(initialState);
const ActionsContext = createContext<ObservatoryActions | null>(null);

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status}: ${error.message}`;
  if (error instanceof Error) return error.message;
  return String(error);
}

/**
 * True if a response was captured under a run/generation that is no longer
 * current: either it belongs to a different run_id, or a newer exclusive
 * action (selectScenario/startRun/reset) has started since the request was
 * issued. Pure so it can be unit-tested without the provider.
 */
export function isStaleSnapshot(
  currentRunId: string | null,
  currentGeneration: number,
  snapshot: Snapshot,
  requestGeneration: number,
): boolean {
  return snapshot.state.run_id !== currentRunId || requestGeneration !== currentGeneration;
}

export function ObservatoryProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const runIdRef = useRef<string | null>(null);
  // Serial executor state: queueTailRef is the promise chain every mutating
  // action is threaded through (so requests never overlap), pendingCountRef
  // counts enqueued-but-not-settled tasks (drives `busy`), and generationRef
  // is bumped by exclusive actions that supersede anything already in flight.
  // The LIVE poll loop (below) reuses this exact executor: it goes through
  // runExclusive so a poll in flight suppresses the next one, and every
  // response - poll or mutation alike - is published through applyRun, which
  // drops it via isStaleSnapshot if the run/generation has moved on.
  const queueTailRef = useRef<Promise<void>>(Promise.resolve());
  const pendingCountRef = useRef(0);
  const generationRef = useRef(0);
  const speedRef = useRef(state.speed);
  speedRef.current = state.speed;

  // Every mutation returns the complete snapshot (state, events and timeline
  // captured under the run's lock at one revision); the UI publishes exactly
  // that, so a response always describes its own request - unless it is
  // stale (superseded run or generation), in which case it is dropped.
  const applyRun = useCallback((snapshot: Snapshot, generation: number) => {
    if (isStaleSnapshot(runIdRef.current, generationRef.current, snapshot, generation)) return;
    dispatch({
      type: "snapshot",
      run: snapshot.state,
      events: snapshot.events.events,
      timeline: snapshot.timeline,
    });
  }, []);

  // Appends `work` to the serial queue. The wrapper never rejects (errors are
  // caught and turned into the `error`/`playing` dispatches below), so a
  // failed task can never poison the chain: task N+1 always starts once task
  // N has fully settled, in strict FIFO order.
  const enqueue = useCallback((work: () => Promise<void>) => {
    pendingCountRef.current += 1;
    if (pendingCountRef.current === 1) dispatch({ type: "busy", busy: true });
    const task = queueTailRef.current.then(async () => {
      try {
        await work();
        dispatch({ type: "error", error: null });
      } catch (error) {
        dispatch({ type: "error", error: describe(error) });
        dispatch({ type: "playing", playing: false });
      } finally {
        pendingCountRef.current -= 1;
        if (pendingCountRef.current === 0) dispatch({ type: "busy", busy: false });
      }
    });
    // The wrapper above cannot reject, but the chain tail must survive even if it
    // ever did: a rejected tail would poison every later task and wedge `busy`.
    queueTailRef.current = task.catch(() => {});
    return task;
  }, []);

  // Exclusive actions (selectScenario, startRun, step, reset, playback ticks)
  // deliberately refuse outright while anything is queued or running, rather
  // than piling up behind it.
  const runExclusive = useCallback(
    (work: () => Promise<void>) => {
      if (pendingCountRef.current > 0) return Promise.resolve();
      return enqueue(work);
    },
    [enqueue],
  );

  // Queued actions (seek) always enqueue: queued seeks are strictly FIFO, so
  // the final displayed time is whichever seek was issued last.
  const runQueued = useCallback((work: () => Promise<void>) => enqueue(work), [enqueue]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [health, scenarios] = await Promise.all([api.health(), api.listScenarios()]);
        if (cancelled) return;
        dispatch({ type: "health", health: `${health.engine} ${health.version}` });
        dispatch({ type: "scenarios", scenarios });
      } catch (error) {
        if (!cancelled) dispatch({ type: "error", error: describe(error) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // A selection is ignored while a request is queued or in flight (the
  // selector is also disabled), so a run snapshot can never land under a
  // newer scenario. runExclusive owns that refusal; the placeholder dispatch
  // and runIdRef clear below only take effect if the request is not refused.
  const selectScenario = useCallback(
    (scenarioId: string) => {
      return runExclusive(async () => {
        generationRef.current += 1;
        runIdRef.current = null;
        dispatch({ type: "scenario", scenarioId, scenario: null });
        const scenario = await api.getScenario(scenarioId);
        dispatch({ type: "scenario", scenarioId, scenario });
      });
    },
    [runExclusive],
  );

  // Starting or replacing a run always stops playback, even when the start
  // itself is refused because another request is still queued or running.
  const startRun = useCallback(() => {
    const scenarioId = state.scenarioId;
    if (!scenarioId) return Promise.resolve();
    dispatch({ type: "playing", playing: false });
    return runExclusive(async () => {
      generationRef.current += 1;
      const generation = generationRef.current;
      const snapshot = await api.createRun(scenarioId);
      runIdRef.current = snapshot.state.run_id;
      dispatch({ type: "select", selection: null });
      applyRun(snapshot, generation);
    });
  }, [applyRun, runExclusive, state.scenarioId]);

  const step = useCallback(() => {
    const runId = runIdRef.current;
    if (!runId) return Promise.resolve();
    return runExclusive(async () => {
      const generation = generationRef.current;
      applyRun(await api.step(runId), generation);
    });
  }, [applyRun, runExclusive]);

  // Reset pauses first for the same reason as startRun; a refused reset still pauses.
  const reset = useCallback(() => {
    const runId = runIdRef.current;
    if (!runId) return Promise.resolve();
    dispatch({ type: "playing", playing: false });
    return runExclusive(async () => {
      generationRef.current += 1;
      const generation = generationRef.current;
      applyRun(await api.reset(runId), generation);
    });
  }, [applyRun, runExclusive]);

  // Seeking pauses playback and always queues (runQueued): a seek issued
  // while a tick or another seek is still in flight waits its turn instead of
  // being dropped, and multiple queued seeks resolve strictly FIFO so the
  // final displayed time is the last seek issued.
  const seek = useCallback(
    (timeS: number) => {
      const runId = runIdRef.current;
      if (!runId) return Promise.resolve();
      dispatch({ type: "playing", playing: false });
      return runQueued(async () => {
        const generation = generationRef.current;
        applyRun(await api.seek(runId, timeS), generation);
      });
    },
    [applyRun, runQueued],
  );

  // Fetches sensor availability; safe to call anytime (read-only), independent
  // of the serial queue used by mutating actions.
  const refreshLiveAvailability = useCallback(async () => {
    try {
      const availability = await api.liveStatus();
      dispatch({ type: "liveAvailability", availability });
    } catch (error) {
      dispatch({ type: "error", error: describe(error) });
    }
  }, []);

  // Starting a live run has no scenario to select: it replaces whatever run
  // (if any) is active, the same way startRun replaces a replay run. Pauses
  // playback first for the same reason startRun/reset do.
  const startLiveRun = useCallback(
    (capture: boolean) => {
      dispatch({ type: "playing", playing: false });
      return runExclusive(async () => {
        generationRef.current += 1;
        const generation = generationRef.current;
        const snapshot = await api.createLiveRun(capture);
        runIdRef.current = snapshot.state.run_id;
        dispatch({ type: "select", selection: null });
        dispatch({ type: "liveStore", store: null });
        applyRun(snapshot, generation);
        try {
          const liveStore = await api.getStore(snapshot.state.run_id);
          if (runIdRef.current === snapshot.state.run_id) {
            dispatch({ type: "liveStore", store: liveStore });
          }
        } catch (error) {
          dispatch({ type: "error", error: describe(error) });
        }
      }).finally(() => void refreshLiveAvailability());
    },
    [applyRun, refreshLiveAvailability, runExclusive],
  );

  // Stop/reconnect are user-initiated hardware controls: unlike a playback
  // tick, they must never be silently dropped just because a live poll
  // happens to be in flight, so they always queue (runQueued) rather than
  // refuse outright.
  const stopLiveRun = useCallback(() => {
    const runId = runIdRef.current;
    if (!runId) return Promise.resolve();
    return runQueued(async () => {
      const generation = generationRef.current;
      applyRun(await api.stopRun(runId), generation);
    }).finally(() => void refreshLiveAvailability());
  }, [applyRun, refreshLiveAvailability, runQueued]);

  const reconnectLiveRun = useCallback(() => {
    const runId = runIdRef.current;
    if (!runId) return Promise.resolve();
    return runQueued(async () => {
      const generation = generationRef.current;
      applyRun(await api.reconnectRun(runId), generation);
    }).finally(() => void refreshLiveAvailability());
  }, [applyRun, refreshLiveAvailability, runQueued]);

  const play = useCallback(() => {
    // A run replacement or seek may be in flight; playback binds to the run
    // that exists once it settles, so refuse to start while anything is
    // queued or running rather than binding to a run about to be discarded.
    if (pendingCountRef.current > 0) return;
    if (runIdRef.current && !state.run?.finished) dispatch({ type: "playing", playing: true });
  }, [state.run?.finished]);
  const pause = useCallback(() => dispatch({ type: "playing", playing: false }), []);
  const setSpeed = useCallback((speed: number) => dispatch({ type: "speed", speed }), []);
  const select = useCallback((selection: Selection) => dispatch({ type: "select", selection }), []);
  const toggleLayer = useCallback((key: keyof Layers) => dispatch({ type: "layer", key }), []);
  const setEventFilter = useCallback(
    (filter: EventFilter) => dispatch({ type: "eventFilter", filter }),
    [],
  );

  // Playback: each wall-clock tick advances SIMULATED time by speed * TICK_MS.
  // The API owns the clock; the browser only paces requests. runExclusive
  // refuses a tick outright while a previous tick (or any other action) is
  // still queued or running, so ticks never overlap.
  useEffect(() => {
    if (!state.playing) return;
    // Bind to the authoritative run identity from state (not a ref captured
    // once): including it in the deps tears the interval down and recreates
    // it whenever the active run changes, and clears it when run becomes
    // null, so a settling run replacement can never leave a stale loop
    // ticking the discarded run.
    const runId = state.run?.run_id ?? null;
    if (!runId) return;
    const handle = window.setInterval(() => {
      // Belt and braces: skip if the run changed since this interval was set up.
      if (runIdRef.current !== runId) return;
      const seconds = (speedRef.current * TICK_MS) / 1000;
      void runExclusive(async () => {
        const generation = generationRef.current;
        const snapshot = await api.advance(runId, seconds);
        applyRun(snapshot, generation);
        if (snapshot.state.finished) dispatch({ type: "playing", playing: false });
      });
    }, TICK_MS);
    return () => window.clearInterval(handle);
  }, [applyRun, runExclusive, state.playing, state.run?.run_id]);

  // LIVE polling: a LIVE run's time is the wall clock owned by the sensor
  // session, not something the client advances, so instead of calling
  // advance() this polls the server's own snapshot. It reuses runExclusive
  // (a poll in flight suppresses the next one) and applyRun/isStaleSnapshot
  // (a response for a run that has since been replaced/reset is dropped) -
  // the exact same discipline as the replay tick loop above. Bound to the
  // run's mode/id/finished from state (not a ref) so a run replacement, a
  // stop, or the run finishing all tear this interval down.
  useEffect(() => {
    if (state.run?.mode !== "LIVE" || state.run.finished) return;
    const runId = state.run.run_id;
    const handle = window.setInterval(() => {
      if (runIdRef.current !== runId) return;
      void runExclusive(async () => {
        const generation = generationRef.current;
        applyRun(await api.getSnapshot(runId), generation);
      });
    }, LIVE_POLL_MS);
    return () => window.clearInterval(handle);
  }, [applyRun, runExclusive, state.run?.mode, state.run?.run_id, state.run?.finished]);

  const actions = useMemo<ObservatoryActions>(
    () => ({
      selectScenario,
      startRun,
      play,
      pause,
      step,
      reset,
      seek,
      setSpeed,
      select,
      toggleLayer,
      setEventFilter,
      refreshLiveAvailability,
      startLiveRun,
      stopLiveRun,
      reconnectLiveRun,
    }),
    [
      selectScenario,
      startRun,
      play,
      pause,
      step,
      reset,
      seek,
      setSpeed,
      select,
      toggleLayer,
      setEventFilter,
      refreshLiveAvailability,
      startLiveRun,
      stopLiveRun,
      reconnectLiveRun,
    ],
  );

  return (
    <StateContext.Provider value={state}>
      <ActionsContext.Provider value={actions}>{children}</ActionsContext.Provider>
    </StateContext.Provider>
  );
}

export function useObservatory(): ObservatoryState {
  return useContext(StateContext);
}

export function useActions(): ObservatoryActions {
  const actions = useContext(ActionsContext);
  if (!actions) throw new Error("useActions must be used inside ObservatoryProvider");
  return actions;
}

/** Event rows visible under the current filter and selection. */
export function filterEvents(
  events: ObservatoryEvent[],
  filter: EventFilter,
  selection: Selection,
): ObservatoryEvent[] {
  return events.filter((event) => {
    if (filter === "LIFECYCLE" && event.kind === "RETAIL_EVENT") return false;
    if (filter !== "ALL" && filter !== "LIFECYCLE" && event.label !== filter) return false;
    if (selection?.kind === "person") {
      return (
        event.shopper_track_id === selection.id || event.counterpart_track_id === selection.id
      );
    }
    if (selection?.kind === "item") return event.epc === selection.id;
    return true;
  });
}
