"""Run a scenario end to end through mock adapters and the Foundation pipeline."""

from __future__ import annotations

from collections.abc import Iterable

from radiowave.adapters.mmwave.mock import MockPeopleTracker
from radiowave.adapters.rfid.mock import MockItemTracker
from radiowave.adapters.vision.mock import MockVisionEvidenceProvider
from radiowave.contracts.observations import AnyObservation
from radiowave.contracts.recording import EntryKind
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.interfaces import Recorder
from radiowave.pipeline import FoundationPipeline, PipelineConfig, PipelineResult, merge_streams
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


def scenario_observation_stream(
    scenario: Scenario, generator_config: GeneratorConfig | None = None
) -> list[AnyObservation]:
    """The observation stream a scenario is *fed*, as opposed to what it generates.

    Scenarios flagged ``duplicate_observation_stream`` replay every observation twice in
    canonical order so deduplication is exercised deterministically.
    """
    observations = scenario_observations(scenario, generator_config)
    if not scenario.duplicate_observation_stream:
        return observations
    return sorted(observations + observations, key=lambda o: (o.timestamp, o.observation_id))


def build_pipeline(
    scenario: Scenario,
    pipeline_config: PipelineConfig | None = None,
    recorder: Recorder | None = None,
) -> FoundationPipeline:
    """Pipeline for a scenario; ground-truth events are scheduled into the recording."""
    base = pipeline_config or PipelineConfig()
    if base.vision_enabled and not scenario.vision_enabled:
        msg = f"pipeline config enables vision but scenario {scenario.scenario_id} has no provider"
        raise ValueError(msg)
    config = base.model_copy(update={"vision_enabled": scenario.vision_enabled})
    pipeline = FoundationPipeline(
        StoreRegistry(scenario.store),
        config,
        scenario_id=scenario.scenario_id,
        recorder=recorder,
    )
    for truth in scenario.expected_events:
        pipeline.schedule_entry(
            scenario.at(truth.t), EntryKind.GROUND_TRUTH, truth.model_dump(mode="json")
        )
    return pipeline


def run_observations(
    scenario: Scenario,
    observations: Iterable[AnyObservation],
    pipeline_config: PipelineConfig | None = None,
    recorder: Recorder | None = None,
) -> PipelineResult:
    return build_pipeline(scenario, pipeline_config, recorder).run(observations)


def run_scenario(
    scenario: Scenario,
    pipeline_config: PipelineConfig | None = None,
    generator_config: GeneratorConfig | None = None,
    recorder: Recorder | None = None,
) -> PipelineResult:
    observations = scenario_observation_stream(scenario, generator_config)
    return run_observations(scenario, observations, pipeline_config, recorder)
