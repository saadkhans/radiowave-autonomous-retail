import { CartPanel } from "@/components/CartPanel";
import { EventStream } from "@/components/EventStream";
import { Inspector } from "@/components/Inspector";
import { LayerToggles } from "@/components/LayerToggles";
import { LiveControls } from "@/components/LiveControls";
import { ReplayControls } from "@/components/ReplayControls";
import { ScenarioSelector } from "@/components/ScenarioSelector";
import { StoreMap } from "@/components/StoreMap";
import { Timeline } from "@/components/Timeline";
import { ObservatoryProvider, useObservatory } from "@/state/store";

/** Clearly visible LIVE (red/pulsing) vs REPLAY mode indicator; never rendered without a run. */
function ModeBadge() {
  const { run } = useObservatory();
  if (!run) return null;
  const isLive = run.mode === "LIVE";
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
