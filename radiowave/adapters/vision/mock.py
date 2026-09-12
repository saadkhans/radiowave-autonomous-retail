"""Mock vision evidence provider driven by a synthetic scenario."""

from __future__ import annotations

from collections.abc import Iterator

from radiowave.contracts.observations import VisionEvidence
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.ingestion.normalization import ObservationNormalizer
from radiowave.simulator.generators import GeneratorConfig, VisionGenerator
from radiowave.simulator.scenario import Scenario


class MockVisionEvidenceProvider:
    """Implements :class:`~radiowave.fusion.interfaces.VisionEvidenceProvider`.

    Emits only semantic, non-biometric interaction evidence at ground-truth
    moments; yields nothing unless the scenario enables vision.
    """

    def __init__(
        self, scenario: Scenario, registry: StoreRegistry, config: GeneratorConfig | None = None
    ) -> None:
        self._generator = VisionGenerator(scenario, registry, config)
        self._normalizer = ObservationNormalizer(registry, scenario.scenario_id)

    def evidence(self) -> Iterator[VisionEvidence]:
        for detection in self._generator.detections():
            yield self._normalizer.vision(detection)
