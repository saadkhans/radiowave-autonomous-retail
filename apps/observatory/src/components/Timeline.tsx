import { useEffect, useState } from "react";

import { decisionColor } from "@/lib/format";
import { useActions, useObservatory } from "@/state/store";
import type { DecisionKind } from "@/types/api";

export function Timeline() {
  const { run, timeline, busy } = useObservatory();
  const { seek } = useActions();
  const duration = run?.duration_s ?? 0;
  const [scrub, setScrub] = useState<number | null>(null);

  useEffect(() => {
    setScrub(null);
  }, [run?.time_s]);

  const value = scrub ?? run?.time_s ?? 0;
  const markers = timeline?.markers ?? [];
  const truth = timeline?.ground_truth ?? [];

  return (
    <div className="panel px-3 py-2" data-testid="timeline">
      <div className="relative h-4">
        {duration > 0
          ? truth.map((entry, index) => (
              <span
                key={`gt-${index}`}
                title={`ground truth ${entry.event_type} ${entry.epc.slice(-6)} @ ${entry.t_s}s`}
                className="absolute top-0 h-2 w-px bg-console-muted/60"
                style={{ left: `${(entry.t_s / duration) * 100}%` }}
              />
            ))
          : null}
        {duration > 0
          ? markers.map((marker, index) => (
              <span
                key={`m-${index}`}
                title={`${marker.label} ${marker.epc?.slice(-6) ?? ""} @ ${marker.t_s}s`}
                className="absolute bottom-0 h-2 w-0.5"
                style={{
                  left: `${(marker.t_s / duration) * 100}%`,
                  background: decisionColor(marker.decision as DecisionKind | null),
                }}
              />
            ))
          : null}
      </div>
      <input
        type="range"
        aria-label="Timeline"
        className="w-full"
        min={0}
        max={duration}
        step={run?.step_interval_s ?? 0.25}
        value={value}
        disabled={!run || busy}
        onChange={(event) => setScrub(Number(event.target.value))}
        onMouseUp={() => {
          if (scrub !== null) void seek(scrub);
        }}
        onKeyUp={() => {
          if (scrub !== null) void seek(scrub);
        }}
        onTouchEnd={() => {
          if (scrub !== null) void seek(scrub);
        }}
      />
      <div className="mono flex justify-between text-[10px] text-console-muted">
        <span>0.0s</span>
        <span>{scrub !== null ? `seek → ${scrub.toFixed(2)}s` : ""}</span>
        <span>{duration.toFixed(1)}s</span>
      </div>
    </div>
  );
}
