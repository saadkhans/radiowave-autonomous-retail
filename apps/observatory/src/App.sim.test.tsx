import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "@/App";
import { installFakeApi } from "@/test/fixtures";

let fake: ReturnType<typeof installFakeApi>;

beforeEach(() => {
  fake = installFakeApi();
});

afterEach(() => {
  fake.restore();
  vi.useRealTimers();
});

/**
 * The Start button is disabled until the SIM controls panel has both loaded
 * the scenario catalog and auto-selected a default scenario (a second,
 * effect-driven render pass) - waiting for it directly avoids a race against
 * that second pass.
 */
async function findEnabledStartSimButton() {
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Start simulation" })).toBeEnabled(),
  );
  return screen.getByRole("button", { name: "Start simulation" });
}

describe("SIM mode", () => {
  it("starting a sim run calls POST /runs/sim and shows an unmistakable SIMULATED badge", async () => {
    render(<App />);
    fireEvent.click(await findEnabledStartSimButton());

    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/sim"));

    const badge = await screen.findByTestId("mode-badge");
    // Distinct from the hardware LIVE badge: different text AND colour class
    // (never the danger-red used for hardware LIVE), plus the scenario id and
    // seed inline so a screenshot of the badge alone is self-describing.
    expect(badge).toHaveTextContent("SIMULATED");
    expect(badge).toHaveTextContent("acceptance_60s");
    expect(badge).toHaveTextContent("seed 7");
    expect(badge.className).not.toContain("console-danger");

    // The reused LIVE panel is present (Stop works) but its own inline badge
    // must also read SIMULATED, never LIVE, for the same reason.
    expect(screen.getByTestId("live-controls")).toBeInTheDocument();
    expect(screen.getByTestId("mode-badge-live")).toHaveTextContent("SIMULATED");
    expect(screen.getByTestId("mode-badge-live").className).not.toContain("console-danger");
  });

  it("sends the chosen scenario id and seed override exactly", async () => {
    const originalFetch = globalThis.fetch;
    const bodies: unknown[] = [];
    const spied: typeof fetch = async (input, init) => {
      if (String(input).endsWith("/api/runs/sim") && init?.body) {
        bodies.push(JSON.parse(String(init.body)));
      }
      return originalFetch(input, init);
    };
    globalThis.fetch = spied;

    render(<App />);
    const startButton = await findEnabledStartSimButton();
    fireEvent.change(screen.getByRole("spinbutton", { name: /seed override/i }), {
      target: { value: "99" },
    });
    fireEvent.click(startButton);

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ scenario_id: "acceptance_60s", seed: 99 });
    globalThis.fetch = originalFetch;
  });

  it("Advance and Step drive the sim clock forward, and Reset rebuilds it", async () => {
    render(<App />);
    fireEvent.click(await findEnabledStartSimButton());
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("SIMULATED"));

    fireEvent.click(screen.getByRole("button", { name: "Step" }));
    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/sim-0001/step"));

    fireEvent.click(screen.getByRole("button", { name: "Advance" }));
    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/sim-0001/advance"));

    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/sim-0001/reset"));
  });

  it("renders the scenario's ground truth on the timeline, distinct from the system's own decision markers", async () => {
    render(<App />);
    fireEvent.click(await findEnabledStartSimButton());
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("SIMULATED"));

    const timeline = screen.getByTestId("timeline");
    const groundTruthMark = timeline.querySelector('[title^="ground truth"]');
    expect(groundTruthMark).not.toBeNull();
  });

  it("starting a sim run stops an active hardware live run first", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Start live" }));
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));

    fireEvent.click(await findEnabledStartSimButton());
    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/live-0001/stop"));
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("SIMULATED"));
  });
});
