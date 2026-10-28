import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import App from "@/App";
import { installFakeApi } from "@/test/fixtures";
import type { SimRfidBench } from "@/types/api";

const SHIRT = "E2801160600002000000A001";

function bench(state: "ON_SHELF" | "CARRIED", carrier: string | null = null): SimRfidBench {
  return {
    run_id: "live-0001",
    enabled: true,
    reason: null,
    attribution_simulated: true,
    items: [
      {
        epc: SHIRT,
        short_epc: "…A001",
        gtin: "4006381333931",
        product_name: "Folded shirt",
        home_fixture_id: "shelf-a",
        home_fixture_name: "Shelf A",
        state,
        carrier_track_id: carrier,
      },
    ],
  };
}

let fake: ReturnType<typeof installFakeApi>;
let actions: unknown[];
let refuseNextPick: string | null;

beforeEach(() => {
  fake = installFakeApi();
  actions = [];
  refuseNextPick = null;
  let current = bench("ON_SHELF");
  const inner = globalThis.fetch;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    if (url.pathname.endsWith("/sim-rfid")) {
      const json = (body: unknown, status = 200) =>
        new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
      if ((init?.method ?? "GET") === "GET") return json(current);
      const body = JSON.parse(String(init?.body)) as { action: string; epc: string };
      actions.push(body);
      if (body.action === "pick" && refuseNextPick) {
        const detail = refuseNextPick;
        refuseNextPick = null;
        return json({ detail }, 409);
      }
      current = body.action === "pick" ? bench("CARRIED", "P0001") : bench("ON_SHELF");
      return json(current);
    }
    return inner(input, init);
  }) as typeof fetch;
});

afterEach(() => {
  fake.restore();
});

async function startLive() {
  render(<App />);
  await screen.findByRole("button", { name: "Start live" });
  fireEvent.click(screen.getByRole("button", { name: "Start live" }));
  await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));
}

describe("Simulated RFID panel", () => {
  it("is absent before a live run starts", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Start live" });
    expect(screen.queryByTestId("sim-rfid-panel")).not.toBeInTheDocument();
  });

  it("lists items, says the reads are simulated, and picks / puts back", async () => {
    await startLive();
    const panel = await screen.findByTestId("sim-rfid-panel");
    expect(within(panel).getByTestId("sim-rfid-note")).toHaveTextContent("item reads are simulated");
    expect(within(panel).getByText(/on shelf · Shelf A/)).toBeInTheDocument();

    fireEvent.click(within(panel).getByRole("button", { name: "Pick up" }));
    await waitFor(() => expect(within(panel).getByText("carried by P0001")).toBeInTheDocument());
    expect(actions).toEqual([{ action: "pick", epc: SHIRT }]);

    fireEvent.click(within(panel).getByRole("button", { name: "Put back" }));
    await waitFor(() => expect(within(panel).getByRole("button", { name: "Pick up" })).toBeInTheDocument());
    expect(actions).toEqual([
      { action: "pick", epc: SHIRT },
      { action: "put_back", epc: SHIRT },
    ]);
  });

  it("shows why a pick was refused", async () => {
    refuseNextPick = "nobody is near shelf-a: the closest tracked person (P0002) is 3.1 m away";
    await startLive();
    const panel = await screen.findByTestId("sim-rfid-panel");
    fireEvent.click(within(panel).getByRole("button", { name: "Pick up" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("nobody is near shelf-a");
  });
});
