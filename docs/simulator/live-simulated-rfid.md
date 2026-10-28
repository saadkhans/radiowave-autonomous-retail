# Simulated RFID on a live radar run

Exercise radar + RFID + fusion + carts end to end with a **real mmWave radar** and **no RFID
hardware**. The operator presses buttons in the Observatory ("Pick up", "Put back"); a virtual
reader turns that into RFID reads that enter the live pipeline through the production
normalizer, on the radar's clock.

Code: `radiowave/api/live_rfid.py` (`OperatorRfidBench`), wired into `LiveObservatoryRun.tick`
(`radiowave/api/live.py`); routes `GET/POST /api/runs/{id}/sim-rfid`; Observatory panel
`apps/observatory/src/components/SimRfidPanel.tsx`.

## What it tests, and what it does not

| Tested | Not tested |
| --- | --- |
| Live canonical person tracks from the real radar | **Who took the item.** After a pick the item follows the radar person nearest its shelf, so attribution is the operator's input, not fusion's inference |
| Item tracks, PICK / CARRY / PUTBACK / EXIT_WITH_ITEM events, the confidence engine, carts | RFID physics: read range, body blocking, zone leakage, metal/liquids |
| Capture + replay of a mixed radar/RFID live session | Any real reader's data format |

The panel and the API both say so (`attribution_simulated: true`).

## Running it

1. Use a `TiLiveConfig` whose twin has, in addition to the radar:
   - an RFID sensor with `vendor_metadata: {"simulated": "true"}` (only these are emulated, so a
     real reader added later is never impersonated); optional `max_range_m` (default 6);
   - fixtures with `FIXTURE` zones that include the floor a shopper stands on to reach the shelf
     (0.6 m around the shelf, as in the Virtual Store Lab) — fusion's zone evidence depends on it;
   - an `EXIT` **zone** over the door (the `boundaries` entry alone is not enough), and the EXIT
     boundary;
   - products and items with home fixtures.

   `configs/examples/ti-iwr6843-lab-sim-rfid.json` is a complete example (two shelves, four items).
2. Start the API with it: `RADIOWAVE_TI_CONFIG=<that file> pnpm run dev`, start a live run.
3. The **Simulated RFID** panel lists the items. Stand at a shelf, press **Pick up**: the item
   attaches to the tracked person nearest that shelf (refused if nobody is within 2.5 m). Walk
   around carrying it, then leave through the doorway, or press **Put back**.

## Findings while building it

- **Fusion's PICK needs localized item positions.** Reads that only say "tag seen at this
  antenna" never move an item out of `ON_FIXTURE`, so they produce no PICK or EXIT. The Virtual
  Store Lab hides this by localizing reads from the item's true position (0.5 m blur); the bench
  does the same. Cheap fixed readers (and an Impinj R700 out of the box) are zone-only, so real
  RFID hardware will need either reader-side localization or zone-based PICK inference in fusion.
  This is Phase 5/6 work.
- **Read rate matters.** At the lab's 2 polls/s the item's movement was noticed seconds after the
  pick, by when the shopper had left the shelf's zone; PICK confidence peaked at ~0.4–0.6 and never
  reached the 0.75 COMMIT threshold, so EXIT_WITH_ITEM committed with no shopper. At 10 polls/s
  (the bench default, ~5 reads/s at room range) PICK, CARRY and EXIT_WITH_ITEM all committed to
  the walker in the scripted test.
- **Even then, PICK confidence is marginal** (~0.76–0.78 against 0.75) for one shopper with the
  item exactly following them. Small changes in read rate or timing can drop it below COMMIT.
