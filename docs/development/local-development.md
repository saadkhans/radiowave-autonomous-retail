# Local Development

## Environment setup

Python 3.12 is required.

Using `uv` (recommended):

```sh
uv venv .venv --python 3.12
uv pip install -e ".[dev,api]"
pnpm install            # Observatory frontend workspace (apps/observatory)
```

Using `pip`:

```sh
python -m venv .venv
# Windows
.venv\Scripts\activate
# POSIX
source .venv/bin/activate

pip install -e ".[dev,api]"
```

## Running checks

All checks are exposed as `pnpm` scripts (see `package.json`); `pnpm` itself has no npm
dependencies to install for this project — it only wraps the Python-based checks below.

```sh
pnpm run lint         # ruff check . && eslint (apps/observatory)
pnpm run typecheck    # mypy radiowave scripts && tsc -b (apps/observatory)
pnpm run test         # pytest && vitest (apps/observatory)
pnpm run build        # compileall + JSON schema drift check + vite build
pnpm run security:secrets   # gitleaks detect --source . --no-banner
pnpm run dev          # Observatory: API (uvicorn :8765) + Vite (:5173) side by side
pnpm run dev:api      # API only
pnpm run dev:observatory   # frontend only
```

Run a single test file or targeted test directly with pytest, without going through `pnpm`:

```sh
# Windows
.venv\Scripts\python.exe -m pytest tests/unit/test_something.py -q

# POSIX
.venv/bin/python -m pytest tests/unit/test_something.py -q
```

### Secret scanning locally

`pnpm run security:secrets` requires the `gitleaks` binary on `PATH`.

- **Windows:** download the release zip for your platform from
  [github.com/gitleaks/gitleaks/releases](https://github.com/gitleaks/gitleaks/releases),
  extract it, and put the folder containing `gitleaks.exe` on your `PATH`.
- **macOS/Linux:** install via your package manager (e.g. `brew install gitleaks`) or download the
  matching release tarball and place the `gitleaks` binary on `PATH`.

CI pins a specific gitleaks version (see `.github/workflows/ci.yml`); prefer matching that version
locally to avoid rule-set drift between local runs and CI.

## Running the simulator

Generate a synthetic scenario recording:

```sh
python -m radiowave.cli simulate --scenario 01 --out data/synthetic/scenario-01.jsonl
```

Parquet output is also supported:

```sh
python -m radiowave.cli simulate --scenario 01 --out data/synthetic/scenario-01.parquet --format parquet
```

List available scenarios:

```sh
python -m radiowave.cli scenarios
```

## Replaying a recording

```sh
python -m radiowave.cli replay data/synthetic/scenario-01.jsonl --rate 0   # twin and thresholds come from the recording (or --store/--scenario/--config)
```

`--rate` controls playback speed:
- `0` — as fast as possible (no real-time pacing)
- `1.0` — real time
- `10` — 10x accelerated

Use `--step` to advance one observation/event at a time interactively instead of continuous
playback.

## Deferred infrastructure

PostgreSQL/PostGIS, NATS, MinIO, and Docker Compose are **intentionally not part of Foundation
v0**. All tests and the simulator/replay tooling run in-process against synthetic data — no
external database, message bus, object store, or container orchestration is required to develop,
test, or review this stage of the project.

This infrastructure will be introduced later, once persistence and streaming are actually needed
to support hardware experiments (see `docs/hardware/hardware-roadmap.md`) — not before. Do not add
these dependencies preemptively.

## Data handling

`data/` is git-ignored except for synthetic fixtures committed explicitly under `tests/` (see
`.gitignore`: `data/raw/`, `data/captures/`, and `data/private/` are excluded). Never commit real
customer video, biometric data, payment data, production secrets, or unredacted production sensor
captures — synthetic fixtures only. See `README.md` and `CLAUDE.md` for the full data/security
policy.
