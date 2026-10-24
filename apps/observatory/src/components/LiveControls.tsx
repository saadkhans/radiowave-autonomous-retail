import { useEffect, useState } from "react";

import { LiveStatusPanel } from "@/components/LiveStatusPanel";
import { useActions, useObservatory } from "@/state/store";

/**
 * LIVE mode's equivalent of ReplayControls+Timeline scrubber: shown instead of
 * them while the active run is LIVE (radar status + Stop/Reconnect), and
 * offers the "Start live" entry point whenever no run is active. Renders
 * nothing while a REPLAY run is active (ReplayControls owns that case).
 */
export function LiveControls() {
  const { run, liveAvailability, busy } = useObservatory();
  const { startLiveRun, stopLiveRun, reconnectLiveRun, adoptLiveRun, refreshLiveAvailability } =
    useActions();
  const [capture, setCapture] = useState(false);

  useEffect(() => {
    void refreshLiveAvailability();
  }, [refreshLiveAvailability]);

  // A stopped live run keeps mode "LIVE" (it stays readable) but frees the
  // sensor, so once it's finished this falls back to the "Start live" panel.
  const isLive = run?.mode === "LIVE" && !run.finished;

  // Invariant 19: the server may report a LIVE run still running (e.g. after
  // a page reload) that this client is not bound to. Surfaced only when it's
  // genuinely not ours yet - once adopted, `run.run_id` matches and this
  // resolves to false, so the control disappears in favor of the LIVE panel
  // above (Stop/Reconnect).
  const activeRunId = liveAvailability?.active_run_id ?? null;
  const isBoundToActiveRun = activeRunId !== null && run?.run_id === activeRunId;
  const adoptable = activeRunId !== null && !isBoundToActiveRun;

  // This panel is reused unchanged for a SIM run (it also reports mode
  // "LIVE"), so its own inline badge must not claim hardware "LIVE" for a
  // simulated feed - same correctness requirement as the TopBar ModeBadge.
  const isSimulated = run?.live?.simulated === true;

  if (isLive) {
    return (
      <div className="panel flex items-center gap-3 px-3 py-2" data-testid="live-controls">
        <span
          className={`mono flex items-center gap-1.5 rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-widest ${
            isSimulated ? "bg-[#c792ff]/20 text-[#c792ff]" : "text-console-danger"
          }`}
          data-testid="mode-badge-live"
        >
          {isSimulated ? (
            <span aria-hidden="true">🧪</span>
          ) : (
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-console-danger" aria-hidden="true" />
          )}
          {isSimulated ? "SIMULATED" : "LIVE"}
        </span>
        <div className="flex-1">
          <LiveStatusPanel live={run.live} />
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {/*
            Deliberately not disabled by `busy`: busy flickers true/false on every
            250 ms poll, and Stop/Reconnect are user-initiated hardware controls
            that must stay clickable throughout - stopLiveRun/reconnectLiveRun use
            runQueued so the click is queued behind any in-flight poll rather than
            lost. (Disabled once finished simply because there'd be nothing to
            stop/reconnect.)
          */}
          <button
            type="button"
            className="btn"
            onClick={() => void reconnectLiveRun()}
            aria-label="Reconnect"
          >
            ⟲ Reconnect
          </button>
          <button type="button" className="btn btn-active" onClick={() => void stopLiveRun()} aria-label="Stop">
            ■ Stop
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="panel flex flex-col gap-2 px-3 py-2" data-testid="live-controls">
      <div className="panel-title">Live sensor</div>
      <div className="mono text-[11px] text-console-muted">
        {liveAvailability
          ? `${liveAvailability.sensor_name ?? liveAvailability.sensor_id ?? "no sensor"}${
              liveAvailability.data_port ? ` · ${liveAvailability.data_port}` : ""
            }`
          : "checking sensor availability…"}
      </div>
      {liveAvailability?.reason ? (
        <div className="mono text-[11px] text-console-danger" data-testid="live-unavailable-reason">
          {liveAvailability.reason}
        </div>
      ) : null}
      {adoptable ? (
        <button
          type="button"
          className="btn self-start"
          onClick={() => void adoptLiveRun(activeRunId!)}
        >
          Resume live run {activeRunId}
        </button>
      ) : null}
      <label className="flex items-center gap-2 text-[12px]">
        <input
          type="checkbox"
          checked={capture}
          onChange={(event) => setCapture(event.target.checked)}
          className="accent-console-accent"
        />
        Capture normalized recording
      </label>
      <button
        type="button"
        className="btn btn-active self-start"
        disabled={!liveAvailability || liveAvailability.reason !== null || busy}
        onClick={() => void startLiveRun(capture)}
      >
        Start live
      </button>
    </div>
  );
}
