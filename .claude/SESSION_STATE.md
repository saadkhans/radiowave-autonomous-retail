# SESSION_STATE — crash / interruption recovery

**Purpose.** This file is the durable hand-off record for in-flight work on this repo. If a
session crashes, is compacted, or is resumed cold, **read this file first**, then run the
Recovery checklist below. It is the only place that records work that exists in the working
tree but is not yet in a commit — everything else (commits, PR threads, CI) is recoverable
from git and `gh`.

**Maintenance contract (for Claude).**
- Update the *Current state* section whenever the answer to "what would be lost if this
  session died right now?" changes: after applying a fix set, after running checks, after a
  commit/push, after posting a review comment.
- Keep it short and factual. No narrative. Dates absolute.
- When a phase/PR is finished and merged by the owner, collapse its section down to one line
  in *History* and start a fresh *Current state*.
- This file tracks **transient session state only**. Durable design rationale belongs in
  `docs/`, and cross-session working knowledge belongs in the Claude memory directory
  (`~/.claude/projects/C--Users-walee-downloads-radiowave-autonomous-retail/memory/`).

---

## Recovery checklist (run this on a cold start)

1. `git branch --show-current` and `git log --oneline -5` — compare against *Current state*.
2. `git status --short` and `git diff --stat` — uncommitted work is the part not recoverable
   from anywhere else; the *Uncommitted work* table below says what it is and why.
3. `gh pr view <PR#> --json state,statusCheckRollup` — is remote head == local head, is CI green.
4. `gh api repos/saadkhans/radiowave-autonomous-retail/pulls/<PR#>/reviews` — find the latest
   Codex review id, then pull its inline comments with
   `gh api repos/.../pulls/<PR#>/comments?per_page=100 --jq '.[] | select(.pull_request_review_id==<id>)'`.
   **Also read the review submission body** — a P1 can live there with no inline thread.
5. Resume at the step named in *Next step* below.

### Environment gotchas (cost real time when forgotten)
- `pnpm run lint|typecheck|test|build` call bare `python`; prefix
  `PATH="$PWD/.venv/Scripts:$HOME/.local/bin:$PATH"` or they fail on missing pydantic/ruff.
- Big multi-file heredocs in the Bash tool fail to parse; write patch scripts to the
  scratchpad with Write and run them.
- Required before any push: `pnpm run lint`, `pnpm run typecheck`, `pnpm run test`,
  `pnpm run build`, `pnpm run security:secrets`, plus `final-reviewer` returning `VERDICT: PASS`.
- Never commit to `main`/`dev`; never merge a PR (owner decision only).

---

## Current state — 2026-09-17

**Phase 4 — Virtual Store Lab v0.** Branch `claude/virtual-store-lab-v0` -> PR **#4**, stacked on
PR #3. **Never merge either.**

- Phase-4 HEAD `c0b9587`; base is PR #3 HEAD `ab9b497` + 2 docs commits. PR #3 has NOT advanced,
  so **no rebase is needed** (re-check with
  `git merge-base --is-ancestor origin/claude/ti-mmwave-live-v0 HEAD`).
- Gate at `c0b9587`: lint/typecheck/test/build/security all PASS, **617 Python tests**.

### Phase 3 / PR #3 — parked, not finished
Software gate is **PASS on checks** but `FINAL CODEX: PENDING`. A final review was requested
once (comment `5712432606`, 2026-09-17T09:56Z) and **never arrived** — ~2h vs a 15-16 min norm,
no review and no 👍 reaction, i.e. silently dropped. 31 threads, 0 unresolved. Do NOT infer a
clean review from silence; either re-request (the user said exactly one request, so ask first)
or report PENDING. **Do not merge PR #3.**

### Phase-4 architecture (decided, do not relitigate)
- World -> virtual TI radar -> **real UART bytes** -> real `TiFrameParser` -> real
  `TiTargetNormalizer` -> `PersonObservation`. No shortcut; `radiowave/` never imports `tests/`.
- World -> virtual RFID -> `NativeRfidRead` -> **existing** adapter/`ObservationNormalizer` ->
  `ItemObservation`. **`ItemObservation` and `NativeRfidRead` already existed — do not invent an
  RFID contract.**
- **Ground truth is a sink.** `GroundTruthLog` is imported only by the simulator + its tests.
  Fusion must infer events or we are grading an answer key.
- **The seed varies observation, not truth.** Scripted motion is seed-invariant unless `jitter()`
  is applied; the seed drives sensor noise and faults. This is deliberate — it lets one physical
  scenario be replayed against many noise realizations.
- **Determinism oracle is the synchronous path** (bytes -> parser ->
  `frame_to_observations(received_at=<sim time>)`), NOT the live session: the reader thread's
  batching is not reproducible. The live path exists only for the Observatory acceptance gate.
- **SIM runs stay `mode="LIVE"`** (reusing `LiveObservatoryRun` + a simulator `stream_factory`,
  which is already an injectable hook) rather than adding a `"SIM"` RunMode that would ripple
  through viewmodels/TS types/RunManager/routes. But simulated data MUST be unmistakably labelled
  — plan is an additive `simulated: bool` on `ObservatoryLiveStatus` + a distinct badge.
- Phase-4 store is a NEW builder (`lab/store.py`). **Never modify `build_lab_store()`** — 14
  existing scenarios depend on it.

### Wave status
- **Wave 1 DONE + pushed (`c0b9587`)**: `lab/store.py`, `world.py`, `actors.py`,
  `interactions.py`, `ground_truth.py`, `sensors/ti_encoder.py`, `sensors/ti_radar.py` + 62 tests.
  Round-trip verified independently: exact to ~5e-8 m across several poses AND a non-default
  `TiCoordinateConvention`.
- **Wave 2 IN FLIGHT (uncommitted if this session died)**: `sensors/rfid.py` + `test_rfid.py`;
  `scenarios.py` + `test_scenarios.py` (catalog 01-13 plus `acceptance_60s`).
- **Not started**: `lab/engine.py` (orchestrator owns this — wires world+sensors+pipeline),
  `lab/metrics.py`, Observatory SIM controls, docs.

### Lesson worth keeping
Workers report "tests pass" but do NOT run the repo gate. Wave 1 arrived with 18 ruff errors +
1 mypy error. **Always run `pnpm run lint`/`typecheck` on the combined result before trusting a
worker hand-back**, and spot-check headline claims: one worker's "different seed -> different
evolution" test only passed because its helper called `jitter()`.

### Outstanding from the user
The Phase-4 brief arrived **truncated** at scenario `14_co_w` (14+ missing, plus any section
after 26). Scenarios 01-13 + `acceptance_60s` are being built from the explicit list; reconcile
14+ against `docs/phases/phase-4-virtual-store-lab-v0.md`'s 18-scenario list or ask.

## History
- PR #1 — Foundation v0 (merged by owner).
- PR #2 — Observatory v0 (merged by owner).
