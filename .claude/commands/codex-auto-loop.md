# /codex-auto-loop

Drive the current PR through iterative Codex review, fix, and re-review cycles until there are no
active Codex blockers. The current PR is **#3** (`claude/ti-mmwave-live-v0` -> `dev`, Phase 3 TI
mmWave live integration); earlier PRs (#1 Foundation v0, #2 Observatory v0) are merged and closed. This command is for the main orchestrator Claude
session; it delegates work to the helper agents in `.claude/agents/`.

## Hard rules (apply for the entire loop)

- Never merge the PR automatically, under any condition.
- Never create a new branch or a new PR — all work happens on the existing
  `claude/ti-mmwave-live-v0` branch. Never push to `dev` or `main`.
- Confirm the current head (`git rev-parse HEAD`) matches the PR head before reading a review.
- Read ONLY the latest Codex review on the current head. Only findings raised against the
  CURRENT head are active. A finding from an earlier commit that already has an owner reply
  naming a fix and a regression test is superseded and must not be reopened unless the defect
  is demonstrated to still exist in the working tree at the current head. Do not reopen stale
  PR #1/#2 findings unless the defect demonstrably exists in the Phase 3 head.
- Maximum 3 review cycles per invocation. If after 3 cycles only low-value P3 hardening
  remains, STOP and report for a human merge decision.
- Never suppress a genuine secret finding. If `secret-scan-worker` reports a genuine secret, stop
  the loop immediately and report to the user instead of continuing.
- Never commit directly to `main` or `dev`.
- Do not touch Python code, `pyproject.toml`, or `package.json` yourself — delegate all code
  changes to `fix-worker`.
- If the same finding fails to resolve after 3 iterations of this loop, stop and report it to the
  user rather than continuing to retry.

## Steps

1. **Inspect PR #3.**
   Run `gh pr view 3` and `gh pr checks 3` to see current PR state, mergeability, and CI status,
   and confirm the head SHA.

2. **Read the latest Codex review only.**
   Confirm (via `gh pr view 3 --comments` or `gh api repos/{owner}/{repo}/pulls/3/reviews`) that a
   Codex review exists on the current head. If none exists, stop this loop and report — there is nothing to act on.
   **Important:** a Codex P1 blocker may appear in the review SUBMISSION BODY itself, not only
   as inline thread comments. Reading only inline comments can miss a critical blocker. Always
   inspect both the review body and the inline threads.

3. **Delegate to `codex-review-reader`.**
   Have it summarize the active findings from the latest Codex review only, in its structured
   format (file, line, severity, summary, still_applies).

4. **Classify blockers vs deferred.**
   As the main Claude, label each finding P1 / P2 / P3 and decide whether it is **Phase-3
   blocking**:
   - **Phase 3 blocker** — violates a `CLAUDE.md`/`AGENTS.md` invariant or one of the Phase 3
     review-gate items (TI details leaking past the adapter, native ids as canonical identity,
     unconverted TI coordinates, naive/non-UTC timestamps, fabricated uncertainty/confidence,
     unbounded parser buffers or queues, NaN/inf reaching fusion, a serial failure that can crash
     the API, unserialized pipeline mutation, hardware required by CI, non-deterministic capture/
     replay, LIVE/REPLAY confusion in the Observatory, or a Foundation/Observatory regression).
   - **Deferred** — valid but out of Phase 3 scope (RFID, multi-radar, raw ADC/DSP, firmware
     flashing, cloud/broker infrastructure, calibration studies), low-value P3 hardening, or
     already stale (`still_applies: false`).
   Record the classification and rationale for each finding before proceeding.

5. **Delegate to `repo-investigator`** for each blocker (one call per finding, or batched
   independent findings in parallel) to produce a minimal implementation plan: exact files,
   functions, and tests.

6. **Delegate to `fix-worker`** with the exact plan from step 5 for each blocker. Do not hand
   `fix-worker` open-ended instructions — pass the specific files/changes/tests from the
   investigator's plan.

7. **Delegate to `test-runner`** to run the targeted test(s) for each fix.

8. **Run the full check sequence** via `test-runner`: `pnpm run lint`, `pnpm run typecheck`,
   `pnpm run test`, `pnpm run build`, `pnpm run security:secrets`. If
   `security:secrets` fails, delegate to `secret-scan-worker` before proceeding further (see hard
   rules above regarding genuine secrets).

9. **Delegate to `final-reviewer`.**
   Give it the Phase 3 hardware-boundary focus (see `docs/phases/phase-3-ti-mmwave-live.md`,
   "Review gate"). It must return `VERDICT: PASS` before continuing. On `VERDICT: FAIL`, take its prioritized list
   back to step 5 (or step 6 if the plan was already correct and only the implementation was
   wrong) and iterate. Do not proceed to commit/push on a `FAIL`.

10. **Commit with conventional commits.**
    One commit per logical fix (or a single commit covering the iteration's fixes if they are
    tightly related), using a conventional commit type (`fix:`, `refactor:`, `test:`, `docs:` as
    appropriate) and a summary of what Codex finding it addresses.

11. **Push** to `origin/claude/ti-mmwave-live-v0` only.

12. **Resolve threads for each fixed finding.**
    For each active finding that was addressed by the fixes in step 6:
    - Verify the exact defect is actually fixed in the pushed code.
    - Verify a named regression test exists that locks the fix (step 7 output).
    - Reply on that thread with the fixing commit SHA and the exact test name.
    - Resolve ONLY that thread.
    Note: a finding raised in the review SUBMISSION BODY may have no inline thread; answer it
    with a PR comment instead.
    Forbidden: mass-resolving threads, resolving without proving the fix, and resolving
    unrelated historical comments. Each thread must have a specific reply proving the fix before
    it is marked resolved.

13. **Comment on PR #3** with exactly this text (no additions, no paraphrasing):

    ```
    @codex review the latest commit. Please perform a comprehensive Phase-3 hardware-boundary review. Focus on TI parser ambiguity and packet bounds, firmware-profile validity, stream-generation identity, subframe-aware deduplication, monotonic-correlated UTC timestamps, atomic drain/clock behavior, reconnect recovery, exclusive live-run ownership, capture preservation, and LIVE-to-REPLAY transition safety. Please look for root-invariant violations rather than isolated stylistic issues. Do not request RFID, multi-radar fusion, raw ADC DSP, production infrastructure, or later-phase scope. Do not reopen superseded findings unless the defect still exists on the latest head.
    ```

    Then STOP and wait for that review before any speculative new work.

14. **Repeat** from step 1 (when the next review arrives) until `codex-review-reader` reports no
    active Phase 3 blockers in the latest review, or the 3-cycle cap is reached.

## Stopping conditions

- No active blockers remain in the latest Codex review: report success and stop (do not merge).
- A genuine secret is found: stop immediately, report, do not push.
- The same finding persists unresolved after 3 full iterations of this loop: stop and report the
  finding, what was tried, and why it did not resolve, for human decision.
- Any step's agent reports it cannot proceed (e.g. ambiguous finding, missing base ref): stop and
  report rather than guessing.
- After the cycle cap is reached, if zero P1 findings remain, zero meaningful P2 findings, only
  minor P3/nit hardening remains, CI is green (all checks pass: lint, typecheck, test, build,
  security:secrets), and final-reviewer returns PASS: STOP and report that the PR is ready for
  a human merge decision. The loop must never chase review perfection and must never merge.

**Required before any push:** Full check sequence must pass (`pnpm run lint`, `pnpm run typecheck`, `pnpm run test`, `pnpm run build`, `pnpm run security:secrets`) and final-reviewer must return `VERDICT: PASS`.
