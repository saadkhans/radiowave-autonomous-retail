# Schemas

- `json/` — JSON Schema generated from the Pydantic contracts in `radiowave/contracts`.
  Regenerate with `pnpm run schemas`; `pnpm run build` fails on drift.
- `proto/radiowave/v0/contracts.proto` — hand-maintained protobuf mirror of the same
  contracts for a future transport layer. Not compiled or imported in Foundation v0.

The Python models are the source of truth. Nothing in this directory references a
sensor vendor, a message broker or a database.
