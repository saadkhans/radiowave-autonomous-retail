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

**Phase 3 — TI IWR6843 mmWave live people tracking.**
- Branch: `claude/ti-mmwave-live-v0` → PR **#3** into `dev`. **Never merge — owner's call.**
- Local HEAD == `origin/claude/ti-mmwave-live-v0` == `80a88e6`. Working tree clean apart from
  this file.
- **The codex-auto-loop is FINISHED: all 3 cycles used.** Do not start a cycle 4 and do not post
  another `@codex review` comment. The PR is waiting on a human merge decision.

### Codex rounds — all complete
| Round | Head reviewed | Findings | Fixed in | Threads |
|---|---|---|---|---|
| 1 | `1ba1782` | 6 P1 + 9 P2 | `9677b53` | answered |
| 2 | `9677b53` | 2 P1 + 5 P2 | `6455378` | replied + resolved individually |
| 3 | `0a0aca7` | 4 P1 + 6 P2 | `80a88e6` | replied + resolved individually |

23 findings total (8 P1, 15 P2). Gate on each push: all 5 checks + `final-reviewer` PASS.
Final state: 620 tests (554 Python, 66 TS). PR body now carries architecture impact, tests,
known limitations, deferred work and hardware assumptions (required by CLAUDE.md).

### Round 3 — what changed (all in `80a88e6`)
Four P1s shared one shape: a failure the live path absorbed and ran past.
- `api/live.py` `stop()` finalized although `session.stop()` returned False → run stays
  unfinished; `api/runs.py` `delete()` keeps the slot; `_stuck_live_run_id` makes the permanent
  case say "restart the process" instead of "try again shortly".
- `api/live.py` recorder I/O error counted as a bad sample → `_AttributableRecorder` latches
  (on `record` **and** `close`, since `JsonlRecorder` is buffered and a full disk usually
  surfaces at close), run fails observably, live status reports ERROR.
- `ti/session.py` raw-capture write shared the parser's except handler → `_write_capture()`
  detaches once, `raw_capture_failed` in diagnostics + health. Opposite call to the recorder
  above, deliberately.
- `store.tsx` replay transition ignored an unbound server-side live run → `stopActiveLiveRun`
  consults `serverLiveRunIdRef`, but ONLY on committing transitions; a dropdown selection
  deliberately does not tear down another operator's session.
P2s: side-info bound + count cross-check, packet-tail validation (rejects embedded magic word),
firmware TLV-family enforcement, reconnect-request acknowledgement window, `max_attempts` at
equality, `close_all` waiting for an in-flight `factory()`, and `firmware_profile_for` via
`dataclasses.replace` (it had been silently dropping `tlv_family`).

### If work resumes here
The three open threads to pick up are all in the PR body's *Known limitations* / *Deferred
work*: the target-height id width (settle at bring-up), the vendor-neutral diagnostics contract
extraction, and the `2 * packet_alignment` tail bound. None blocks merge.

### Hardware status
No TI board has ever been connected. Hardware acceptance NOT RUN. All measured fields in
`docs/experiments/ti-iwr6843-single-person-baseline.md` are still TBD.

---

## History
- PR #1 — Foundation v0 (merged by owner).
- PR #2 — Observatory v0 (merged by owner).
