import type { Bounds, Item } from "@/types/api";

/**
 * World (store frame, meters, y north) -> screen (pixels, y down) projection.
 *
 * The scale is derived purely from the store floor bounds and the available
 * viewport, so the same renderer works for every store twin.
 */
export interface Projector {
  scale: number; // pixels per meter
  originX: number; // screen x of world x = floor.min_x
  originY: number; // screen y of world y = floor.max_y (north edge at the top)
  width: number;
  height: number;
  floor: Bounds;
  toScreen(x: number, y: number): { x: number; y: number };
  toWorld(px: number, py: number): { x: number; y: number };
  meters(pixels: number): number;
  pixels(meters: number): number;
}

export function makeProjector(
  floor: Bounds,
  width: number,
  height: number,
  padding = 24,
): Projector {
  const worldWidth = Math.max(floor.max_x - floor.min_x, 1e-6);
  const worldHeight = Math.max(floor.max_y - floor.min_y, 1e-6);
  const usableWidth = Math.max(width - 2 * padding, 1);
  const usableHeight = Math.max(height - 2 * padding, 1);
  const scale = Math.min(usableWidth / worldWidth, usableHeight / worldHeight);
  const drawnWidth = worldWidth * scale;
  const drawnHeight = worldHeight * scale;
  const originX = padding + (usableWidth - drawnWidth) / 2;
  const originY = padding + (usableHeight - drawnHeight) / 2;
  return {
    scale,
    originX,
    originY,
    width,
    height,
    floor,
    toScreen(x, y) {
      return { x: originX + (x - floor.min_x) * scale, y: originY + (floor.max_y - y) * scale };
    },
    toWorld(px, py) {
      return { x: floor.min_x + (px - originX) / scale, y: floor.max_y - (py - originY) / scale };
    },
    meters(pixels) {
      return pixels / scale;
    },
    pixels(meters) {
      return meters * scale;
    },
  };
}

/** Screen rectangle for a world-frame box. */
export function rectFor(projector: Projector, bounds: Bounds) {
  const topLeft = projector.toScreen(bounds.min_x, bounds.max_y);
  const bottomRight = projector.toScreen(bounds.max_x, bounds.min_y);
  return {
    x: topLeft.x,
    y: topLeft.y,
    width: bottomRight.x - topLeft.x,
    height: bottomRight.y - topLeft.y,
  };
}

/** Whole-meter grid lines inside the floor bounds, for the background grid. */
export function gridLines(floor: Bounds): { xs: number[]; ys: number[] } {
  const xs: number[] = [];
  const ys: number[] = [];
  for (let x = Math.ceil(floor.min_x); x <= Math.floor(floor.max_x); x += 1) xs.push(x);
  for (let y = Math.ceil(floor.min_y); y <= Math.floor(floor.max_y); y += 1) ys.push(y);
  return { xs, ys };
}

/** Trail points since the item started moving; earlier points are on-fixture jitter. */
export function episodeTrail(item: Item): Item["trail"] {
  // movement_start_s is cleared once the item settles; episode_start_s survives.
  const since = item.episode_start_s ?? item.movement_start_s;
  return since === null ? item.trail : item.trail.filter((point) => point.t_s >= since);
}
