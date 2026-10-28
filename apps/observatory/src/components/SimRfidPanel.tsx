import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import { useObservatory } from "@/state/store";
import type { SimRfidBench, SimRfidItem } from "@/types/api";

export const SIM_RFID_POLL_MS = 1000;

const STATE_LABEL: Record<SimRfidItem["state"], string> = {
  ON_SHELF: "on shelf",
  CARRIED: "carried",
  GONE: "left the store",
};

/**
 * Operator buttons for simulated RFID on a hardware LIVE run: the radar is
 * real, the item reads come from a virtual reader. Hidden for replay, for
 * simulated (lab) runs, for finished runs, and for stores with no simulated
 * read point. Keeps its own small poll instead of joining the run snapshot so
 * it never changes the live polling contract.
 */
export function SimRfidPanel() {
  const { run } = useObservatory();
  const runId = run?.run_id ?? null;
  const eligible = run?.mode === "LIVE" && !run.finished && run.live?.simulated !== true;
  const [bench, setBench] = useState<SimRfidBench | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  useEffect(() => {
    setBench(null);
    setMessage(null);
    if (!eligible || runId === null) return;
    let cancelled = false;
    const load = async () => {
      try {
        const next = await api.simRfid(runId);
        if (!cancelled) setBench(next);
      } catch {
        // A run without the route (or a transient error) just shows nothing.
      }
    };
    void load();
    const timer = setInterval(() => void load(), SIM_RFID_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [eligible, runId]);

  const act = useCallback(
    async (action: "pick" | "put_back", epc: string) => {
      if (runId === null) return;
      setPending(epc);
      setMessage(null);
      try {
        setBench(await api.simRfidAction(runId, action, epc));
      } catch (error) {
        setMessage(error instanceof Error ? error.message : String(error));
      } finally {
        setPending(null);
      }
    },
    [runId],
  );

  if (!eligible || !bench?.enabled) return null;

  return (
    <div className="panel flex flex-col gap-2 px-3 py-2" data-testid="sim-rfid-panel">
      <div className="flex items-center gap-2">
        <div className="panel-title">Simulated RFID</div>
        <span className="mono rounded bg-[#c792ff]/20 px-1.5 py-0.5 text-[10px] tracking-widest text-[#c792ff]">
          VIRTUAL READER
        </span>
      </div>
      <div className="text-[11px] text-console-muted" data-testid="sim-rfid-note">
        Radar is real; item reads are simulated. A pick attaches the item to the tracked person
        nearest its shelf, so who-took-what here is your input, not the system&apos;s inference.
      </div>
      <ul className="flex flex-col gap-1">
        {bench.items.map((item) => (
          <li key={item.epc} className="flex items-center gap-2 text-[12px]" data-testid="sim-rfid-item">
            <span className="flex-1">
              {item.product_name ?? item.gtin}{" "}
              <span className="mono text-console-muted">{item.short_epc}</span>
            </span>
            <span className="mono text-[11px] text-console-muted">
              {item.state === "CARRIED" && item.carrier_track_id
                ? `carried by ${item.carrier_track_id}`
                : `${STATE_LABEL[item.state]}${item.state === "ON_SHELF" ? ` · ${item.home_fixture_name}` : ""}`}
            </span>
            {item.state === "ON_SHELF" ? (
              <button
                type="button"
                className="btn"
                disabled={pending === item.epc}
                onClick={() => void act("pick", item.epc)}
              >
                Pick up
              </button>
            ) : (
              <button
                type="button"
                className="btn"
                disabled={pending === item.epc}
                onClick={() => void act("put_back", item.epc)}
              >
                Put back
              </button>
            )}
          </li>
        ))}
      </ul>
      {message ? (
        <div className="mono text-[11px] text-console-danger" role="alert">
          {message}
        </div>
      ) : null}
    </div>
  );
}
