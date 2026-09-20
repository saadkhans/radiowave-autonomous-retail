import { CartPanel } from "@/components/CartPanel";
import { EventStream } from "@/components/EventStream";
import { Inspector } from "@/components/Inspector";
import { LayerToggles } from "@/components/LayerToggles";
import { LiveControls } from "@/components/LiveControls";
import { ReplayControls } from "@/components/ReplayControls";
import { ScenarioSelector } from "@/components/ScenarioSelector";
import { SimControls } from "@/components/SimControls";
import { StoreMap } from "@/components/StoreMap";
import { Timeline } from "@/components/Timeline";
import { ObservatoryProvider, useObservatory } from "@/state/store";

/**
 * Clearly visible LIVE (red/pulsing) vs REPLAY vs SIMULATED mode indicator;
 * never rendered without a run. A SIM run reports `mode === "LIVE"` (it reuses
 * every LIVE panel), so it is distinguished here by `live.simulated`: a
 * SIMULATED run gets its own colour (never the hardware LIVE red) plus the
 * scenario id and seed inline, so a screenshot of this badge alone is
 * self-describing and cannot be mistaken for a real measurement.
 */
function ModeBadge() {
  const { run } = useObservatory();
  if (!run) return null;
  const isSimulated = run.mode === "LIVE" && run.live?.simulated === true;
  const isLive = run.mode === "LIVE" && !isSimulated;

  if (isSimulated) {
    return (
      <span
        className="mono flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-widest bg-[#c792ff]/20 text-[#c792ff]"
        data-testid="mode-badge"
      >
        <span aria-hidden="true">🧪</span>
        SIMULATED
        {run.live?.simulated_scenario_id ? ` · ${run.live.simulated_scenario_id}` : ""}
        {run.live?.simulated_seed !== null && run.live?.simulated_seed !== undefined
          ? ` · seed ${run.live.simulated_seed}`
          : ""}
      </span>
    );
  }

  return (
    <span
      className={`mono flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-widest ${
        isLive ? "bg-console-danger/20 text-console-danger" : "bg-console-panel-2 text-console-muted"
      }`}
      data-testid="mode-badge"
    >
      {isLive ? (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-console-danger" aria-hidden="true" />
      ) : null}
      {isLive ? "LIVE" : "REPLAY"}
    </span>
  );
}

function TopBar() {
  const { health, error, run } = useObservatory();
  return (
    <header className="flex items-center gap-4 border-b border-console-line px-4 py-2">
      <div className="flex items-baseline gap-2">
        <span className="mono text-sm font-semibold tracking-[0.2em] text-console-accent">
          RADIOWAVE
        </span>
        <span className="mono text-xs tracking-[0.2em] text-console-muted">OBSERVATORY v0</span>
      </div>
      <ModeBadge />
      <div className="mono text-[11px] text-console-muted">
        engine {health ?? "offline"}
        {run ? ` · run ${run.run_id} · seed ${run.seed}` : ""}
      </div>
      <div className="flex-1" />
      {error ? (
        <div className="mono text-[11px] text-console-danger" role="alert">
          {error}
        </div>
      ) : null}
      <span className="mono text-[10px] uppercase tracking-widest text-console-muted">
        synthetic data · engineering console
      </span>
    </header>
  );
}

function Console() {
  const { run } = useObservatory();
  // ReplayControls owns REPLAY runs (and the no-run placeholder); LiveControls
  // owns LIVE runs (running or stopped-and-finished, which frees the sensor
  // back to a "Start live" prompt) and the no-run placeholder. Both show when
  // no run is active yet, so either kind of run can be started from here.
  const showReplayControls = !run || run.mode === "REPLAY";
  const showLiveControls = !run || run.mode === "LIVE";
  // SimControls shares LiveControls' visibility (both are "mode LIVE" family
  // controls, hidden only during an active REPLAY run): it shows the scenario
  // picker/Start whenever no sim is currently driving - including while a
  // hardware LIVE run is active, so an operator can switch to the lab the
  // same way selecting a scenario switches out of LIVE - and shows the
  // Advance/Step/Reset controls a simulated run supports (but hardware LIVE
  // does not) once its own sim run is bound and active.
  const showSimControls = showLiveControls;
  return (
    <div className="flex h-full min-h-[900px] min-w-[1440px] flex-col bg-console-bg text-console-text">
      <TopBar />
      <div className="grid min-h-0 flex-1 grid-cols-[300px_1fr_360px] gap-2 p-2">
        <aside className="flex min-h-0 flex-col gap-2">
          <ScenarioSelector />
          <LayerToggles />
        </aside>
        <main className="flex min-h-0 flex-col gap-2">
          <div className="panel relative min-h-0 flex-1 overflow-hidden">
            <StoreMap />
          </div>
          {showReplayControls ? <ReplayControls /> : null}
          {showLiveControls ? <LiveControls /> : null}
          {showSimControls ? <SimControls /> : null}
          <Timeline />
          <div className="panel min-h-0 h-[260px]">
            <EventStream />
          </div>
        </main>
        <aside className="flex min-h-0 flex-col gap-2">
          <div className="panel min-h-0 flex-1">
            <Inspector />
          </div>
          <div className="panel min-h-0 h-[300px]">
            <CartPanel />
          </div>
        </aside>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <ObservatoryProvider>
      <Console />
    </ObservatoryProvider>
  );
}
