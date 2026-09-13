// Mirror of radiowave/api/viewmodels.py. Times are seconds since the scenario
// epoch; coordinates are store-frame meters; identities are canonical
// (person track id, session id, EPC, GTIN). Sensor ids are provenance only.

export interface Bounds {
  min_x: number;
  min_y: number;
  max_x: number;
  max_y: number;
}

export interface StoreZone {
  zone_id: string;
  name: string;
  kind: "SALES_FLOOR" | "FIXTURE" | "ENTRY" | "EXIT" | "FITTING_ROOM" | "BACK_OF_HOUSE";
  bounds: Bounds;
}

export interface StoreFixture {
  fixture_id: string;
  zone_id: string;
  name: string;
  bounds: Bounds;
}

export interface StoreBoundary {
  boundary_id: string;
  kind: "ENTRY" | "EXIT";
  bounds: Bounds;
}

export interface StoreSensor {
  sensor_id: string;
  modality: "MMWAVE" | "RFID" | "VISION";
  x: number;
  y: number;
  z: number;
  yaw: number;
  name: string | null;
}

export interface StoreProduct {
  gtin: string;
  name: string;
  sku: string | null;
  home_fixture_id: string | null;
}

export interface CatalogItem {
  epc: string;
  short_epc: string;
  gtin: string;
  product_name: string;
  home_fixture_id: string | null;
}

export interface ObservatoryStore {
  store_id: string;
  name: string;
  units: string;
  floor: Bounds;
  zones: StoreZone[];
  fixtures: StoreFixture[];
  boundaries: StoreBoundary[];
  sensors: StoreSensor[];
  products: StoreProduct[];
  items: CatalogItem[];
}

export interface TrailPoint {
  t_s: number;
  x: number;
  y: number;
}

export interface CandidateFeature {
  name: string;
  score: number | null;
  weight: number | null;
}

export interface Candidate {
  track_id: string;
  score: number;
  features: CandidateFeature[];
  raw_features: Record<string, number>;
}

export type DecisionKind = "COMMIT" | "WAIT" | "REVIEW";

export interface ItemDecision {
  event_id: string;
  event_type: string;
  decision: DecisionKind;
  confidence: number;
  margin: number;
  waited_s: number;
  reason: string;
  at_s: number;
}

export type PersonState = "ACTIVE" | "LOST" | "ENDED";

export interface Person {
  track_id: string;
  session_id: string | null;
  state: PersonState;
  x: number;
  y: number;
  vx: number;
  vy: number;
  speed: number;
  heading_deg: number | null;
  confidence: number;
  sigma_m: number;
  observation_count: number;
  created_s: number;
  updated_s: number;
  sensor_ids: string[];
  cart_id: string | null;
  carried_epcs: string[];
  trail: TrailPoint[];
}

export type ItemState =
  | "ON_FIXTURE"
  | "INTERACTION_CANDIDATE"
  | "CARRIED"
  | "MISPLACED"
  | "EXITED"
  | "UNKNOWN";

export interface Item {
  epc: string;
  short_epc: string;
  gtin: string | null;
  product_name: string | null;
  sku: string | null;
  home_fixture_id: string | null;
  state: ItemState;
  state_since_s: number | null;
  x: number | null;
  y: number | null;
  sigma_m: number | null;
  zone_id: string | null;
  carrier_track_id: string | null;
  movement_start_s: number | null;
  /** When the item last left its fixture; survives settling as MISPLACED/EXITED. */
  episode_start_s: number | null;
  last_seen_s: number | null;
  observation_count: number;
  candidates: Candidate[];
  /** Latest COMMIT / WAIT / REVIEW decision for the item's current episode. */
  decision: ItemDecision | null;
  trail: TrailPoint[];
}

export interface CartLine {
  epc: string;
  short_epc: string;
  gtin: string | null;
  product_name: string | null;
  added_s: number;
  final_ownership_candidate: boolean;
  exit_event_s: number | null;
}

export interface Cart {
  cart_id: string;
  shopper_track_id: string;
  session_id: string | null;
  status: "OPEN" | "EXITED";
  exited_s: number | null;
  lines: CartLine[];
}

export interface Unresolved {
  epc: string;
  short_epc: string;
  gtin: string | null;
  product_name: string | null;
  reason: string;
  source_event_id: string;
  t_s: number;
}

export interface Session {
  session_id: string;
  track_id: string;
  state: "ACTIVE" | "EXITED" | "ABANDONED";
  entered_s: number;
  exited_s: number | null;
  entry_boundary_id: string | null;
  exit_boundary_id: string | null;
}

export type EventKind = "RETAIL_EVENT" | "ITEM_TRANSITION" | "PERSON_TRACK" | "SESSION";

export interface ObservatoryEvent {
  seq: number;
  t_s: number;
  kind: EventKind;
  label: string;
  epc: string | null;
  shopper_track_id: string | null;
  counterpart_track_id: string | null;
  confidence: number | null;
  margin: number | null;
  decision: DecisionKind | null;
  reason: string;
  event_id: string | null;
  from_state: string | null;
  to_state: string | null;
}

export interface Counters {
  accepted: number;
  dropped_duplicates: number;
  out_of_order: number;
  rejected_unknown_sensor: number;
  rejected_low_confidence: number;
  rejected_foreign_scenario: number;
  rejected_spatially_inconsistent: number;
}

export interface GroundTruth {
  t_s: number;
  event_type: string;
  epc: string;
  shopper_label: string | null;
  counterpart_label: string | null;
}

export interface ScenarioSummary {
  scenario_id: string;
  name: string;
  description: string;
  duration_s: number;
  seed: number;
  shopper_count: number;
  item_count: number;
  vision_enabled: boolean;
  radar_dropouts: number;
  rfid_dropouts: number;
  ground_truth: GroundTruth[];
}

export interface ScenarioDetail extends ScenarioSummary {
  store: ObservatoryStore;
}

export interface RunState {
  run_id: string;
  /** Incremented by every mutation; equal across one atomic snapshot. */
  revision: number;
  /** Incremented when replay is rebuilt; event sequence numbers live within one epoch. */
  epoch: number;
  scenario_id: string;
  scenario_name: string;
  seed: number;
  time_s: number;
  duration_s: number;
  step_interval_s: number;
  steps: number;
  finished: boolean;
  observations_cursor: number;
  observations_total: number;
  events_total: number;
  persons: Person[];
  items: Item[];
  carts: Cart[];
  /** Committed physical events the cart engine could not attribute to a cart. */
  unresolved: Unresolved[];
  sessions: Session[];
  counters: Counters;
}

export interface TimelineMarker {
  t_s: number;
  label: string;
  epc: string | null;
  shopper_track_id: string | null;
  decision: string | null;
}

export interface Timeline {
  run_id: string;
  time_s: number;
  duration_s: number;
  markers: TimelineMarker[];
  ground_truth: GroundTruth[];
}

/** State, events and timeline captured under one lock at one revision. */
export interface Snapshot {
  state: RunState;
  events: EventPage;
  timeline: Timeline;
}

export interface EventPage {
  run_id: string;
  epoch: number;
  events: ObservatoryEvent[];
  next_seq: number;
  total: number;
}
