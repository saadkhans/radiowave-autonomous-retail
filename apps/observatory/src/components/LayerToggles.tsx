import { useActions, useObservatory, type Layers } from "@/state/store";

const LAYER_LABELS: Array<[keyof Layers, string]> = [
  ["zones", "Zones"],
  ["fixtures", "Fixtures"],
  ["sensors", "Sensors"],
  ["grid", "Grid"],
  ["trails", "Trajectories"],
  ["shopperLabels", "Shopper labels"],
  ["epcLabels", "EPC labels"],
  ["associations", "Candidate lines"],
  ["confidenceLabels", "Confidence labels"],
];

export function LayerToggles() {
  const { layers } = useObservatory();
  const { toggleLayer } = useActions();
  return (
    <div className="panel">
      <div className="panel-title border-b border-console-line px-3 py-2">Layers</div>
      <div className="grid grid-cols-2 gap-x-2 gap-y-1 px-3 py-2">
        {LAYER_LABELS.map(([key, label]) => (
          <label key={key} className="flex cursor-pointer items-center gap-2 text-[12px]">
            <input
              type="checkbox"
              checked={layers[key]}
              onChange={() => toggleLayer(key)}
              className="accent-console-accent"
            />
            {label}
          </label>
        ))}
      </div>
    </div>
  );
}
