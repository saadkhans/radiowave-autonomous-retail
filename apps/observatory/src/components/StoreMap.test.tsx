import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StoreMapView } from "@/components/StoreMap";
import { episodeTrail } from "@/lib/geometry";
import { DEFAULT_LAYERS } from "@/state/store";
import { item, person, STORE } from "@/test/fixtures";

function renderMap(props: Partial<React.ComponentProps<typeof StoreMapView>> = {}) {
  const onSelect = vi.fn();
  render(
    <StoreMapView
      store={STORE}
      persons={[]}
      items={[]}
      layers={DEFAULT_LAYERS}
      selection={null}
      width={1000}
      height={500}
      onSelect={onSelect}
      {...props}
    />,
  );
  return { onSelect };
}

describe("StoreMapView", () => {
  it("scales the floor from the twin bounds and draws zones, fixtures and boundaries", () => {
    renderMap();
    const svg = screen.getByTestId("store-map");
    // 10 m x 5 m floor in a 1000 x 500 viewport with 24 px padding: height-limited -> 90.4 px/m
    expect(Number(svg.getAttribute("data-scale"))).toBeCloseTo((500 - 48) / 5, 1);
    expect(screen.getByTestId("fixture-F1")).toBeInTheDocument();
    expect(screen.getByTestId("zone-entry")).toBeInTheDocument();
    expect(screen.getByTestId("boundary-exit")).toBeInTheDocument();
    expect(screen.queryByTestId("sensor-radar-1")).not.toBeInTheDocument(); // sensors off by default
  });

  it("projects world coordinates with north up", () => {
    renderMap({ persons: [person({ x: 5, y: 2.5 })], items: [item({ x: 5, y: 4 })] });
    const shopper = screen.getByTestId("person-P0001");
    const marker = shopper.querySelectorAll("circle")[0];
    expect(Number(marker.getAttribute("cx"))).toBeCloseTo(500, 0);
    expect(Number(marker.getAttribute("cy"))).toBeCloseTo(250, 0);
    const shirt = screen.getByTestId("item-00A001");
    const polygon = shirt.querySelector("polygon")!;
    const yOfItem = Number(polygon.getAttribute("points")!.split(" ")[1].split(",")[1]);
    expect(yOfItem).toBeLessThan(250); // y = 4 m is north of y = 2.5 m, therefore higher on screen
  });

  it("exposes person and item states and draws the carrier link", () => {
    renderMap({
      persons: [person({ state: "LOST" })],
      items: [item({ state: "CARRIED", carrier_track_id: "P0001" })],
    });
    expect(screen.getByTestId("person-P0001")).toHaveAttribute("data-state", "LOST");
    expect(screen.getByTestId("item-00A001")).toHaveAttribute("data-state", "CARRIED");
    expect(screen.getByTestId("carrier-line-00A001")).toBeInTheDocument();
  });

  it("draws one candidate line per ranked candidate when no carrier is assigned", () => {
    renderMap({
      persons: [person({ track_id: "P0001", x: 4 }), person({ track_id: "P0002", x: 6 })],
      items: [
        item({
          state: "INTERACTION_CANDIDATE",
          candidates: [
            { track_id: "P0001", score: 0.6, features: [], raw_features: {} },
            { track_id: "P0002", score: 0.55, features: [], raw_features: {} },
          ],
        }),
      ],
    });
    expect(screen.getByTestId("candidate-line-00A001-P0001")).toBeInTheDocument();
    expect(screen.getByTestId("candidate-line-00A001-P0002")).toBeInTheDocument();
  });

  it("honours layer toggles and selection callbacks", () => {
    const { onSelect } = renderMap({
      layers: { ...DEFAULT_LAYERS, fixtures: false, sensors: true },
      persons: [person()],
      items: [item()],
    });
    expect(screen.queryByTestId("fixture-F1")).not.toBeInTheDocument();
    expect(screen.getByTestId("sensor-radar-1")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("person-P0001"));
    expect(onSelect).toHaveBeenCalledWith({ kind: "person", id: "P0001" });
    fireEvent.click(screen.getByTestId("item-00A001"));
    expect(onSelect).toHaveBeenCalledWith({ kind: "item", id: "3034F0000000000000A001" });
    fireEvent.click(screen.getByTestId("store-map"));
    expect(onSelect).toHaveBeenLastCalledWith(null);
  });

  it("draws the uncertainty disc at metric scale without a pixel floor", () => {
    renderMap({ persons: [person({ sigma_m: 0.02 })] });
    const disc = screen.getByTestId("sigma-P0001");
    const scale = Number(screen.getByTestId("store-map").getAttribute("data-scale"));
    expect(Number(disc.getAttribute("r"))).toBeCloseTo(0.02 * scale, 3);
    expect(Number(disc.getAttribute("r"))).toBeLessThan(6);
  });

  it("trims a moving item's trail to its movement episode", () => {
    const trail = [
      { t_s: 1, x: 5, y: 4 },
      { t_s: 2, x: 5.1, y: 4.1 },
      { t_s: 8, x: 5.5, y: 3 },
      { t_s: 9, x: 6, y: 2.5 },
    ];
    expect(episodeTrail(item({ trail, movement_start_s: 8 })).map((p) => p.t_s)).toEqual([8, 9]);
    expect(episodeTrail(item({ trail, movement_start_s: null }))).toHaveLength(4);
    // A settled (MISPLACED) item has no movement_start_s but keeps its episode start.
    expect(episodeTrail(item({ trail, movement_start_s: null, episode_start_s: 8 })).map((p) => p.t_s)).toEqual([8, 9]);
  });

  it("skips items that are not localized", () => {
    renderMap({ items: [item({ state: "UNKNOWN", x: null, y: null })] });
    expect(screen.queryByTestId("item-00A001")).not.toBeInTheDocument();
  });
});
