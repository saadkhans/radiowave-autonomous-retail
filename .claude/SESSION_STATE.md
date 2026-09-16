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

## Current state — 2026-09-16

**Phase 3 — TI IWR6843 mmWave live people tracking.**
- Branch: `claude/ti-mmwave-live-v0` → PR **#3** into `dev`. Never merge.
- Local HEAD == `origin/claude/ti-mmwave-live-v0` == `9677b53` ("fix(mmwave): harden live
  sensor integrity and lifecycle"). CI green on that head.
- Loop: `/codex-auto-loop` (`.claude/commands/codex-auto-loop.md`), **cycle 2 of 3**.
- Codex review round 2 (review id `5197521431`, submitted 2026-09-14T12:17Z against `9677b53`):
  **7 findings — 2 P1 + 5 P2**, listed below.

### Uncommitted work (would be lost — this is the round-2 fix set)

| Codex finding | Where fixed |
|---|---|
| P1 target-height TLV 1012 record layout | `ti/parser.py` (`<B3xff`, 12-byte record), `ti/protocol.py` (rationale: uint8 id + 3 pad bytes, **deliberately not** Codex's uint32 reading), `docs/hardware/ti-iwr6843-first-bringup.md` (confirm against real bytes at bring-up) |
| P1 live ownership released before teardown finished | `api/runs.py` (`_closing_live_run_id`, held across `delete()`'s `run.close()` in a `finally`), `api/routes/live.py` (distinct "a live run is stopping" reason) |
| P2 TARGET_INDEX unbounded by `max_points` | `ti/parser.py` |
| P2 reconnect/stop-triggered stream close counted as transport failure | `ti/session.py` (`ByteStreamClosed` **and** `ByteStreamError` treated as benign when a stop/reconnect is pending) |
| P2 `session.start()` outside constructor cleanup | `api/live.py` (start inside the `try`), `ti/session.py` (close a just-opened raw capture and restore `_stopped` if thread start raises) |
| P2 no shared `PeopleTracker` boundary | `ti/session.py` (`observations()`), `adapters/mmwave/base.py` (`LivePeopleSource` protocol; full vendor-neutral diagnostics extraction deliberately deferred and documented) |
| P2 active live run unrecoverable after browser reload | `apps/observatory/src/state/store.tsx` (`adoptLiveRun`), `components/LiveControls.tsx` ("Resume live run" control) |

Regression tests added alongside: `tests/unit/mmwave_ti/test_parser.py`,
`tests/unit/mmwave_ti/test_session.py`, `tests/api/test_live_runs.py`,
`apps/observatory/src/App.live.test.tsx`.

Also modified: `.claude/commands/codex-auto-loop.md` — cycle cap 5→3, added "read the review
body too", per-thread resolution step (reply with fixing SHA + test name, no mass-resolve),
and an explicit ready-for-human-merge stopping condition.

### Next step
Run the full check sequence → `final-reviewer` → commit → push → reply to and resolve each
round-2 thread with the fixing SHA + test name → post the `@codex review` comment from step 13
of the auto-loop → stop and wait for round 3 (last of the 3 cycles).

### Hardware status
No TI board has ever been connected. Hardware acceptance NOT RUN. All measured fields in
`docs/experiments/ti-iwr6843-single-person-baseline.md` are still TBD.

---

## History
- PR #1 — Foundation v0 (merged by owner).
- PR #2 — Observatory v0 (merged by owner).
