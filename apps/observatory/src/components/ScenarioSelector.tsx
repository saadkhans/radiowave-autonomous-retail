import { useActions, useObservatory } from "@/state/store";

export function ScenarioSelector() {
  const { scenarios, scenarioId, scenario, run, busy, playing } = useObservatory();
  const locked = busy || playing;
  const { selectScenario, startRun } = useActions();

  return (
    <div className="panel flex min-h-0 flex-1 flex-col">
      <div className="panel-title border-b border-console-line px-3 py-2">Scenarios</div>
      <div className="scroll-y min-h-0 flex-1" role="listbox" aria-label="Scenarios">
        {scenarios.length === 0 ? (
          <div className="px-3 py-2 text-console-muted">No scenarios loaded.</div>
        ) : null}
        {scenarios.map((entry) => {
          const active = entry.scenario_id === scenarioId;
          return (
            <button
              key={entry.scenario_id}
              type="button"
              role="option"
              aria-selected={active}
              disabled={locked}
              onClick={() => void selectScenario(entry.scenario_id)}
              className={`flex w-full items-baseline gap-2 border-b border-console-line/60 px-3 py-1.5 text-left hover:bg-console-panel-2 disabled:cursor-not-allowed disabled:opacity-60 ${
                active ? "bg-console-panel-2 text-console-accent" : ""
              }`}
            >
              <span className="mono w-8 shrink-0 text-console-muted">{entry.scenario_id}</span>
              <span className="truncate">{entry.name}</span>
            </button>
          );
        })}
      </div>
      <div className="border-t border-console-line px-3 py-2">
        {scenario ? (
          <div className="flex flex-col gap-2" data-testid="scenario-detail">
            <div className="mono text-[11px] text-console-accent">
              {scenario.scenario_id} · {scenario.name}
            </div>
            <p className="leading-snug text-console-text/90">{scenario.description}</p>
            <dl className="mono grid grid-cols-2 gap-x-2 gap-y-0.5 text-[11px] text-console-muted">
              <dt>duration</dt>
              <dd>{scenario.duration_s.toFixed(1)} s</dd>
              <dt>shoppers</dt>
              <dd>{scenario.shopper_count}</dd>
              <dt>items</dt>
              <dd>{scenario.item_count}</dd>
              <dt>vision</dt>
              <dd>{scenario.vision_enabled ? "enabled" : "off"}</dd>
              <dt>dropouts</dt>
              <dd>
                radar {scenario.radar_dropouts} · rfid {scenario.rfid_dropouts}
              </dd>
              <dt>seed</dt>
              <dd>{scenario.seed}</dd>
            </dl>
            {scenario.ground_truth.length > 0 ? (
              <div>
                <div className="panel-title mb-1">Expected ground truth</div>
                <ul className="mono text-[11px] text-console-muted">
                  {scenario.ground_truth.map((truth, index) => (
                    <li key={index}>
                      {truth.t_s.toFixed(1)}s {truth.event_type} {truth.epc.slice(-6)}
                      {truth.shopper_label ? ` · ${truth.shopper_label}` : ""}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            <button
              type="button"
              className="btn btn-active"
              disabled={busy}
              onClick={() => void startRun()}
            >
              {run ? `Restart scenario ${scenario.scenario_id}` : `Run scenario ${scenario.scenario_id}`}
            </button>
          </div>
        ) : (
          <div className="text-console-muted">Select a scenario to see its description.</div>
        )}
      </div>
    </div>
  );
}
