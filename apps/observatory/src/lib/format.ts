import type { DecisionKind, ItemState, LiveState, PersonState } from "@/types/api";

export const fmtSeconds = (seconds: number | null | undefined): string =>
  seconds === null || seconds === undefined ? "—" : `${seconds.toFixed(2)}s`;

/** "0.3 s ago" style relative age, for LiveStatus.last_frame_age_s. */
export const fmtAgo = (seconds: number | null | undefined): string =>
  seconds === null || seconds === undefined ? "-" : `${seconds.toFixed(1)} s ago`;

export const fmtRate = (hz: number | null | undefined): string =>
  hz === null || hz === undefined ? "-" : `${hz.toFixed(1)} Hz`;

export const fmtMeters = (value: number | null | undefined, digits = 2): string =>
  value === null || value === undefined ? "—" : `${value.toFixed(digits)} m`;

export const fmtScore = (value: number | null | undefined): string =>
  value === null || value === undefined ? "—" : value.toFixed(2);

export const fmtPercent = (value: number | null | undefined): string =>
  value === null || value === undefined ? "—" : `${Math.round(value * 100)}%`;

export const personColor = (state: PersonState): string => {
  switch (state) {
    case "ACTIVE":
      return "#5ec8ff";
    case "LOST":
      return "#f2b84b";
    case "ENDED":
      return "#6b7683";
  }
};

export const itemColor = (state: ItemState): string => {
  switch (state) {
    case "ON_FIXTURE":
      return "#8b98a8";
    case "INTERACTION_CANDIDATE":
      return "#f2b84b";
    case "CARRIED":
      return "#5ad48f";
    case "MISPLACED":
      return "#ff8f5e";
    case "EXITED":
      return "#c792ff";
    case "UNKNOWN":
      return "#4d5866";
  }
};

export const decisionColor = (decision: DecisionKind | null | undefined): string => {
  switch (decision) {
    case "COMMIT":
      return "#5ad48f";
    case "WAIT":
      return "#f2b84b";
    case "REVIEW":
      return "#ff6b6b";
    default:
      return "#8b98a8";
  }
};

export const RETAIL_EVENT_TYPES = [
  "PICK",
  "CARRY",
  "PUTBACK",
  "MISPLACE",
  "HANDOFF",
  "EXIT_WITH_ITEM",
] as const;

export type RetailEventType = (typeof RETAIL_EVENT_TYPES)[number];

export const isRetailEventType = (label: string): label is RetailEventType =>
  (RETAIL_EVENT_TYPES as readonly string[]).includes(label);

export const liveStateColor = (state: LiveState | null | undefined): string => {
  switch (state) {
    case "STREAMING":
      return "#5ad48f";
    case "STALE":
      return "#f2b84b";
    case "ERROR":
      return "#ff6b6b";
    case "CONNECTING":
    case "DISCONNECTED":
    default:
      return "#8b98a8";
  }
};

export const featureLabel = (name: string): string =>
  ({
    distance: "distance",
    distance_trend: "dist. trend",
    velocity: "velocity",
    temporal: "temporal",
    co_motion: "co-motion",
    zone: "zone",
    vision: "vision",
  })[name] ?? name;
