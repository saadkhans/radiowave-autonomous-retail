"""Run a scenario end to end through mock adapters and the Foundation pipeline."""

from __future__ import annotations

from collections.abc import Iterable

from radiowave.adapters.mmwave.mock import MockPeopleTracker
from radiowave.adapters.rfid.mock import MockItemTracker
from radiowave.adapters.vision.mock import MockVisionEvidenceProvider
from radiowave.contracts.observations import AnyObservation
from radiowave.contracts.recording import EntryKind
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.pipeline import FoundationPipeline, PipelineConfig, PipelineResult, merge_streams
from radiowave.replay.recorder import InMemoryRecorder
from radiowave.simulator.generators import GeneratorConfig
from radiowave.simulator.scenario import Scenario


def scenario_observations(
    scenario: Scenario, generator_config: GeneratorConfig | None = None
) -> list[AnyObservation]:
    """All normalized observations for a scenario, merged in canonical order."""
    registry = StoreRegistry(scenario.store)
    people = MockPeopleTracker(scenario, registry, generator_config)
    items = MockItemTracker(scenario, registry, generator_config)
    vision = MockVisionEvidenceProvider(scenario, registry, generator_config)
    return list(merge_streams(people.observations(), items.observations(), vision.evidence()))


def run_observations(
    scenario: Scenario,
    observations: Iterable[AnyObservation],
    pipeline_config: PipelineConfig | None = None,
    recorder: InMemoryRecorder | None = None,
) -> PipelineResult:
    registry = StoreRegistry(scenario.store)
    pipeline = FoundationPipeline(
        registry,
        pipeline_config,
        vision_enabled=scenario.vision_enabled,
        scenario_id=scenario.scenario_id,
        recorder=recorder,
    )
    return pipeline.run(observations)


def run_scenario(
    scenario: Scenario,
    pipeline_config: PipelineConfig | None = None,
    generator_config: GeneratorConfig | None = None,
    recorder: InMemoryRecorder | None = None,
) -> PipelineResult:
    if recorder is not None:
        for truth in scenario.expected_events:
            recorder.record_payload(
                EntryKind.GROUND_TRUTH,
                scenario.at(truth.t),
                truth.model_dump(mode="json"),
                scenario.scenario_id,
            )
    observations = scenario_observations(scenario, generator_config)
    return run_observations(scenario, observations, pipeline_config, recorder)
