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

from radiowave.contracts.recording import EntryKind, RecordedEntry
from radiowave.contracts.store import Store
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.pipeline import FoundationPipeline, PipelineResult
from radiowave.replay.clock import ReplayPacer
from radiowave.replay.reader import ReplayPlayer, observations_from, open_replay_source
from radiowave.replay.recorder import InMemoryRecorder, JsonlRecorder, ParquetRecorder
from radiowave.simulator.library import SCENARIOS, load_scenario
from radiowave.simulator.runner import run_scenario
from radiowave.simulator.stores import build_lab_store


def _print_summary(result: PipelineResult, out: Callable[[str], None] = print) -> None:
    out(f"scenario        : {result.scenario_id}")
    out(
        f"observations    : {result.observations_accepted} accepted, "
        f"{result.observations_dropped} duplicates dropped, {result.steps} fusion steps"
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


def _store_for(entries: list[RecordedEntry], scenario_id: str | None) -> Store:
    if scenario_id is None:
        for entry in entries:
            if entry.scenario_id in SCENARIOS:
                scenario_id = entry.scenario_id
                break
    if scenario_id is not None and scenario_id in SCENARIOS:
        return load_scenario(scenario_id).store
    return build_lab_store()


def cmd_replay(args: argparse.Namespace) -> int:
    source = open_replay_source(args.path)
    entries = list(source.entries())
    if not entries:
        print("recording is empty", file=sys.stderr)
        return 1
    store = _store_for(entries, args.scenario)
    scenario_id = args.scenario or entries[0].scenario_id
    vision_enabled = any(
        e.kind == EntryKind.OBSERVATION
        and e.source_type is not None
        and e.source_type.value == "VISION"
        for e in entries
    )
    registry = StoreRegistry(store)
    pipeline = FoundationPipeline(registry, vision_enabled=vision_enabled, scenario_id=scenario_id)
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
            pipeline.ingest(observation)
        result = pipeline.result()
    else:
        pacer = ReplayPacer(rate=args.rate) if args.rate > 0 else None
        player = ReplayPlayer(source, pacer)
        result = pipeline.run(observations_from(iter(player)))
    _print_summary(result)
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
    rep.add_argument("--scenario", help="scenario id whose store twin to use")
    rep.set_defaults(func=cmd_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
