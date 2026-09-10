"""Command-line entry points: list scenarios, simulate to a recording, replay a recording.

Examples::

    python -m radiowave.cli scenarios
    python -m radiowave.cli simulate --scenario 01 --out data/synthetic/scenario-01.jsonl
    python -m radiowave.cli simulate --scenario 05 --out data/synthetic/scenario-05.parquet
    python -m radiowave.cli replay data/synthetic/scenario-01.jsonl --rate 0
    python -m radiowave.cli replay data/synthetic/scenario-01.jsonl --step
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ValidationError

from radiowave.contracts.recording import EntryKind, RecordedEntry
from radiowave.contracts.store import Store
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.pipeline import FoundationPipeline, PipelineConfig, PipelineResult
from radiowave.replay.clock import ReplayPacer
from radiowave.replay.reader import ReplayPlayer, observations_from, open_replay_source
from radiowave.replay.recorder import InMemoryRecorder, JsonlRecorder, ParquetRecorder
from radiowave.simulator.library import SCENARIOS, load_scenario
from radiowave.simulator.runner import run_scenario


def _print_summary(result: PipelineResult, out: Callable[[str], None] = print) -> None:
    out(f"scenario        : {result.scenario_id}")
    out(
        f"observations    : {result.observations_accepted} accepted, "
        f"{result.observations_dropped} duplicates dropped, "
        f"{result.observations_rejected_low_confidence} rejected (low confidence), "
        f"{result.observations_rejected_unknown_sensor} rejected (unknown sensor), "
        f"{result.steps} fusion steps"
    )
    out(f"person tracks   : {[t.track_id for t in result.person_tracks]}")
    out("items           :")
    for item in result.item_tracks:
        out(f"  {item.epc.value}  {item.state.value:<22} carrier={item.carrier_track_id}")
    out("committed events:")
    for event in result.committed_events:
        target = f" -> {event.counterpart_track_id}" if event.counterpart_track_id else ""
        out(
            f"  {event.timestamp.strftime('%H:%M:%S.%f')[:-4]}  {event.event_type.value:<15} "
            f"{event.epc.value}  {event.shopper_track_id}{target}  "
            f"confidence={event.confidence:.2f} margin={event.margin:.2f}"
        )
    if result.review_events:
        out("review events   :")
        for event in result.review_events:
            out(
                f"  {event.event_type.value} {event.epc.value} "
                f"candidates={[(c.person_track_id, round(c.score, 2)) for c in event.candidates]}"
            )
    decisions = {d.decision.value: 0 for d in result.decisions}
    for decision in result.decisions:
        decisions[decision.decision.value] += 1
    out(f"decisions       : {decisions}")
    out("carts           :")
    for cart in result.cart_state.carts.values():
        out(f"  {cart.cart_id} [{cart.status.value}] {sorted(cart.lines)}")
    if result.cart_state.unresolved:
        out(f"unresolved      : {sorted(result.cart_state.unresolved)}")


def cmd_scenarios(_: argparse.Namespace) -> int:
    for scenario_id in SCENARIOS:
        scenario = load_scenario(scenario_id)
        print(f"{scenario_id:>4}  {scenario.name}")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    if args.seed is not None:
        scenario = scenario.model_copy(update={"seed": args.seed})
    recorder: InMemoryRecorder
    if args.out is None:
        recorder = InMemoryRecorder()
    else:
        path = Path(args.out)
        fmt = args.format or ("parquet" if path.suffix.lower() == ".parquet" else "jsonl")
        recorder = ParquetRecorder(path) if fmt == "parquet" else JsonlRecorder(path)
    result = run_scenario(scenario, recorder=recorder)
    recorder.close()
    _print_summary(result)
    if args.out is not None:
        print(f"recording       : {args.out}")
    return 0


def _load_json_model[M: BaseModel](path: Path, model: type[M]) -> M:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc
    except ValidationError as exc:
        raise SystemExit(f"{path} is not a valid {model.__name__}: {exc}") from exc


def _config_for(entries: list[RecordedEntry], args: argparse.Namespace) -> PipelineConfig:
    """The configuration the recording was produced with; never silently defaulted."""
    if args.config is not None:
        return _load_json_model(Path(args.config), PipelineConfig)
    for entry in entries:
        if entry.kind == EntryKind.PIPELINE_CONFIG:
            return PipelineConfig.model_validate(entry.payload)
    msg = (
        "recording carries no PIPELINE_CONFIG entry; pass --config <pipeline.json> so replay "
        "uses the thresholds the recording was produced with"
    )
    raise SystemExit(msg)


def _store_for(entries: list[RecordedEntry], args: argparse.Namespace) -> Store:
    """The twin a recording was produced against; never silently substituted."""
    if args.store is not None:
        return _load_json_model(Path(args.store), Store)
    if args.scenario is not None:
        return load_scenario(args.scenario).store
    for entry in entries:
        if entry.kind == EntryKind.STORE_TWIN:
            return Store.model_validate(entry.payload)
    msg = (
        "recording carries no STORE_TWIN entry; pass --store <store.json> or "
        "--scenario <id> so replay uses the original digital twin"
    )
    raise SystemExit(msg)


def cmd_replay(args: argparse.Namespace) -> int:
    source = open_replay_source(args.path)
    entries = list(source.entries())
    if not entries:
        print("recording is empty", file=sys.stderr)
        return 1
    store = _store_for(entries, args)
    scenario_id = args.scenario or entries[0].scenario_id
    registry = StoreRegistry(store)
    pipeline = FoundationPipeline(registry, _config_for(entries, args), scenario_id=scenario_id)
    if args.step:
        print("step mode: press Enter to release the next observation, q to finish")
        for entry in entries:
            if entry.kind != EntryKind.OBSERVATION:
                continue
            observation = entry.to_observation()
            print(
                f"{entry.sequence:>6} {entry.timestamp.isoformat()} {entry.source_type} "
                f"{entry.sensor_id} {observation.observation_id}"
            )
            answer = sys.stdin.readline()
            if answer.strip().lower() == "q":
                break
            pipeline.ingest(observation)  # steps fusion across every boundary it crosses
        result = pipeline.finish()
    else:
        pacer = ReplayPacer(rate=args.rate) if args.rate > 0 else None
        player = ReplayPlayer(source, pacer)
        result = pipeline.run(observations_from(iter(player)))
    _print_summary(result)
    if result.observations_rejected_unknown_sensor and not result.observations_accepted:
        print(
            "every observation named a sensor unknown to the twin; the recording and the "
            "store do not belong together",
            file=sys.stderr,
        )
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radiowave", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scenarios", help="list built-in synthetic scenarios").set_defaults(
        func=cmd_scenarios
    )

    sim = sub.add_parser("simulate", help="run a scenario and optionally record it")
    sim.add_argument("--scenario", required=True, help="scenario id, e.g. 01")
    sim.add_argument("--out", help="recording path (.jsonl or .parquet)")
    sim.add_argument("--format", choices=["jsonl", "parquet"], help="override format")
    sim.add_argument("--seed", type=int, help="override the scenario seed")
    sim.set_defaults(func=cmd_simulate)

    rep = sub.add_parser("replay", help="replay a recording through the pipeline")
    rep.add_argument("path")
    rep.add_argument(
        "--rate",
        type=float,
        default=0.0,
        help="0 = as fast as possible, 1 = real time, 10 = 10x (default 0)",
    )
    rep.add_argument("--step", action="store_true", help="release observations one by one")
    rep.add_argument("--scenario", help="built-in scenario id whose store twin to use")
    rep.add_argument("--store", help="path to a Store JSON twin to replay against")
    rep.add_argument("--config", help="path to a PipelineConfig JSON to replay with")
    rep.set_defaults(func=cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
