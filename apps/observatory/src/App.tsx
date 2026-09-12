import { CartPanel } from "@/components/CartPanel";
import { EventStream } from "@/components/EventStream";
import { Inspector } from "@/components/Inspector";
import { LayerToggles } from "@/components/LayerToggles";
import { ReplayControls } from "@/components/ReplayControls";
import { ScenarioSelector } from "@/components/ScenarioSelector";
import { StoreMap } from "@/components/StoreMap";
import { Timeline } from "@/components/Timeline";
import { ObservatoryProvider, useObservatory } from "@/state/store";

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
          <ReplayControls />
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
