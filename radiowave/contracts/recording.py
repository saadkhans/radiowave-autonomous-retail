"""Recorder / replay entry contract.

A recording is an ordered sequence of :class:`RecordedEntry` values. Each entry
preserves the timestamp, entry kind, sensor, the full normalized payload
(including store-frame coordinates, confidence and retained native metadata)
and the scenario identifier, so any experiment can be replayed without hardware.

Format versions
---------------
* **v1** (Foundation v0): observations, proposals, decisions, cart events, ground
  truth and the ``STORE_TWIN`` / ``PIPELINE_CONFIG`` header. A v1 recording ends
  with its last observation; replay finishes the default way (one final step).
* **v2** (Observatory v0): adds the input-side entries a v1 reader cannot
  understand: ``CLOCK`` (an explicit clock advance without observation),
  ``DUPLICATE_OBSERVATION`` (an ingress attempt the deduplicator rejected, so replay
  exercises deduplication again) and ``RUN_END`` (the terminal entry; nothing
  follows it).

Every writer emits :data:`CURRENT_RECORDING_FORMAT_VERSION`; readers accept every
version in :data:`SUPPORTED_RECORDING_VERSIONS`, reject anything else explicitly,
and never reinterpret one version's entries under another's semantics. A single
recording uses one version throughout (:func:`validate_recording`).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import Field, field_validator, model_validator

from radiowave.contracts._base import ContractModel, UtcDatetime
from radiowave.contracts.observations import (
    AnyObservation,
    ItemObservation,
    PersonObservation,
    SensorObservation,
    VisionEvidence,
)
from radiowave.contracts.store import SourceType

RecordingFormatVersion = Literal[1, 2]

CURRENT_RECORDING_FORMAT_VERSION: Final[RecordingFormatVersion] = 2
SUPPORTED_RECORDING_VERSIONS: Final[frozenset[int]] = frozenset({1, 2})


class EntryKind(StrEnum):
    OBSERVATION = "OBSERVATION"
    RETAIL_EVENT = "RETAIL_EVENT"
    DECISION = "DECISION"
    CART_EVENT = "CART_EVENT"
    GROUND_TRUTH = "GROUND_TRUTH"
    STORE_TWIN = "STORE_TWIN"
    PIPELINE_CONFIG = "PIPELINE_CONFIG"
    # --- v2 ---------------------------------------------------------------------
    CLOCK = "CLOCK"  # explicit replay-clock advance (no observation), payload: timestamp
    DUPLICATE_OBSERVATION = "DUPLICATE_OBSERVATION"  # rejected duplicate ingress attempt
    RUN_END = "RUN_END"  # terminal entry of the run, payload: {"advance": bool}


# The lowest format version in which each kind may appear. Kinds absent here are v1.
KIND_MIN_VERSION: Final[dict[EntryKind, int]] = {
    EntryKind.CLOCK: 2,
    EntryKind.DUPLICATE_OBSERVATION: 2,
    EntryKind.RUN_END: 2,
}

# Entries that carry an observation payload and are fed back through intake on replay.
INPUT_KINDS: Final[frozenset[EntryKind]] = frozenset(
    {EntryKind.OBSERVATION, EntryKind.DUPLICATE_OBSERVATION}
)


class RecordingError(ValueError):
    """A recording violates the format contract (version, ordering or consistency)."""


class RecordedEntry(ContractModel):
    sequence: int = Field(ge=0)
    timestamp: UtcDatetime = Field(
        description="Recording position: for input entries the observation timestamp, for "
        "a DUPLICATE_OBSERVATION the pipeline clock at rejection, so a recording is always "
        "chronological"
    )
    kind: EntryKind
    source_type: SourceType | None = None
    sensor_id: str | None = None
    scenario_id: str | None = None
    format_version: RecordingFormatVersion = Field(
        default=CURRENT_RECORDING_FORMAT_VERSION,
        description="Recording format: 1 = Foundation v0, 2 = adds CLOCK, "
        "DUPLICATE_OBSERVATION and RUN_END; other versions are rejected on read",
    )
    payload: dict[str, Any]

    @field_validator("format_version", mode="before")
    @classmethod
    def _supported_version(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            msg = f"format_version must be an integer, got {value!r}"
            raise ValueError(msg)
        if value not in SUPPORTED_RECORDING_VERSIONS:
            msg = (
                f"unsupported recording format_version {value}; this reader understands "
                f"{sorted(SUPPORTED_RECORDING_VERSIONS)}"
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _kind_allowed_in_version(self) -> RecordedEntry:
        required = KIND_MIN_VERSION.get(self.kind, 1)
        if self.format_version < required:
            msg = (
                f"entry {self.sequence}: kind {self.kind.value} requires recording "
                f"format_version >= {required}, got {self.format_version}"
            )
            raise ValueError(msg)
        return self

    @classmethod
    def from_observation(
        cls, sequence: int, observation: SensorObservation, scenario_id: str | None = None
    ) -> RecordedEntry:
        return cls(
            sequence=sequence,
            timestamp=observation.timestamp,
            kind=EntryKind.OBSERVATION,
            source_type=observation.source_type,
            sensor_id=observation.sensor_id,
            scenario_id=scenario_id or observation.scenario_id,
            payload=observation.model_dump(mode="json"),
        )

    def to_observation(self) -> AnyObservation:
        """The observation carried by an OBSERVATION or DUPLICATE_OBSERVATION entry.

        A duplicate entry is stamped with the clock at which it was rejected, so only
        the envelope's sensor, modality and scenario are cross-checked against the
        payload; the payload keeps the attempt's own timestamp. That is the deliberate
        trade for a chronological file: a duplicate's payload timestamp is not
        envelope-checked, which is safe because replay only accepts the entry if the
        deduplicator rejects it again, and a rejected attempt mutates no state.
        """
        if self.kind not in INPUT_KINDS or self.source_type is None:
            msg = f"entry {self.sequence} is not an observation"
            raise ValueError(msg)
        if self.source_type == SourceType.MMWAVE:
            observation: AnyObservation = PersonObservation.model_validate(self.payload)
        elif self.source_type == SourceType.RFID:
            observation = ItemObservation.model_validate(self.payload)
        else:
            observation = VisionEvidence.model_validate(self.payload)
        if (
            (self.kind == EntryKind.OBSERVATION and observation.timestamp != self.timestamp)
            or observation.sensor_id != self.sensor_id
            or observation.source_type != self.source_type
            or (
                self.scenario_id is not None
                and observation.scenario_id is not None
                and observation.scenario_id != self.scenario_id
            )
        ):
            msg = f"entry {self.sequence}: observation payload disagrees with its envelope"
            raise ValueError(msg)
        return observation


def validate_recording(entries: Iterable[RecordedEntry]) -> Iterator[RecordedEntry]:
    """Yield ``entries`` while enforcing the stream-level contract.

    * one format version per recording: mixed streams are rejected, never reinterpreted;
    * ``RUN_END`` is terminal: an entry after it makes the recording invalid.

    Per-entry rules (supported version, kinds allowed in that version) are enforced by
    :class:`RecordedEntry` itself. Lazy, so paced replay is not buffered.
    """
    version: int | None = None
    ended_at: int | None = None
    for entry in entries:
        if version is None:
            version = entry.format_version
        elif entry.format_version != version:
            msg = (
                f"entry {entry.sequence}: recording mixes format versions "
                f"{version} and {entry.format_version}"
            )
            raise RecordingError(msg)
        if ended_at is not None:
            msg = (
                f"entry {entry.sequence} ({entry.kind.value}) follows RUN_END "
                f"(entry {ended_at}); a recording ends at RUN_END"
            )
            raise RecordingError(msg)
        if entry.kind == EntryKind.RUN_END:
            ended_at = entry.sequence
        yield entry
