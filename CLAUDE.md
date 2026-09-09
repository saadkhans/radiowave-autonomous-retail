# Claude Development Instructions

## Role
Claude is the implementation orchestrator for this repository. Plan first, delegate routine work to helper agents, review their output, run checks, then commit/push.

## Branch discipline
- Never commit directly to `main` or `dev`.
- `main` = stable milestones only.
- `dev` = integration branch.
- Work on `claude/*` or other feature branches and open PRs into `dev`.
- Never merge a PR unless explicitly instructed by the project owner.

## Architecture invariants
1. Vendor-neutral adapters: TI, Infineon, Impinj, Zebra, NVIDIA, cameras, POS, payments, and future hardware must be replaceable.
2. Downstream business logic consumes normalized world-coordinate/time contracts, never vendor-native coordinates or identifiers.
3. Durable IP lives in the digital twin, event model, sensor fusion, trajectory association, confidence engine, cart engine, replay, calibration, datasets, and exception tooling.
4. Radio-first hypothesis: mmWave for anonymous people trajectories; RAIN RFID for unique merchandise identity/movement; CV selectively for semantic/ground-truth evidence.
5. Event-first and replay-first design.
6. No autonomous charging until shadow-cart validation meets approved thresholds.
7. Do not implement vendor/patent-specific localization or beam-trigger behavior without an explicit architecture/IP review.

## Data/security
Never commit real customer video, biometric data, payment/card information, production secrets, private RFID/customer mappings, or unredacted production sensor captures. Use synthetic fixtures for CI.

## Required checks before push
- `pnpm run lint`
- `pnpm run typecheck`
- `pnpm run test`
- `pnpm run build`
- `pnpm run security:secrets`

## PR policy
Every PR must state architecture impact, tests, known limitations, deferred work, and hardware/vendor assumptions. Request Codex review after each pushed fix cycle.
