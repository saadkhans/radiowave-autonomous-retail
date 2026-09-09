"""Export JSON Schema for the canonical contracts into ``schemas/json``.

Run: ``python scripts/export_schemas.py`` (and ``--check`` in CI to detect drift).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from radiowave.contracts import (
    CartEvent,
    ConfidenceDecision,
    ConfidenceThresholds,
    InteractionCandidate,
    ItemObservation,
    ItemTrack,
    PersonObservation,
    PersonTrack,
    RetailEvent,
    SensorHealth,
    ShopperSession,
    Store,
    VisionEvidence,
)
from radiowave.contracts.recording import RecordedEntry

EXPORTS: dict[str, type[BaseModel]] = {
    "person_observation": PersonObservation,
    "item_observation": ItemObservation,
    "vision_evidence": VisionEvidence,
    "sensor_health": SensorHealth,
    "person_track": PersonTrack,
    "item_track": ItemTrack,
    "interaction_candidate": InteractionCandidate,
    "retail_event": RetailEvent,
    "cart_event": CartEvent,
    "confidence_decision": ConfidenceDecision,
    "confidence_thresholds": ConfidenceThresholds,
    "shopper_session": ShopperSession,
    "store": Store,
    "recorded_entry": RecordedEntry,
}


def render(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", default=str(Path(__file__).resolve().parents[1] / "schemas" / "json")
    )
    parser.add_argument("--check", action="store_true", help="fail if committed schemas differ")
    args = parser.parse_args(argv)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    drift: list[str] = []
    for name, model in EXPORTS.items():
        path = out_dir / f"{name}.schema.json"
        content = render(model)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                drift.append(path.name)
        else:
            path.write_text(content, encoding="utf-8")
    if drift:
        print("schema drift detected:", ", ".join(drift), file=sys.stderr)
        return 1
    if not args.check:
        print(f"exported {len(EXPORTS)} schemas to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
