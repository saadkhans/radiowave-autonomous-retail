import { useEffect, useState } from "react";

import { useActions, useObservatory } from "@/state/store";

const DEFAULT_ADVANCE_S = 5;

/**
 * Virtual store lab: pick a scenario (optionally overriding its seed) and
 * start a SIM run - a `mode === "LIVE"` run reusing every existing panel, so
 * the only Observatory-specific work here is the scenario picker and the
 * Advance/Step/Reset controls a SIM run legitimately supports (unlike
 * hardware LIVE, which only ever exposes Stop/Reconnect via LiveControls).
 * Shown whenever no run is active (either kind of run can be started from
 * here, same as ReplayControls/LiveControls) or the active run is a SIM run.
 */
export function SimControls() {
  const { run, simScenarios, busy } = useObservatory();
  const { startSimRun, advance, step, reset } = useActions();
  const [scenarioId, setScenarioId] = useState<string | null>(null);
  const [seedOverride, setSeedOverride] = useState("");
  const [advanceSeconds, setAdvanceSeconds] = useState(DEFAULT_ADVANCE_S);

  useEffect(() => {
    if (scenarioId === null && simScenarios.length > 0) {
      setScenarioId(simScenarios[0].scenario_id);
    }
  }, [scenarioId, simScenarios]);

  const isSim = run?.mode === "LIVE" && run.live?.simulated === true;
  const running = isSim && !run.finished;

  if (isSim && run) {
    return (
      <div className="panel flex items-center gap-3 px-3 py-2" data-testid="sim-controls">
        <div className="mono text-[11px] text-console-muted">
          driving {run.live?.simulated_scenario_id ?? run.scenario_id}
          {run.live?.simulated_seed !== null && run.live?.simulated_seed !== undefined
            ? ` · seed ${run.live.simulated_seed}`
            : ""}
        </div>
        <div className="flex-1" />
        <div className="flex shrink-0 items-center gap-2">
          <input
            type="number"
            min={0.25}
            step={0.25}
            className="btn w-16"
            value={advanceSeconds}
            aria-label="Advance seconds"
            disabled={!running}
            onChange={(event) => setAdvanceSeconds(Number(event.target.value))}
          />
          <button
            type="button"
            className="btn"
            disabled={!running || busy}
            onClick={() => void advance(advanceSeconds)}
            aria-label="Advance"
          >
            ⏩ Advance
          </button>
          <button
            type="button"
            className="btn"
            disabled={!running || busy}
            onClick={() => void step()}
            aria-label="Step"
          >
            ⏭ Step
          </button>
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() => void reset()}
            aria-label="Reset"
          >
            ↺ Reset
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="panel flex flex-col gap-2 px-3 py-2" data-testid="sim-controls">
      <div className="panel-title">Virtual store lab</div>
      <select
        aria-label="Lab scenario"
        className="btn"
        value={scenarioId ?? ""}
        disabled={simScenarios.length === 0 || busy}
        onChange={(event) => setScenarioId(event.target.value)}
      >
        {simScenarios.length === 0 ? <option value="">loading scenarios…</option> : null}
        {simScenarios.map((entry) => (
          <option key={entry.scenario_id} value={entry.scenario_id}>
            {entry.scenario_id} · {entry.name}
          </option>
        ))}
      </select>
      <label className="flex items-center gap-2 text-[12px]">
        Seed override
        <input
          type="number"
          min={0}
          className="btn w-24"
          placeholder="scenario default"
          value={seedOverride}
          onChange={(event) => setSeedOverride(event.target.value)}
        />
      </label>
      <button
        type="button"
        className="btn btn-active self-start"
        disabled={!scenarioId || busy}
        onClick={() => void startSimRun(scenarioId, seedOverride === "" ? undefined : Number(seedOverride))}
      >
        Start simulation
      </button>
    </div>
  );
}
