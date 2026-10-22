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

## Current state — 2026-10-20: TI hardware bring-up (new PC, user `admin`)

Branch `claude/ti-hardware-bringup` off `claude/ti-mmwave-live-v0` (PR #3 still OPEN, CI green).
Board: **IWR6843ISK** (TI part PROC073), standalone (no ICBOOST), USB-powered.
- USB bridge is a **Silicon Labs CP2105**, not XDS110 as `docs/hardware/ti-iwr6843-first-bringup.md`
  assumes; needs the Silicon Labs CP210x VCP driver. Enhanced port = CLI (**COM4**), Standard
  port = data (**COM6**). Doc needs correcting.
- Flashed `radar_toolbox_4_20_00_07` → `People_Tracking/3D_People_Tracking/prebuilt_binaries/3D_people_track_6843_demo.bin`
  via UniFlash 9.7. S1: flash = 1 ON,2 off,3 ON,4 ON,5 off; functional = 1 off,2 off,3 ON,4 ON,5 off.
  Power-cycling the USB re-latches SOP; RST not needed.
- Sent `chirp_configs/ISK_6m_default.cfg` unmodified (sensorPosition 2 0 15) via a scratch script
  over COM4 — all 32 commands `Done`. Board was on a desk, so heights read ~0.1 m (expected).
- Local config (git-ignored): `data/private/ti-lab.json` = example config with cli_port COM4.
  Pose is still the example placeholder — not measured.
- **Probe 15 s: 264 frames, 0 rejected, ~18 fps, one track (ti_id 0), exit 0.**
- Raw capture `data/captures/raw-bringup-01.bin` walked independently of the parser: TLV 1010 =
  112 B/record (**3d_v2 confirmed from bytes**), TLV 1012 = 12 B/record, padding bytes 0 in all
  39 records (only id 0 seen, so uint8-vs-uint32 id stays undecided for ids >255). TLVs seen:
  1010, 1011, 1012, 1020, 1021.
- Found: `TiLiveConfig.load` rejects a UTF-8 BOM (PowerShell 5 `Set-Content -Encoding utf8` writes one).
- Mounted on a desk stand, upright, 1.40 m, 0 tilt (`data/private/ti-office-1p4m.cfg`). Room is 4 m
  deep; floor box 3 m wide is too narrow on the left (a seated person at the desk sits at x≈-0.1).
  Reconfiguring a running board hung it — power-cycle before resending a cfg.
- Walk 01 (`walk-01.jsonl`, free walk): t0 = **real seated person** (confirmed by user), t2 walker,
  t1/t3/t4 ghosts (TV-wall mirror, beyond back wall, below floor).
- Walk 02 (`walk-02-lateral.jsonl`, scripted): walker = TI t1 / P0002. Acceptance results:
  1 PASS, 2 PASS (toward TV wall = +x), 3 PASS (reads ~10-15% long vs user's estimates; 1 m step
  not observed), 4 PASS (P0002 ENDED after exit via doorway), 8 PASS (two replays, identical
  SHA-256 over the full 540 KB PipelineResult). 5, 6, 7 NOT RUN.
- Ghost tracks become canonical people (8 tracks for 1 walker + 1 seated). Fix via TI
  boundaryBox/staticBoundaryBox/presenceBoundaryBox in the cfg to the real room.
- Bugs to fix: `capture` exits 0 with 0 frames; `reconnects=0` after a 75 s missing port (check
  whether it counts attempts or successes); config loader rejects a UTF-8 BOM.
- Replay page: https://claude.ai/artifact/GZCQyAdLzoeVTN8HhWGVui (both walks).
- 2026-10-22: room cfg `data/private/ti-office-room.cfg` (taped: left wall 2.50 m, TV wall 1.39 m,
  depth 4.0 m; TI boxes x -2.40..1.29, y 0.30..3.90, z -1..2.5). Twin 3.89×4 m, radar x=2.5.
  Observatory live run-0001 (API on :8765, web on :5173): **test 7 PASS** — USB pulled mid-run,
  API kept serving, status went ERROR after reconnect attempts; after replug + cfg resend,
  Reconnect resumed STREAMING in the same run (reconnects=1, generation=2, 1 rejected frame).
  Run had ~65k frames (~1 h) and 53 person tracks — not yet separated into people vs ghosts.
- 2026-10-22 run-0004 (live, room cfg): user behind sensor, one person walking randomly, one
  static track P0001 at (1.32, 2.79) all run. Walker left view near the radar's front-right 3x;
  each track ENDED and a new one was created on re-entry (P0002..P0005) → **test 5 PASS**.
  5 tracks in ~100 s, all plausibly real — no ghost tracks with the room boxes.
  **Test 6 PASS** by inspecting the live map's SVG: per-person marker, `sigma-*` uncertainty halo,
  heading arrow, label+confidence, 120-point trails; LIVE/STREAMING badge seen earlier. Not yet
  confirmed by eye on screen.
  → **All 8 manual acceptance checks have now passed on hardware.**
  Runs 0002/0003 were stopped/started from another browser; a stale tab kept saying
  "run-0002 is already using the sensor" after run-0002 had stopped (minor Observatory bug).
- Remote Control enabled; board stays connected to this PC. Any power loss needs a cfg resend.
- RFID (Stage 1B) for KSA: 865–868 MHz band per CITC RI055 (2010; confirm current CST rules).
- **Next:** measure real mount, set sensorPosition + pose to match, run the 8 manual acceptance
  checks. All still NOT RUN.

## Previous state — 2026-09-20

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
