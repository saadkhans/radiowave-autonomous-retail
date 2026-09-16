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
- Local HEAD == `origin/claude/ti-mmwave-live-v0` == `0a0aca7`. Working tree clean.
  - `6455378` fix(mmwave): close the round-2 live-boundary gaps
  - `0a0aca7` chore(claude): session state file + codex-loop tightening
- Loop: `/codex-auto-loop` (`.claude/commands/codex-auto-loop.md`), **cycle 2 of 3 complete**.

### Cycle 2 — done (2026-09-16)
Codex review round 2 (review id `5197521431`, against `9677b53`) raised 7 findings (2 P1 +
5 P2). All 7 fixed in `6455378`, each thread replied to with the fixing SHA + regression test
names and resolved individually.

| Codex finding | Where fixed |
|---|---|
| P1 target-height TLV 1012 record layout | `ti/parser.py` (`<B3xff`, 12-byte record), `ti/protocol.py`, `docs/hardware/ti-iwr6843-first-bringup.md` |
| P1 live ownership released before teardown finished | `api/runs.py` (`_closing_live_run_id`), `api/routes/live.py` |
| P2 TARGET_INDEX / TARGET_HEIGHT unbounded | `ti/parser.py` |
| P2 reconnect/stop-triggered stream close counted as transport failure | `ti/session.py` |
| P2 `session.start()` outside constructor cleanup | `api/live.py`, `ti/session.py` |
| P2 no shared `PeopleTracker` boundary | `ti/session.py` (`observations()`), `adapters/mmwave/base.py` (`LivePeopleSource`) |
| P2 active live run unrecoverable after browser reload | `store.tsx` (`adoptLiveRun`), `LiveControls.tsx` |

Gate: all 5 checks PASS (598 tests); `final-reviewer` `VERDICT: PASS`. Its three non-blocking
notes were fixed before push (stale test docstring, overclaiming `protocol.py` sentence, and a
missing `ByteStreamError`-with-pending-request test — the new test was mutation-checked to
confirm it fails without the guard).

**Open deviation to watch:** Codex asked for a uint32 target-height id; we kept uint8 + 3
skipped pad bytes and argued it on the thread. Round 3 may push back. Inert today
(`TiTargetHeight.native_track_id` has no consumer); settle at hardware bring-up.

**Deferred (own change):** extract a vendor-neutral diagnostics contract so
`LivePeopleSource.diagnostics()` can be typed and `LiveObservatoryRun.session` annotated
against the protocol — today that would drag `TiAdapterDiagnostics` into the neutral module.

### Next step
**Waiting on Codex round 3** (requested 2026-09-16, comment `5697400380`; reviews take ~12–16
min). When it lands: read both the review body and the inline threads, and treat round-2
findings already answered on `6455378` as superseded. This is the **last of the 3 cycles** — if
only P3 hardening remains and CI is green, STOP and report the PR as ready for the owner's
merge decision. Never merge.

### Hardware status
No TI board has ever been connected. Hardware acceptance NOT RUN. All measured fields in
`docs/experiments/ti-iwr6843-single-person-baseline.md` are still TBD.

---

## History
- PR #1 — Foundation v0 (merged by owner).
- PR #2 — Observatory v0 (merged by owner).
