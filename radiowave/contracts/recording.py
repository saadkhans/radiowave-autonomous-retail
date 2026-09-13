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

Two distinct constants govern the field:

* the **read default** is v1 (:data:`LEGACY_RECORDING_FORMAT_VERSION`): Foundation v0
  wrote ``format_version`` as an optional field defaulting to 1, so a legacy row that
  omits it is still v1 and a legacy stream may mix omitted and explicit-1 rows;
* the **writer version** is v2 (:data:`CURRENT_RECORDING_FORMAT_VERSION`): every new
  writer sets it explicitly. The model default is never used to decide what is written.

Readers accept every version in :data:`SUPPORTED_RECORDING_VERSIONS`, reject anything
else explicitly, and never reinterpret one version's entries under another's
semantics. A single recording uses one version throughout, its sequence numbers
strictly increase, its envelope timestamps never move backwards, and a v2 recording
ends with ``RUN_END`` (:func:`validate_recording`); a v1 recording ends at EOF.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from radiowave.contracts._base import ContractModel, UtcDatetime, ensure_utc
from radiowave.contracts.observations import (
    AnyObservation,
    ItemObservation,
    PersonObservation,
    SensorObservation,
    VisionEvidence,
)
from radiowave.contracts.store import SourceType

RecordingFormatVersion = Literal[1, 2]

# Read default: what an entry that omits ``format_version`` means (Foundation v0 data).
LEGACY_RECORDING_FORMAT_VERSION: Final[RecordingFormatVersion] = 1
# Writer version: what every new recording explicitly declares.
CURRENT_RECORDING_FORMAT_VERSION: Final[RecordingFormatVersion] = 2
SUPPORTED_RECORDING_VERSIONS: Final[frozenset[int]] = frozenset({1, 2})
# Backward-compatible import alias: Foundation v0 exposed this name (then equal to 1) as
# the version its writer emitted, so it keeps meaning "the format the current
# implementation writes". New code should use CURRENT_RECORDING_FORMAT_VERSION.
RECORDING_FORMAT_VERSION: Final[RecordingFormatVersion] = CURRENT_RECORDING_FORMAT_VERSION


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
        default=LEGACY_RECORDING_FORMAT_VERSION,
        description="Recording format: 1 = Foundation v0 (the default when the field is "
        "absent, for legacy data), 2 = adds CLOCK, DUPLICATE_OBSERVATION and RUN_END (what "
        "every new writer declares explicitly); other versions are rejected on read",
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

    @model_validator(mode="after")
    def _control_payloads_are_typed(self) -> RecordedEntry:
        """v2 control entries carry a fixed, typed payload, never an arbitrary dict.

        * ``RUN_END``: exactly ``{"advance": <bool>}``; ``"false"``, ``0`` or ``None`` are
          rejected, never coerced, because the flag decides whether a terminal fusion
          step runs.
        * ``CLOCK``: the envelope timestamp is authoritative; the payload's
          ``timestamp`` must parse to the same UTC instant so a recording never carries
          two contradictory clock values.
        * input entries name the sensor and modality that replay cross-checks.
        """
        if self.kind == EntryKind.RUN_END:
            if set(self.payload) != {"advance"} or type(self.payload["advance"]) is not bool:
                msg = (
                    f"entry {self.sequence}: RUN_END payload must be "
                    f'{{"advance": <bool>}}, got {self.payload!r}'
                )
                raise ValueError(msg)
        elif self.kind == EntryKind.CLOCK:
            raw = self.payload.get("timestamp") if set(self.payload) == {"timestamp"} else None
            if not isinstance(raw, str):
                msg = (
                    f"entry {self.sequence}: CLOCK payload must be "
                    f'{{"timestamp": <ISO-8601 UTC>}}, got {self.payload!r}'
                )
                raise ValueError(msg)
            try:
                stamp = ensure_utc(datetime.fromisoformat(raw))
            except ValueError as exc:
                msg = f"entry {self.sequence}: CLOCK payload timestamp {raw!r} is invalid: {exc}"
                raise ValueError(msg) from exc
            if stamp != self.timestamp:
                msg = (
                    f"entry {self.sequence}: CLOCK payload timestamp {raw!r} disagrees with "
                    f"the envelope timestamp {self.timestamp.isoformat()}"
                )
                raise ValueError(msg)
        elif self.kind in INPUT_KINDS and (self.source_type is None or self.sensor_id is None):
            msg = f"entry {self.sequence}: {self.kind.value} entries must name sensor and modality"
            raise ValueError(msg)
        return self

    @property
    def run_end_advance(self) -> bool:
        """The validated ``advance`` flag of a ``RUN_END`` entry."""
        if self.kind != EntryKind.RUN_END:
            msg = f"entry {self.sequence} is not a RUN_END entry"
            raise RecordingError(msg)
        advance: bool = self.payload["advance"]
        return advance

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
            format_version=CURRENT_RECORDING_FORMAT_VERSION,
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
            raise RecordingError(msg)
        try:
            if self.source_type == SourceType.MMWAVE:
                observation: AnyObservation = PersonObservation.model_validate(self.payload)
            elif self.source_type == SourceType.RFID:
                observation = ItemObservation.model_validate(self.payload)
            else:
                observation = VisionEvidence.model_validate(self.payload)
        except ValidationError as exc:
            errors = exc.errors()
            where = ".".join(str(part) for part in errors[0]["loc"]) if errors else ""
            reason = errors[0]["msg"] if errors else "invalid"
            msg = (
                f"entry {self.sequence}: observation payload is invalid"
                f"{f' at {where}' if where else ''}: {reason}"
            )
            raise RecordingError(msg) from exc
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
            raise RecordingError(msg)
        return observation


def validate_recording(entries: Iterable[RecordedEntry]) -> Iterator[RecordedEntry]:
    """Yield ``entries`` while enforcing the stream-level contract.

    * one format version per recording: mixed streams are rejected, never reinterpreted;
    * ``sequence`` strictly increases (an ordered event log, not a bag of rows);
    * envelope timestamps never move backwards (equal timestamps are fine). For a
      ``DUPLICATE_OBSERVATION`` the envelope carries the pipeline clock at rejection,
      never the attempt's own timestamp, so a stale or future-stamped duplicate payload
      does not affect ordering;
    * ``RUN_END`` is terminal: an entry after it makes the recording invalid;
    * a v2 recording must end with ``RUN_END``: EOF without it means a truncated,
      corrupted or still-being-written file, never a completed run. A v1 recording
      predates ``RUN_END`` and legitimately ends at EOF.

    Per-entry rules (supported version, kinds allowed in that version, typed control
    payloads) are enforced by :class:`RecordedEntry` itself. Lazy, so paced replay is
    not buffered; the completeness check runs when the source is exhausted.
    """
    version: int | None = None
    ended_at: int | None = None
    previous: RecordedEntry | None = None
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
        if previous is not None:
            if entry.sequence <= previous.sequence:
                msg = (
                    f"entry {entry.sequence} does not follow entry {previous.sequence}; "
                    "recording sequence numbers must strictly increase"
                )
                raise RecordingError(msg)
            if entry.timestamp < previous.timestamp:
                msg = (
                    f"entry {entry.sequence} ({entry.kind.value}) at "
                    f"{entry.timestamp.isoformat()} precedes entry {previous.sequence} at "
                    f"{previous.timestamp.isoformat()}; recording timestamps never move "
                    "backwards"
                )
                raise RecordingError(msg)
        if entry.kind == EntryKind.RUN_END:
            ended_at = entry.sequence
        previous = entry
        yield entry
    if version is not None and version >= 2 and ended_at is None:
        last = previous.sequence if previous is not None else "<none>"
        msg = (
            f"v{version} recording ended without RUN_END after entry {last}; the file is "
            "truncated, corrupted or still being written"
        )
        raise RecordingError(msg)
