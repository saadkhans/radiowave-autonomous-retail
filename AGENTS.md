# Engineering Agent Guide

## Project mission
Build a vendor-neutral autonomous retail R&D platform for apparel, footwear, accessories, electronics, and other uniquely RFID-tagged merchandise.

## Core interfaces to preserve
- PeopleTracker
- ItemTracker
- VisionEvidenceProvider
- FusionEngine
- CartEngine
- ConfidenceEngine
- Recorder
- ReplaySource

## Canonical retail events
- PICK
- CARRY
- PUTBACK
- MISPLACE
- HANDOFF
- EXIT_WITH_ITEM

## Engineering rules
- Prefer explicit, typed contracts over framework coupling.
- Make all algorithms replayable from recorded observations.
- Keep SKU identity separate from physical EPC/item identity.
- All timestamps are UTC and ordered/validated.
- Business-level identifiers must not depend on vendor-native IDs.
- Duplicate observations must be idempotent.
- Missing sensor samples must not corrupt state.
- Keep initial fusion explainable; use spatial, temporal, velocity, fixture-zone, and co-motion evidence before complex ML.
- Add real hardware adapters only after mock/simulator contracts are stable.

## Review priorities
Correctness > determinism > portability > observability > performance optimization.
