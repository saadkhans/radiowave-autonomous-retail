import { decisionColor, featureLabel, fmtMeters, fmtScore, fmtSeconds, itemColor, personColor } from "@/lib/format";
import { useActions, useObservatory } from "@/state/store";
import type { Item, ItemDecision, Person } from "@/types/api";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <dt className="text-console-muted">{label}</dt>
      <dd className="mono truncate">{children}</dd>
    </>
  );
}

function DecisionBlock({ pending }: { pending: ItemDecision }) {
  return (
    <div
      className="mt-2 rounded border px-2 py-1.5"
      style={{ borderColor: decisionColor(pending.decision) }}
      data-testid="decision-block"
    >
      <div className="flex items-center gap-2">
        <span className="badge" style={{ background: "#0f1216", color: decisionColor(pending.decision) }}>
          {pending.decision}
        </span>
        <span className="mono text-[11px]">{pending.event_type}</span>
        <span className="mono text-[10px] text-console-muted">{pending.event_id}</span>
      </div>
      <dl className="mono mt-1 grid grid-cols-[90px_1fr] gap-x-2 text-[11px]">
        <dt className="text-console-muted">confidence</dt>
        <dd>{fmtScore(pending.confidence)}</dd>
        <dt className="text-console-muted">margin</dt>
        <dd>{fmtScore(pending.margin)}</dd>
        {pending.decision !== "COMMIT" ? (
          <>
            <dt className="text-console-muted">pending for</dt>
            <dd>{fmtSeconds(pending.waited_s)}</dd>
          </>
        ) : null}
        <dt className="text-console-muted">evaluated</dt>
        <dd>{fmtSeconds(pending.at_s)}</dd>
        {pending.reason ? (
          <>
            <dt className="text-console-muted">reason</dt>
            <dd className="whitespace-normal break-words">{pending.reason}</dd>
          </>
        ) : null}
      </dl>
    </div>
  );
}

function PersonDetails({ person, items }: { person: Person; items: Item[] }) {
  const { select } = useActions();
  const candidateItems = items.filter((item) =>
    item.candidates.some((candidate) => candidate.track_id === person.track_id),
  );
  return (
    <div className="px-3 py-2" data-testid="person-details">
      <div className="mb-2 flex items-center gap-2">
        <span className="mono text-sm" style={{ color: personColor(person.state) }}>
          ● {person.track_id}
        </span>
        <span className="badge" style={{ background: "#0f1216", color: personColor(person.state) }}>
          {person.state}
        </span>
      </div>
      <dl className="grid grid-cols-[110px_1fr] gap-x-2 gap-y-0.5 text-[12px]">
        <Row label="session">{person.session_id ?? "—"}</Row>
        <Row label="position">
          ({person.x.toFixed(2)}, {person.y.toFixed(2)}) m ± {person.sigma_m.toFixed(2)}
        </Row>
        <Row label="velocity">
          ({person.vx.toFixed(2)}, {person.vy.toFixed(2)}) m/s · {person.speed.toFixed(2)} m/s
          {person.heading_deg !== null ? ` · ${person.heading_deg.toFixed(0)}°` : ""}
        </Row>
        <Row label="confidence">{fmtScore(person.confidence)}</Row>
        <Row label="observations">{person.observation_count}</Row>
        <Row label="created">{fmtSeconds(person.created_s)}</Row>
        <Row label="updated">{fmtSeconds(person.updated_s)}</Row>
        <Row label="cart">{person.cart_id ?? "—"}</Row>
        <Row label="provenance">{person.sensor_ids.join(", ") || "—"}</Row>
      </dl>
      <div className="panel-title mb-1 mt-3">Carried items</div>
      {person.carried_epcs.length === 0 ? (
        <div className="mono text-[11px] text-console-muted">none</div>
      ) : (
        <ul className="mono text-[11px]">
          {person.carried_epcs.map((epc) => (
            <li key={epc}>
              <button type="button" className="hover:underline" onClick={() => select({ kind: "item", id: epc })}>
                {epc.slice(-6)}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="panel-title mb-1 mt-3">Candidate items</div>
      {candidateItems.length === 0 ? (
        <div className="mono text-[11px] text-console-muted">none</div>
      ) : (
        <ul className="mono text-[11px]">
          {candidateItems.map((item) => {
            const score = item.candidates.find((candidate) => candidate.track_id === person.track_id)?.score;
            return (
              <li key={item.epc} className="flex gap-2">
                <button type="button" className="hover:underline" onClick={() => select({ kind: "item", id: item.epc })}>
                  {item.short_epc}
                </button>
                <span className="text-console-muted">{item.state}</span>
                <span>{fmtScore(score)}</span>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function ItemDetails({ item }: { item: Item }) {
  const { select } = useActions();
  const featureNames = item.candidates[0]?.features.map((feature) => feature.name) ?? [];
  return (
    <div className="px-3 py-2" data-testid="item-details">
      <div className="mb-2 flex items-center gap-2">
        <span className="mono text-sm" style={{ color: itemColor(item.state) }}>
          ◆ {item.short_epc}
        </span>
        <span className="badge" style={{ background: "#0f1216", color: itemColor(item.state) }}>
          {item.state}
        </span>
      </div>
      <dl className="grid grid-cols-[110px_1fr] gap-x-2 gap-y-0.5 text-[12px]">
        <Row label="epc">{item.epc}</Row>
        <Row label="gtin">{item.gtin ?? "—"}</Row>
        <Row label="product">
          {item.product_name ?? "—"}
          {item.sku ? ` (${item.sku})` : ""}
        </Row>
        <Row label="home fixture">{item.home_fixture_id ?? "—"}</Row>
        <Row label="zone">{item.zone_id ?? "—"}</Row>
        <Row label="position">
          {item.x !== null && item.y !== null
            ? `(${item.x.toFixed(2)}, ${item.y.toFixed(2)}) m ± ${fmtMeters(item.sigma_m)}`
            : "not localized"}
        </Row>
        <Row label="state since">{fmtSeconds(item.state_since_s)}</Row>
        <Row label="movement start">{fmtSeconds(item.movement_start_s)}</Row>
        <Row label="last seen">{fmtSeconds(item.last_seen_s)}</Row>
        <Row label="observations">{item.observation_count}</Row>
        <Row label="carrier">
          {item.carrier_track_id ? (
            <button
              type="button"
              className="text-console-accent hover:underline"
              onClick={() => select({ kind: "person", id: item.carrier_track_id! })}
            >
              {item.carrier_track_id}
            </button>
          ) : (
            "—"
          )}
        </Row>
      </dl>
      {item.decision ? <DecisionBlock pending={item.decision} /> : null}
      <div className="panel-title mb-1 mt-3">Candidate ranking</div>
      {item.candidates.length === 0 ? (
        <div className="mono text-[11px] text-console-muted">no candidates</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="mono w-full text-[11px]" data-testid="candidate-table">
            <thead className="text-console-muted">
              <tr className="text-left">
                <th className="pr-2 font-normal">#</th>
                <th className="pr-2 font-normal">track</th>
                <th className="pr-2 font-normal">score</th>
                {featureNames.map((name) => (
                  <th key={name} className="pr-2 font-normal">
                    {featureLabel(name)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {item.candidates.map((candidate, index) => (
                <tr key={candidate.track_id} className={index === 0 ? "text-console-text" : "text-console-muted"}>
                  <td className="pr-2">{index + 1}</td>
                  <td className="pr-2">
                    <button
                      type="button"
                      className="hover:underline"
                      onClick={() => select({ kind: "person", id: candidate.track_id })}
                    >
                      {candidate.track_id}
                    </button>
                  </td>
                  <td className="pr-2">{fmtScore(candidate.score)}</td>
                  {candidate.features.map((feature) => (
                    <td key={feature.name} className="pr-2" title={feature.weight !== null ? `w=${feature.weight}` : "unavailable"}>
                      {feature.score === null ? "—" : feature.score.toFixed(2)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {item.candidates.length > 1 ? (
            <div className="mono mt-1 text-[10px] text-console-muted">
              margin top-1 vs top-2: {fmtScore(item.candidates[0].score - item.candidates[1].score)}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

export function Inspector() {
  const { run, selection } = useObservatory();
  const person = selection?.kind === "person" ? run?.persons.find((p) => p.track_id === selection.id) : undefined;
  const item = selection?.kind === "item" ? run?.items.find((i) => i.epc === selection.id) : undefined;

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="inspector">
      <div className="panel-title border-b border-console-line px-3 py-2">Inspector</div>
      <div className="scroll-y min-h-0 flex-1">
        {person ? <PersonDetails person={person} items={run?.items ?? []} /> : null}
        {item ? <ItemDetails item={item} /> : null}
        {!person && !item ? (
          <div className="px-3 py-2 text-console-muted">
            {run
              ? "Click a shopper, item, cart line, or event row to inspect it."
              : "Run a scenario to populate the inspector."}
            {run ? (
              <div className="mono mt-3 grid grid-cols-[150px_1fr] gap-y-0.5 text-[11px]">
                <span>persons</span>
                <span>{run.persons.length}</span>
                <span>items</span>
                <span>{run.items.length}</span>
                <span>sessions</span>
                <span>{run.sessions.length}</span>
                <span>observations</span>
                <span>{run.counters.accepted} accepted</span>
                <span>dropped dup</span>
                <span>{run.counters.dropped_duplicates}</span>
                <span>out of order</span>
                <span>{run.counters.out_of_order}</span>
                <span>rejected sensor</span>
                <span>{run.counters.rejected_unknown_sensor}</span>
                <span>rejected low conf</span>
                <span>{run.counters.rejected_low_confidence}</span>
                <span>rejected foreign</span>
                <span>{run.counters.rejected_foreign_scenario}</span>
                <span>rejected spatial</span>
                <span>{run.counters.rejected_spatially_inconsistent}</span>
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
