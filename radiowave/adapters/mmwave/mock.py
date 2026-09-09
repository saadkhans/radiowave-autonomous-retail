"""Mock mmWave people tracker driven by a synthetic scenario."""

from __future__ import annotations

from collections.abc import Iterator

from radiowave.contracts.observations import PersonObservation
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.ingestion.normalization import ObservationNormalizer
from radiowave.simulator.generators import GeneratorConfig, RadarGenerator
from radiowave.simulator.scenario import Scenario


class MockPeopleTracker:
    """Implements :class:`~radiowave.fusion.interfaces.PeopleTracker` from scripted ground truth.

    Radar samples are produced in each radar's native frame and normalized into
    the store frame exactly as a real adapter's output would be.
    """

    def __init__(
        self, scenario: Scenario, registry: StoreRegistry, config: GeneratorConfig | None = None
    ) -> None:
        self._generator = RadarGenerator(scenario, registry, config)
        self._normalizer = ObservationNormalizer(registry, scenario.scenario_id)

    def observations(self) -> Iterator[PersonObservation]:
        for sample in self._generator.samples():
            yield self._normalizer.person(sample)
