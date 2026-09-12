"""Mock RAIN RFID item tracker driven by a synthetic scenario."""

from __future__ import annotations

from collections.abc import Iterator

from radiowave.contracts.observations import ItemObservation
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.ingestion.normalization import ObservationNormalizer
from radiowave.simulator.generators import GeneratorConfig, RfidGenerator
from radiowave.simulator.scenario import Scenario


class MockItemTracker:
    """Implements :class:`~radiowave.fusion.interfaces.ItemTracker` from scripted ground truth.

    Location estimates carry the configured accuracy; nothing here is more
    precise than ``GeneratorConfig.rfid_sigma_m`` claims.
    """

    def __init__(
        self, scenario: Scenario, registry: StoreRegistry, config: GeneratorConfig | None = None
    ) -> None:
        self._generator = RfidGenerator(scenario, registry, config)
        self._normalizer = ObservationNormalizer(registry, scenario.scenario_id)

    def observations(self) -> Iterator[ItemObservation]:
        for read in self._generator.reads():
            yield self._normalizer.item(read)
