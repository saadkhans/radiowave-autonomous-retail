import { useEffect, useMemo, useRef } from "react";

import { decisionColor, fmtScore, RETAIL_EVENT_TYPES } from "@/lib/format";
import { filterEvents, useActions, useObservatory, type EventFilter } from "@/state/store";

const FILTERS: EventFilter[] = ["ALL", ...RETAIL_EVENT_TYPES, "LIFECYCLE"];
const MAX_ROWS = 2000;

export function EventStream() {
  const { events, eventFilter, selection } = useObservatory();
  const { setEventFilter, select } = useActions();
  const bottomRef = useRef<HTMLDivElement>(null);

  const visible = useMemo(() => {
    const rows = filterEvents(events, eventFilter, selection);
    return rows.length > MAX_ROWS ? rows.slice(rows.length - MAX_ROWS) : rows;
  }, [events, eventFilter, selection]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [visible.length]);

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="event-stream">
      <div className="flex items-center gap-1 border-b border-console-line px-3 py-1.5">
        <span className="panel-title mr-2">Events</span>
        {FILTERS.map((filter) => (
          <button
            key={filter}
            type="button"
            className={`btn !px-2 !py-0.5 !text-[10px] ${eventFilter === filter ? "btn-active" : ""}`}
            onClick={() => setEventFilter(filter)}
            aria-pressed={eventFilter === filter}
          >
            {filter}
          </button>
        ))}
        <div className="flex-1" />
        {selection ? (
          <button type="button" className="btn !px-2 !py-0.5 !text-[10px]" onClick={() => select(null)}>
            {selection.kind === "person" ? "shopper" : "epc"} {selection.id.slice(-6)} ✕
          </button>
        ) : null}
        <span className="mono text-[10px] text-console-muted">
          {visible.length}/{events.length}
        </span>
      </div>
      <div className="scroll-y min-h-0 flex-1">
        <table className="mono w-full text-[11px]">
          <thead className="sticky top-0 bg-console-panel text-console-muted">
            <tr className="text-left">
              <th className="px-2 py-1 font-normal">t</th>
              <th className="px-2 py-1 font-normal">event</th>
              <th className="px-2 py-1 font-normal">epc</th>
              <th className="px-2 py-1 font-normal">shopper</th>
              <th className="px-2 py-1 font-normal">conf</th>
              <th className="px-2 py-1 font-normal">decision</th>
              <th className="px-2 py-1 font-normal">detail</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((event) => (
              <tr
                key={event.seq}
                className="cursor-pointer border-b border-console-line/40 hover:bg-console-panel-2"
                onClick={() => {
                  if (event.epc) select({ kind: "item", id: event.epc });
                  else if (event.shopper_track_id) select({ kind: "person", id: event.shopper_track_id });
                }}
              >
                <td className="px-2 py-0.5 text-console-muted">{event.t_s.toFixed(2)}</td>
                <td
                  className="px-2 py-0.5"
                  style={{ color: event.kind === "RETAIL_EVENT" ? decisionColor(event.decision) : undefined }}
                >
                  {event.label}
                </td>
                <td className="px-2 py-0.5">{event.epc ? event.epc.slice(-6) : "—"}</td>
                <td className="px-2 py-0.5">
                  {event.shopper_track_id ?? "—"}
                  {event.counterpart_track_id ? ` → ${event.counterpart_track_id}` : ""}
                </td>
                <td className="px-2 py-0.5">{fmtScore(event.confidence)}</td>
                <td className="px-2 py-0.5" style={{ color: decisionColor(event.decision) }}>
                  {event.decision ?? "—"}
                </td>
                <td className="max-w-[320px] truncate px-2 py-0.5 text-console-muted" title={event.reason}>
                  {event.from_state && event.to_state
                    ? `${event.from_state} → ${event.to_state}`
                    : event.reason}
                </td>
              </tr>
            ))}
            {visible.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-2 py-2 text-console-muted">
                  No events yet.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
