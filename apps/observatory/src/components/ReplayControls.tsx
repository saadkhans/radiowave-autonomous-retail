import { fmtSeconds } from "@/lib/format";
import { SPEEDS, useActions, useObservatory } from "@/state/store";

export function ReplayControls() {
  const { run, playing, speed, busy } = useObservatory();
  const { play, pause, step, reset, setSpeed } = useActions();
  const hasRun = run !== null;
  const finished = run?.finished ?? false;

  return (
    <div className="panel flex items-center gap-2 px-3 py-2" data-testid="replay-controls">
      <button
        type="button"
        className={`btn ${playing ? "btn-active" : ""}`}
        disabled={!hasRun || finished}
        onClick={() => (playing ? pause() : play())}
        aria-label={playing ? "Pause" : "Play"}
      >
        {playing ? "❚❚ Pause" : "▶ Play"}
      </button>
      <button
        type="button"
        className="btn"
        disabled={!hasRun || finished || playing || busy}
        onClick={() => void step()}
        aria-label="Step"
      >
        ⏭ Step
      </button>
      <button
        type="button"
        className="btn"
        disabled={!hasRun || busy}
        onClick={() => void reset()}
        aria-label="Reset"
      >
        ↺ Reset
      </button>
      <div className="mx-2 h-5 w-px bg-console-line" />
      <div className="flex items-center gap-1" role="group" aria-label="Speed">
        {SPEEDS.map((value) => (
          <button
            key={value}
            type="button"
            className={`btn ${speed === value ? "btn-active" : ""}`}
            onClick={() => setSpeed(value)}
            aria-pressed={speed === value}
          >
            {value}x
          </button>
        ))}
      </div>
      <div className="flex-1" />
      <div className="mono text-[12px] text-console-muted" data-testid="sim-clock">
        t = <span className="text-console-text">{fmtSeconds(run?.time_s ?? 0)}</span>
        {run ? ` / ${fmtSeconds(run.duration_s)}` : ""}
        {run ? ` · step ${run.steps} · obs ${run.observations_cursor}/${run.observations_total}` : ""}
        {finished ? <span className="ml-2 text-console-ok">finished</span> : null}
      </div>
    </div>
  );
}
