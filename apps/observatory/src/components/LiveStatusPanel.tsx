import { fmtAgo, fmtRate, liveStateColor } from "@/lib/format";
import type { LiveStatus } from "@/types/api";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-console-muted">{label}</dt>
      <dd className="mono truncate">{children}</dd>
    </>
  );
}

/** Radar/sensor health for the active LIVE run: state, message, rates, frame counters. */
export function LiveStatusPanel({ live }: { live: LiveStatus | null }) {
  if (!live) {
    return <div className="mono text-[11px] text-console-muted">no sensor status yet</div>;
  }
  return (
    <div data-testid="live-status-panel">
      <div className="mb-1 flex items-center gap-2">
        <span
          className="badge"
          style={{ background: "#0f1216", color: liveStateColor(live.state) }}
          data-testid="live-state-badge"
        >
          {live.state}
        </span>
        <span className="mono text-[11px]">
          {live.sensor_name ?? live.sensor_id} · {live.sensor_id}
        </span>
      </div>
      {live.message ? (
        <div className="mono mb-1 text-[11px] text-console-warn">{live.message}</div>
      ) : null}
      <dl className="mono grid grid-cols-[110px_1fr] gap-x-2 gap-y-0.5 text-[11px]">
        <Row label="last frame">{fmtAgo(live.last_frame_age_s)}</Row>
        <Row label="frame rate">{fmtRate(live.frame_rate_hz)}</Row>
        <Row label="obs rate">{fmtRate(live.observation_rate_hz)}</Row>
        <Row label="frames parsed">{live.frames_parsed}</Row>
        <Row label="frames rejected">{live.frames_rejected}</Row>
        <Row label="frames duplicate">{live.frames_duplicate}</Row>
        <Row label="overflow drops">{live.observations_dropped_overflow}</Row>
        <Row label="reconnects">{live.reconnect_count}</Row>
        <Row label="generation">{live.generation}</Row>
        {live.capture_path ? <Row label="capture">{live.capture_path}</Row> : null}
      </dl>
    </div>
  );
}
