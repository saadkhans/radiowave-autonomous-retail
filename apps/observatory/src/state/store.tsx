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
  ObservatoryEvent,
  RunState,
  ScenarioDetail,
  ScenarioSummary,
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
  | { type: "eventFilter"; filter: EventFilter };

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
}

const StateContext = createContext<ObservatoryState>(initialState);
const ActionsContext = createContext<ObservatoryActions | null>(null);

function describe(error: unknown): string {
  if (error instanceof ApiError) return `${error.status}: ${error.message}`;
  if (error instanceof Error) return error.message;
  return String(error);
}

export function ObservatoryProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  const runIdRef = useRef<string | null>(null);
  const inflightRef = useRef(false);
  const speedRef = useRef(state.speed);
  speedRef.current = state.speed;

  // Run state, events and timeline are published together so the map, carts and
  // event stream never show snapshots from different simulated times.
  const applyRun = useCallback(async (run: RunState) => {
    const [page, timeline] = await Promise.all([
      api.getEvents(run.run_id),
      api.getTimeline(run.run_id),
    ]);
    if (runIdRef.current !== run.run_id) return;
    dispatch({ type: "snapshot", run, events: page.events, timeline });
  }, []);

  const guarded = useCallback(
    async (work: () => Promise<void>) => {
      if (inflightRef.current) return;
      inflightRef.current = true;
      dispatch({ type: "busy", busy: true });
      try {
        await work();
        dispatch({ type: "error", error: null });
      } catch (error) {
        dispatch({ type: "error", error: describe(error) });
        dispatch({ type: "playing", playing: false });
      } finally {
        inflightRef.current = false;
        dispatch({ type: "busy", busy: false });
      }
    },
    [],
  );

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

  // A selection is ignored while a request is in flight (the selector is also
  // disabled), so a run snapshot can never land under a newer scenario.
  const selectScenario = useCallback(
    async (scenarioId: string) => {
      if (inflightRef.current) return;
      runIdRef.current = null;
      dispatch({ type: "scenario", scenarioId, scenario: null });
      await guarded(async () => {
        const scenario = await api.getScenario(scenarioId);
        dispatch({ type: "scenario", scenarioId, scenario });
      });
    },
    [guarded],
  );

  const startRun = useCallback(async () => {
    const scenarioId = state.scenarioId;
    if (!scenarioId) return;
    await guarded(async () => {
      const run = await api.createRun(scenarioId);
      runIdRef.current = run.run_id;
      dispatch({ type: "select", selection: null });
      await applyRun(run);
    });
  }, [applyRun, guarded, state.scenarioId]);

  const step = useCallback(async () => {
    const runId = runIdRef.current;
    if (!runId) return;
    await guarded(async () => applyRun(await api.step(runId)));
  }, [applyRun, guarded]);

  const reset = useCallback(async () => {
    const runId = runIdRef.current;
    if (!runId) return;
    dispatch({ type: "playing", playing: false });
    await guarded(async () => applyRun(await api.reset(runId)));
  }, [applyRun, guarded]);

  const seek = useCallback(
    async (timeS: number) => {
      const runId = runIdRef.current;
      if (!runId) return;
      await guarded(async () => applyRun(await api.seek(runId, timeS)));
    },
    [applyRun, guarded],
  );

  const play = useCallback(() => {
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
  // The API owns the clock; the browser only paces requests.
  useEffect(() => {
    if (!state.playing) return;
    const runId = runIdRef.current;
    if (!runId) return;
    const handle = window.setInterval(() => {
      if (inflightRef.current) return;
      const seconds = (speedRef.current * TICK_MS) / 1000;
      void guarded(async () => {
        const run = await api.advance(runId, seconds);
        await applyRun(run);
        if (run.finished) dispatch({ type: "playing", playing: false });
      });
    }, TICK_MS);
    return () => window.clearInterval(handle);
  }, [applyRun, guarded, state.playing]);

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
