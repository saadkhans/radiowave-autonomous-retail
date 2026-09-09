# /codex-auto-loop

Drive PR #1 (`claude/foundation-v0` -> `dev`) through iterative Codex review, fix, and re-review
cycles until there are no active Codex blockers. This command is for the main orchestrator Claude
session; it delegates work to the helper agents in `.claude/agents/`.

## Hard rules (apply for the entire loop)

- Never merge PR #1 automatically, under any condition.
- Never create a new branch or a new PR — all work happens on the existing
  `claude/foundation-v0` branch.
- Never suppress a genuine secret finding. If `secret-scan-worker` reports a genuine secret, stop
  the loop immediately and report to the user instead of continuing.
- Never commit directly to `main` or `dev`.
- Do not touch Python code, `pyproject.toml`, or `package.json` yourself — delegate all code
  changes to `fix-worker`.
- If the same finding fails to resolve after 3 iterations of this loop, stop and report it to the
  user rather than continuing to retry.

## Steps

1. **Inspect PR #1.**
   Run `gh pr view 1` and `gh pr checks 1` to see current PR state, mergeability, and CI status.

2. **Read the latest Codex review only.**
   Confirm (via `gh pr view 1 --comments` or `gh api repos/{owner}/{repo}/pulls/1/reviews`) that a
   Codex review exists. If none exists, stop this loop and report — there is nothing to act on.

3. **Delegate to `codex-review-reader`.**
   Have it summarize the active findings from the latest Codex review only, in its structured
   format (file, line, severity, summary, still_applies).

4. **Classify blockers vs deferred.**
   As the main Claude, review the structured findings and classify each as:
   - **Foundation v0 blocker** — violates a `CLAUDE.md`/`AGENTS.md` invariant, breaks correctness,
     determinism, idempotency, or vendor neutrality, or blocks the Foundation v0 success criteria
     in `docs/architecture/system-overview.md`.
   - **Deferred** — valid but out of Foundation v0 scope (e.g. production hardware integration,
     performance optimization, nice-to-have refactors) or already stale
     (`still_applies: false`).
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
   It must return `VERDICT: PASS` before continuing. On `VERDICT: FAIL`, take its prioritized list
   back to step 5 (or step 6 if the plan was already correct and only the implementation was
   wrong) and iterate. Do not proceed to commit/push on a `FAIL`.

10. **Commit with conventional commits.**
    One commit per logical fix (or a single commit covering the iteration's fixes if they are
    tightly related), using a conventional commit type (`fix:`, `refactor:`, `test:`, `docs:` as
    appropriate) and a summary of what Codex finding it addresses.

11. **Push** to `origin/claude/foundation-v0`.

12. **Comment on PR #1** with exactly this text (no additions, no paraphrasing):

    ```
    @codex review the latest commit. Please focus on active Foundation v0 correctness, architecture and security issues. Do not reopen outdated findings unless they still exist in the latest diff.
    ```

13. **Repeat** from step 1 until `codex-review-reader` reports no active Foundation v0 blockers in
    the latest review.

## Stopping conditions

- No active blockers remain in the latest Codex review: report success and stop (do not merge).
- A genuine secret is found: stop immediately, report, do not push.
- The same finding persists unresolved after 3 full iterations of this loop: stop and report the
  finding, what was tried, and why it did not resolve, for human decision.
- Any step's agent reports it cannot proceed (e.g. ambiguous finding, missing base ref): stop and
  report rather than guessing.
