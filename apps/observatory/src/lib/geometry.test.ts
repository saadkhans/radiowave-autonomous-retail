import { describe, expect, it } from "vitest";

import { gridLines, makeProjector, rectFor } from "@/lib/geometry";

const FLOOR = { min_x: 0, min_y: 0, max_x: 12, max_y: 8 };

describe("makeProjector", () => {
  it("fits the floor into the viewport preserving aspect ratio", () => {
    const projector = makeProjector(FLOOR, 1200, 800, 0);
    expect(projector.scale).toBeCloseTo(100);
    const wide = makeProjector(FLOOR, 2400, 800, 0);
    expect(wide.scale).toBeCloseTo(100); // height-limited
    const tall = makeProjector(FLOOR, 1200, 1600, 0);
    expect(tall.scale).toBeCloseTo(100); // width-limited
  });

  it("puts north (max y) at the top of the screen", () => {
    const projector = makeProjector(FLOOR, 1200, 800, 0);
    expect(projector.toScreen(0, 8)).toEqual({ x: 0, y: 0 });
    expect(projector.toScreen(12, 0)).toEqual({ x: 1200, y: 800 });
    expect(projector.toScreen(6, 4)).toEqual({ x: 600, y: 400 });
  });

  it("centres the drawn floor inside the padding", () => {
    const projector = makeProjector(FLOOR, 1240, 1000, 20);
    expect(projector.scale).toBeCloseTo(100);
    expect(projector.toScreen(0, 8)).toEqual({ x: 20, y: 100 });
  });

  it("round-trips world <-> screen", () => {
    const projector = makeProjector({ min_x: -3, min_y: 2, max_x: 9, max_y: 10 }, 640, 480, 12);
    const world = projector.toWorld(projector.toScreen(1.5, 7.25).x, projector.toScreen(1.5, 7.25).y);
    expect(world.x).toBeCloseTo(1.5);
    expect(world.y).toBeCloseTo(7.25);
    expect(projector.meters(projector.pixels(2.5))).toBeCloseTo(2.5);
  });

  it("does not divide by zero on degenerate bounds or viewports", () => {
    const projector = makeProjector({ min_x: 0, min_y: 0, max_x: 0, max_y: 0 }, 0, 0);
    expect(Number.isFinite(projector.scale)).toBe(true);
  });
});

describe("rectFor", () => {
  it("returns a screen rectangle with positive size for a world box", () => {
    const projector = makeProjector(FLOOR, 1200, 800, 0);
    const rect = rectFor(projector, { min_x: 2, min_y: 1, max_x: 5, max_y: 3 });
    expect(rect).toEqual({ x: 200, y: 500, width: 300, height: 200 });
  });
});

describe("gridLines", () => {
  it("emits whole-metre lines within the floor", () => {
    expect(gridLines({ min_x: 0.5, min_y: -1.5, max_x: 3.2, max_y: 1 })).toEqual({ xs: [1, 2, 3], ys: [-1, 0, 1] });
  });
});
