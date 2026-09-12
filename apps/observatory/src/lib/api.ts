import type {
  EventPage,
  RunState,
  ScenarioDetail,
  ScenarioSummary,
  Snapshot,
  Timeline,
} from "@/types/api";

const BASE = "/api";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      // keep the status text
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  health: () => request<{ status: string; engine: string; version: string }>("/health"),
  listScenarios: () => request<ScenarioSummary[]>("/scenarios"),
  getScenario: (scenarioId: string) => request<ScenarioDetail>(`/scenarios/${scenarioId}`),
  createRun: (scenarioId: string, seed?: number) =>
    post<RunState>("/runs", { scenario_id: scenarioId, seed: seed ?? null }),
  deleteRun: (runId: string) => request<void>(`/runs/${runId}`, { method: "DELETE" }),
  getState: (runId: string) => request<RunState>(`/runs/${runId}/state`),
  // Mutations return the complete snapshot captured under the run's lock.
  reset: (runId: string) => post<Snapshot>(`/runs/${runId}/reset`),
  step: (runId: string) => post<Snapshot>(`/runs/${runId}/step`),
  advance: (runId: string, seconds: number) =>
    post<Snapshot>(`/runs/${runId}/advance`, { seconds }),
  seek: (runId: string, timeS: number) => post<Snapshot>(`/runs/${runId}/seek`, { time_s: timeS }),
  getEvents: (runId: string, since = 0, limit = 5000) =>
    request<EventPage>(`/runs/${runId}/events?since=${since}&limit=${limit}`),
  getTimeline: (runId: string) => request<Timeline>(`/runs/${runId}/timeline`),
  getSnapshot: (runId: string) => request<Snapshot>(`/runs/${runId}/snapshot`),
};

export type Api = typeof api;
