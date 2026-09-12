import { useEffect, useMemo, useRef, useState } from "react";

import { gridLines, makeProjector, rectFor, type Projector } from "@/lib/geometry";
import { decisionColor, fmtScore, itemColor, personColor } from "@/lib/format";
import { useActions, useObservatory, type Layers, type Selection } from "@/state/store";
import type { Item, ObservatoryStore, Person, StoreZone } from "@/types/api";

const ZONE_FILL: Record<StoreZone["kind"], string> = {
  SALES_FLOOR: "rgba(94, 200, 255, 0.03)",
  FIXTURE: "rgba(139, 152, 168, 0.06)",
  ENTRY: "rgba(90, 212, 143, 0.10)",
  EXIT: "rgba(199, 146, 255, 0.10)",
  FITTING_ROOM: "rgba(242, 184, 75, 0.08)",
  BACK_OF_HOUSE: "rgba(255, 107, 107, 0.06)",
};

const ZONE_STROKE: Record<StoreZone["kind"], string> = {
  SALES_FLOOR: "#2a333f",
  FIXTURE: "#3a4654",
  ENTRY: "#5ad48f",
  EXIT: "#c792ff",
  FITTING_ROOM: "#f2b84b",
  BACK_OF_HOUSE: "#ff6b6b",
};

/** Item trails are only drawn while an item is off its fixture; on-fixture RFID jitter is noise. */
const MOVING_ITEM_STATES = new Set<Item["state"]>(["INTERACTION_CANDIDATE", "CARRIED", "MISPLACED", "EXITED"]);

function useSize(ref: React.RefObject<HTMLDivElement | null>) {
  const [size, setSize] = useState({ width: 800, height: 600 });
  useEffect(() => {
    const node = ref.current;
    if (!node || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect && rect.width > 0 && rect.height > 0) {
        setSize({ width: rect.width, height: rect.height });
      }
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, [ref]);
  return size;
}

function trailPath(projector: Projector, trail: { x: number; y: number }[]): string {
  return trail
    .map((point, index) => {
      const { x, y } = projector.toScreen(point.x, point.y);
      return `${index === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

export interface StoreMapViewProps {
  store: ObservatoryStore;
  persons: Person[];
  items: Item[];
  layers: Layers;
  selection: Selection;
  width: number;
  height: number;
  onSelect(selection: Selection): void;
}

/** Pure SVG renderer: everything derives from world coordinates + floor bounds. */
export function StoreMapView({
  store,
  persons,
  items,
  layers,
  selection,
  width,
  height,
  onSelect,
}: StoreMapViewProps) {
  const projector = useMemo(() => makeProjector(store.floor, width, height), [store.floor, width, height]);
  const grid = useMemo(() => gridLines(store.floor), [store.floor]);
  const floor = rectFor(projector, store.floor);
  const personById = useMemo(() => new Map(persons.map((person) => [person.track_id, person])), [persons]);

  const selectedPersonId = selection?.kind === "person" ? selection.id : null;
  const selectedEpc = selection?.kind === "item" ? selection.id : null;

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className="select-none"
      data-testid="store-map"
      data-scale={projector.scale.toFixed(3)}
      onClick={() => onSelect(null)}
      role="img"
      aria-label={`Store map ${store.name}`}
    >
      <defs>
        <marker id="heading" viewBox="0 0 6 6" refX="3" refY="3" markerWidth="6" markerHeight="6" orient="auto">
          <path d="M0,0 L6,3 L0,6 z" fill="currentColor" />
        </marker>
      </defs>

      {/* floor */}
      <rect {...floor} fill="#12161b" stroke="#2a333f" strokeWidth={1} data-testid="floor" />

      {/* grid (1 m) */}
      {layers.grid ? (
        <g stroke="#1f2732" strokeWidth={0.5} data-testid="grid">
          {grid.xs.map((x) => {
            const sx = projector.toScreen(x, 0).x;
            return <line key={`gx${x}`} x1={sx} x2={sx} y1={floor.y} y2={floor.y + floor.height} />;
          })}
          {grid.ys.map((y) => {
            const sy = projector.toScreen(0, y).y;
            return <line key={`gy${y}`} x1={floor.x} x2={floor.x + floor.width} y1={sy} y2={sy} />;
          })}
        </g>
      ) : null}

      {/* zones */}
      {layers.zones
        ? store.zones.map((zone) => {
            const rect = rectFor(projector, zone.bounds);
            return (
              <g key={zone.zone_id} data-testid={`zone-${zone.zone_id}`}>
                <rect
                  {...rect}
                  fill={ZONE_FILL[zone.kind]}
                  stroke={ZONE_STROKE[zone.kind]}
                  strokeWidth={zone.kind === "SALES_FLOOR" ? 0 : 1}
                  strokeDasharray={zone.kind === "FIXTURE" ? "3 3" : undefined}
                />
                {zone.kind !== "SALES_FLOOR" ? (
                  <text
                    x={rect.x + 4}
                    y={rect.y + 11}
                    fontSize={9}
                    fontFamily="var(--font-mono)"
                    fill={ZONE_STROKE[zone.kind]}
                    opacity={0.8}
                  >
                    {zone.name}
                  </text>
                ) : null}
              </g>
            );
          })
        : null}

      {/* entry / exit boundaries */}
      {store.boundaries.map((boundary) => {
        const rect = rectFor(projector, boundary.bounds);
        const color = boundary.kind === "ENTRY" ? "#5ad48f" : "#c792ff";
        return (
          <g key={boundary.boundary_id} data-testid={`boundary-${boundary.boundary_id}`}>
            <rect {...rect} fill={color} opacity={0.18} />
            <rect {...rect} fill="none" stroke={color} strokeWidth={1.5} />
            <text
              x={rect.x + rect.width / 2}
              y={rect.y + rect.height / 2 + 3}
              textAnchor="middle"
              fontSize={9}
              fontFamily="var(--font-mono)"
              fill={color}
            >
              {boundary.kind}
            </text>
          </g>
        );
      })}

      {/* fixtures */}
      {layers.fixtures
        ? store.fixtures.map((fixture) => {
            const rect = rectFor(projector, fixture.bounds);
            return (
              <g key={fixture.fixture_id} data-testid={`fixture-${fixture.fixture_id}`}>
                <rect {...rect} fill="#222b36" stroke="#4a5666" strokeWidth={1} rx={2} />
                <text
                  x={rect.x + rect.width / 2}
                  y={rect.y + rect.height / 2 + 3}
                  textAnchor="middle"
                  fontSize={10}
                  fontFamily="var(--font-mono)"
                  fill="#8b98a8"
                >
                  {fixture.fixture_id}
                </text>
              </g>
            );
          })
        : null}

      {/* sensors (provenance only) */}
      {layers.sensors
        ? store.sensors.map((sensor) => {
            const { x, y } = projector.toScreen(sensor.x, sensor.y);
            const color = sensor.modality === "MMWAVE" ? "#5ec8ff" : sensor.modality === "RFID" ? "#f2b84b" : "#c792ff";
            return (
              <g key={sensor.sensor_id} data-testid={`sensor-${sensor.sensor_id}`}>
                <rect x={x - 3} y={y - 3} width={6} height={6} fill="none" stroke={color} strokeWidth={1} />
                <text x={x + 5} y={y + 3} fontSize={8} fontFamily="var(--font-mono)" fill={color} opacity={0.8}>
                  {sensor.sensor_id}
                </text>
              </g>
            );
          })
        : null}

      {/* association lines: carrier (solid) and candidates (dashed, weighted by score) */}
      {layers.associations
        ? items.map((item) => {
            if (item.x === null || item.y === null) return null;
            const from = projector.toScreen(item.x, item.y);
            const lines: React.ReactNode[] = [];
            // While a decision is still WAIT/REVIEW the attribution is uncertain: show every
            // ranked candidate. Only a settled carrier gets the single solid link.
            const undecided = item.decision !== null && item.decision.decision !== "COMMIT";
            if (item.carrier_track_id && !undecided) {
              const carrier = personById.get(item.carrier_track_id);
              if (carrier) {
                const to = projector.toScreen(carrier.x, carrier.y);
                lines.push(
                  <line
                    key={`carry-${item.epc}`}
                    x1={from.x}
                    y1={from.y}
                    x2={to.x}
                    y2={to.y}
                    stroke="#5ad48f"
                    strokeWidth={1.2}
                    opacity={0.7}
                    data-testid={`carrier-line-${item.short_epc}`}
                  />,
                );
              }
            } else {
              item.candidates.forEach((candidate) => {
                const person = personById.get(candidate.track_id);
                if (!person) return;
                const to = projector.toScreen(person.x, person.y);
                lines.push(
                  <line
                    key={`cand-${item.epc}-${candidate.track_id}`}
                    x1={from.x}
                    y1={from.y}
                    x2={to.x}
                    y2={to.y}
                    stroke="#f2b84b"
                    strokeWidth={0.6 + candidate.score * 1.6}
                    strokeDasharray="4 3"
                    opacity={0.25 + candidate.score * 0.6}
                    data-testid={`candidate-line-${item.short_epc}-${candidate.track_id}`}
                  />,
                );
              });
            }
            return lines;
          })
        : null}

      {/* item trails */}
      {layers.trails
        ? items.map((item) =>
            item.trail.length > 1 && MOVING_ITEM_STATES.has(item.state) ? (
              <path
                key={`itrail-${item.epc}`}
                d={trailPath(projector, item.trail)}
                fill="none"
                stroke={itemColor(item.state)}
                strokeWidth={1}
                opacity={0.35}
                strokeDasharray="2 2"
              />
            ) : null,
          )
        : null}

      {/* person trails */}
      {layers.trails
        ? persons.map((person) =>
            person.trail.length > 1 ? (
              <path
                key={`ptrail-${person.track_id}`}
                d={trailPath(projector, person.trail)}
                fill="none"
                stroke={personColor(person.state)}
                strokeWidth={1.2}
                opacity={0.45}
              />
            ) : null,
          )
        : null}

      {/* items */}
      {items.map((item) => {
        if (item.x === null || item.y === null) return null;
        const { x, y } = projector.toScreen(item.x, item.y);
        const color = itemColor(item.state);
        const selected = item.epc === selectedEpc;
        const size = selected ? 7 : 5;
        return (
          <g
            key={item.epc}
            data-testid={`item-${item.short_epc}`}
            data-state={item.state}
            className="cursor-pointer"
            onClick={(event) => {
              event.stopPropagation();
              onSelect({ kind: "item", id: item.epc });
            }}
          >
            {selected ? <circle cx={x} cy={y} r={11} fill="none" stroke="#ffffff" strokeWidth={1} opacity={0.8} /> : null}
            <polygon
              points={`${x},${y - size} ${x + size},${y} ${x},${y + size} ${x - size},${y}`}
              fill={color}
              stroke="#0f1216"
              strokeWidth={1}
              opacity={item.state === "ON_FIXTURE" ? 0.8 : 1}
            />
            {layers.epcLabels ? (
              <text x={x + 8} y={y - 6} fontSize={9} fontFamily="var(--font-mono)" fill={color}>
                {item.short_epc}
              </text>
            ) : null}
            {layers.confidenceLabels && item.decision ? (
              <text x={x + 8} y={y + 12} fontSize={8} fontFamily="var(--font-mono)" fill={decisionColor(item.decision.decision)}>
                {item.decision.decision} {item.decision.event_type} {fmtScore(item.decision.confidence)}
              </text>
            ) : null}
          </g>
        );
      })}

      {/* persons */}
      {persons.map((person) => {
        const { x, y } = projector.toScreen(person.x, person.y);
        const color = personColor(person.state);
        const selected = person.track_id === selectedPersonId;
        const uncertainty = projector.pixels(person.sigma_m); // metric, never inflated
        const ring = Math.max(uncertainty, 6) + 4;
        const headingLength = 10 + Math.min(person.speed, 2) * 6;
        const hx = person.speed > 0.05 ? x + (person.vx / person.speed) * headingLength : x;
        const hy = person.speed > 0.05 ? y - (person.vy / person.speed) * headingLength : y;
        return (
          <g
            key={person.track_id}
            data-testid={`person-${person.track_id}`}
            data-state={person.state}
            className="cursor-pointer"
            style={{ color }}
            onClick={(event) => {
              event.stopPropagation();
              onSelect({ kind: "person", id: person.track_id });
            }}
          >
            <circle cx={x} cy={y} r={uncertainty} fill={color} opacity={0.12} data-testid={`sigma-${person.track_id}`} />
            {selected ? <circle cx={x} cy={y} r={ring} fill="none" stroke="#ffffff" strokeWidth={1} /> : null}
            {person.speed > 0.05 ? (
              <line x1={x} y1={y} x2={hx} y2={hy} stroke={color} strokeWidth={1.5} markerEnd="url(#heading)" />
            ) : null}
            <circle
              cx={x}
              cy={y}
              r={6}
              fill={person.state === "ENDED" ? "none" : color}
              stroke={color}
              strokeWidth={1.5}
              strokeDasharray={person.state === "LOST" ? "2 2" : undefined}
            />
            {layers.shopperLabels ? (
              <text x={x + 10} y={y + 4} fontSize={10} fontFamily="var(--font-mono)" fill={color}>
                {person.track_id}
                {layers.confidenceLabels ? ` ${fmtScore(person.confidence)}` : ""}
              </text>
            ) : null}
          </g>
        );
      })}

      {/* scale bar */}
      <g transform={`translate(${floor.x}, ${floor.y + floor.height + 14})`}>
        <line x1={0} x2={projector.pixels(1)} y1={0} y2={0} stroke="#8b98a8" strokeWidth={1} />
        <text x={projector.pixels(1) + 4} y={3} fontSize={9} fontFamily="var(--font-mono)" fill="#8b98a8">
          1 m · {store.name} · {store.floor.max_x - store.floor.min_x}×{store.floor.max_y - store.floor.min_y} m
        </text>
      </g>
    </svg>
  );
}

export function StoreMap() {
  const { scenario, run, layers, selection } = useObservatory();
  const { select } = useActions();
  const containerRef = useRef<HTMLDivElement>(null);
  const size = useSize(containerRef);
  const store = scenario?.store;

  return (
    <div ref={containerRef} className="h-full w-full" data-testid="store-map-container">
      {store ? (
        <StoreMapView
          store={store}
          persons={run?.persons ?? []}
          items={run?.items ?? []}
          layers={layers}
          selection={selection}
          width={size.width}
          height={size.height}
          onSelect={select}
        />
      ) : (
        <div className="flex h-full items-center justify-center text-console-muted">
          Select a scenario to load its store twin.
        </div>
      )}
    </div>
  );
}
