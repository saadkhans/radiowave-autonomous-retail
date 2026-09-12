# Observatory v0 — visual engineering console

Observatory v0 is the first visual frontend over Foundation v0. It exists so an
engineer can watch the fusion engine reason: where the twin thinks each shopper
and EPC is, which candidates are competing for an item, why a PICK is still in
WAIT, and what ended up in each virtual cart. It is deliberately not a checkout,
POS, payment, admin or hardware dashboard.

## Layout

```
radiowave/api/                 thin local API over the existing pipeline
  app.py                       create_app(): FastAPI app, CORS for the Vite dev server
  runs.py                      RunManager + ObservatoryRun (advance / step / seek / reset / state / events / timeline)
  viewmodels.py                explicit Pydantic view models (extra="forbid")
  routes/scenarios.py          GET /api/scenarios, /api/scenarios/{id}, /api/scenarios/{id}/store
  routes/runs.py               POST /api/runs, reset|step|advance|seek, GET state|store|events|timeline
apps/observatory/              React 19 + TypeScript + Vite 7 + Tailwind 4 (pnpm workspace package)
  src/types/api.ts             TypeScript mirror of the view models
  src/lib/api.ts               fetch wrapper over /api
  src/lib/geometry.ts          world (metres, y north) -> SVG (pixels, y down) projector
  src/state/store.tsx          reducer + context + deterministic playback loop
  src/components/StoreMap.tsx  SVG map: floor, grid, zones, fixtures, boundaries, sensors, trails, markers
  src/components/Inspector.tsx shopper / EPC details, candidate table, decision block
  src/components/CartPanel.tsx open and exited virtual carts
  src/components/EventStream.tsx unified event table with filters and selection
  src/components/ReplayControls.tsx, Timeline.tsx, ScenarioSelector.tsx, LayerToggles.tsx
```

## Time model

The API owns simulated time. A run wraps a `FoundationPipeline` plus the
pre-generated observation list for its scenario:

* `advance(seconds)` ingests every observation with `timestamp <= now + seconds`,
  then calls `pipeline.advance_to(target)` so fusion steps run even with no
  observations. The run finishes when the scenario duration is reached and every
  observation has been consumed.
* `step()` is `advance(step_interval_s)`.
* `seek(t)` moving forward is `advance(t - now)`; moving backwards resets the
  run and advances from t = 0. Two runs of the same scenario advanced to the same
  time produce identical state and event streams whatever the step sizes were
  (`tests/api/test_observatory_api.py`).
* Finalization calls `pipeline.finish(advance=False)`: the clock has already
  stepped through the duration, so nothing is evaluated past the advertised end.
  Input that arrived after the last step (a sample stamped exactly on the end, or
  a final partial interval) is evaluated once more at the duration itself.
* Every mutating route runs `ObservatoryRun.apply(operation)`, which performs the
  mutation and takes the snapshot under one lock; FastAPI serves sync handlers
  from worker threads and a response must describe its own request. Each
  mutation bumps `revision` and returns the complete snapshot (state, the full
  event stream and the timeline) captured under that same lock, and so does
  `POST /runs`; `GET /runs/{id}/snapshot` serves the same shape for a fresh
  read. The UI publishes only such snapshots, never independently fetched
  pieces.
* `FoundationPipeline.advance_to()` normalizes its clock through the same
  `ensure_utc` rule as every contract: naive timestamps are rejected. Every
  explicit advance is recorded as a `CLOCK` entry and the end of a run as
  `RUN_END` (with its `advance` flag), and `FoundationPipeline.replay(entries)`
  (also the CLI `replay` command) honours both, so a recording driven through a
  trailing dropout replays to the identical result.
* `reset` (and a backward seek) bumps `epoch`; event sequence numbers are only
  comparable within one epoch and `GET /events?since=&epoch=` restarts at 0
  when the epoch is stale.
* Scenarios flagged `duplicate_observation_stream` (scenario 10) are fed
  `runner.scenario_observation_stream()`, i.e. every observation twice, so the
  dropped-duplicate counter shows the idempotency the scenario exists to prove.

The browser never advances the clock itself. While playing, a 200 ms wall-clock
interval asks the API for `speed × 0.2 s` of simulated time and re-renders the
returned state; a request in flight suppresses the next tick, so a slow API
slows playback instead of drifting.

## View models

`radiowave/api/viewmodels.py` is the contract between the two halves and is
mirrored field for field in `apps/observatory/src/types/api.ts`. Times are seconds
since the scenario epoch, coordinates are store-frame metres, and all identities
are canonical: person track id, session id, EPC, GTIN. Sensor ids appear only in
`Person.sensor_ids` and `Store.sensors` as provenance; vendor-native ids never
reach the UI.

`ObservatoryRunState` is a full snapshot (persons, items, carts, sessions,
counters) capped to the last 40 trail points per entity. Events are served as a
single chronological stream (`/events?since=<seq>`) that merges person-track
creation, item state transitions, retail decisions and session lifecycle, so
the UI never keeps its own copy of pipeline history.

Two derivations deserve a note:

* Each retail decision row is paired with the proposal that was current when it
  was evaluated (`_decisions_with_proposals`). Fusion re-proposes a WAITing event
  id as evidence evolves, possibly with a different top shopper; the history must
  show what was judged, not the final attribution.
* `Item.decision` is the latest decision for the item's current episode
  (movement start, or rest start), so a COMMIT or REVIEW stays inspectable after
  the event leaves the pipeline's pending set.
* Carts map to sessions through the pipeline's `cart_sessions` record, falling
  back to the n-th session of the track for a cart that never committed.
* `RunState.unresolved` lists committed physical events the cart engine could
  not attribute (for example a MISPLACE after an unattributed PICK), so every
  committed event reconciles with either a cart mutation or an exception.

## Rendering rules

* The projector derives its scale purely from the twin's floor bounds and the
  available pixels; nothing is hard-coded to the lab store. North (max y) is at
  the top.
* People are circles (fill = ACTIVE, dashed = LOST, hollow = ENDED) with a
  translucent uncertainty disc of radius `sigma_m` and a heading arrow scaled by
  speed. Items are diamonds coloured by state.
* A settled carrier gets one solid link. While the decision for an item is still
  WAIT or REVIEW, or no carrier is assigned, every ranked candidate gets a dashed
  link whose weight and opacity follow its score.
* Item trails are only drawn once the item is off its fixture and are trimmed to
  the movement episode (`Item.episode_start_s`: the state machine's actual
  movement start, remembered after the item settles, with the transition log's
  last departure from a resting state as fallback); on-fixture RFID jitter is
  noise.
* The event stream includes the initial `UNKNOWN -> ON_FIXTURE|MISPLACED`
  classification of every localized item (fusion logs it as a transition), and
  timeline markers cover COMMIT and terminal REVIEW decisions, never the
  repetitive intermediate WAIT rows.

## Not included

Real adapters, production camera pipelines, authentication, payments, POS,
settlement, customer app, persistence, brokers, WebSockets, mobile layouts, ML.
