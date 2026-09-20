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

## Current state — 2026-09-20

**Phase 4 (Virtual Store Lab) is complete and MERGED. Phase 3 is not.**

- **PR #4 → `claude/ti-mmwave-live-v0`: MERGED.** Because #4 was stacked on #3's branch,
  Phase 4 now lives in `claude/ti-mmwave-live-v0`.
- **PR #3 → `dev`: STILL OPEN.** The owner instructed merging it; the auto-mode classifier
  blocked `gh pr merge 3` and I stopped rather than routing around it. **Finish with
  `gh pr merge 3 --merge`** (run by the user, or after a Bash permission rule is added).
  Merging it carries BOTH phases into `dev`.

### Two facts that must not get lost once this is on `dev`
1. **PR #4 never ran CI.** `.github/workflows/ci.yml` only triggers on PRs targeting
   `dev`/`main`; #4 targeted the stacked parent. Its only verification was the local gate.
   The first real CI run of Phase 4 happens when PR #3 merges — watch it.
2. **PR #3's final merge-readiness Codex review was never delivered** (requested once,
   no review and no reaction, ~2h vs a 15-min norm). Silence was never a pass.
   **Hardware acceptance is unrun** — no TI board has ever been connected; every measured
   field in `docs/experiments/ti-iwr6843-single-person-baseline.md` is TBD.

### Phase 4 review outcome
Codex round 1 on PR #4: **14 findings (7 P1 + 7 P2), all fixed** in `8c301bf`, every thread
answered and resolved. Gate: lint/typecheck/build/secrets + **740 tests**.

Most of those findings were **simulator defects corrupting the lab's own measurements**, which
is the failure mode to watch for in this codebase:
- exit truth stamped on the EXIT instruction rather than at the boundary crossing;
- handoff truth recording receiver-then-giver, reversing the RetailEvent contract;
- RFID polling at the 20 Hz world tick while advertising 2 Hz — ten times the independent
  position blurs, making simulated evidence *better than the hardware assumption it stands in
  for*;
- departed merchandise still answering in-store antennas.

**Lesson worth keeping:** I reported "fusion over-proposes PICK ~10x" from raw proposal counts.
Fusion re-proposes a *waiting* episode every step by design; counted as distinct episodes there
was no such over-proposal. Count episodes, not proposals.

### Known-open, non-blocking
- `01_normal_purchase` declares expected exit truth at t=20 but the action is scheduled at
  t=22 (crossing now lands at 22.8s) — declared expectation is inconsistent, left unadjusted
  rather than silently fitted to the code.
- PICK detection latency is +2.3s..+17.3s at honest reader cadence, mostly beyond the metrics'
  2.0s match tolerance. **Do not widen the tolerance to improve the numbers** — that is tuning
  the ruler. Report latency instead.
- PUTBACK and HANDOFF are still genuinely never inferred. Attribution hardening is Phase 6.

## History
- PR #4 — Virtual Store Lab v0 (merged into the Phase-3 branch 2026-09-20).
- PR #1 — Foundation v0 (merged by owner).
- PR #2 — Observatory v0 (merged by owner).
