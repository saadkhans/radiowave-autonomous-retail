import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "@/App";
import { LIVE_POLL_MS } from "@/state/store";
import { installFakeApi } from "@/test/fixtures";

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

let fake: ReturnType<typeof installFakeApi>;

beforeEach(() => {
  fake = installFakeApi();
});

afterEach(() => {
  fake.restore();
  vi.useRealTimers();
});

describe("LIVE mode", () => {
  it("shows the sensor availability reason and disables Start when unavailable", async () => {
    fake.setLiveAvailability({ reason: "TI serial support not installed", sensor_id: null, sensor_name: null });
    render(<App />);

    expect(await screen.findByTestId("live-unavailable-reason")).toHaveTextContent(
      "TI serial support not installed",
    );
    expect(screen.getByRole("button", { name: "Start live" })).toBeDisabled();
    expect(fake.calls).not.toContain("POST /api/runs/live");
  });

  it("starting a live run calls POST /runs/live and shows the LIVE badge and radar status", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Start live" });

    fireEvent.click(screen.getByRole("checkbox", { name: /capture normalized recording/i }));
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));

    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));
    expect(fake.calls).toContain("POST /api/runs/live");

    // Radar status panel and live controls are visible; replay controls are not.
    expect(screen.getByTestId("live-controls")).toBeInTheDocument();
    expect(screen.getByTestId("live-status-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("replay-controls")).not.toBeInTheDocument();
    expect(screen.queryByRole("slider", { name: "Timeline" })).not.toBeInTheDocument();
  });

  it("sends the capture flag exactly as chosen", async () => {
    const originalFetch = globalThis.fetch;
    const bodies: unknown[] = [];
    const spied: typeof fetch = async (input, init) => {
      if (String(input).endsWith("/api/runs/live") && init?.body) {
        bodies.push(JSON.parse(String(init.body)));
      }
      return originalFetch(input, init);
    };
    globalThis.fetch = spied;

    render(<App />);
    await screen.findByRole("button", { name: "Start live" });
    fireEvent.click(screen.getByRole("checkbox", { name: /capture normalized recording/i }));
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ capture: true });
    globalThis.fetch = originalFetch;
  });

  it("polls the snapshot endpoint on an interval and never calls advance while LIVE", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Start live" });
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));

    await waitFor(
      () =>
        expect(
          fake.calls.filter((call) => call === "GET /api/runs/live-0001/snapshot").length,
        ).toBeGreaterThan(1),
      { timeout: 3000 },
    );
    expect(fake.calls.some((call) => call.includes("/advance"))).toBe(false);
  }, 10000);

  it("a poll in flight suppresses the next poll tick", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Start live" });
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));

    const release = fake.hold("GET /api/runs/live-0001/snapshot");
    await waitFor(() =>
      expect(fake.calls.filter((call) => call === "GET /api/runs/live-0001/snapshot")).toHaveLength(1),
    );

    // Several more poll intervals' worth of real time pass while the first
    // poll is still held: pendingCount stays > 0, so runExclusive refuses
    // every later tick outright instead of overlapping it.
    await sleep(LIVE_POLL_MS * 3);
    expect(fake.calls.filter((call) => call === "GET /api/runs/live-0001/snapshot")).toHaveLength(1);

    release();
    await waitFor(() =>
      expect(
        fake.calls.filter((call) => call === "GET /api/runs/live-0001/snapshot").length,
      ).toBeGreaterThan(1),
    );
  }, 10000);

  it("Stop calls the stop endpoint and polling ceases", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Start live" });
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/live-0001/stop"));

    // Once stopped the run is finished: LiveControls falls back to the "Start
    // live" panel (the sensor is free again), and the poll effect tears down.
    await waitFor(() => expect(screen.getByRole("button", { name: "Start live" })).toBeInTheDocument());

    const before = fake.calls.filter((call) => call.startsWith("GET /api/runs/live-0001/snapshot")).length;
    await sleep(LIVE_POLL_MS * 3);
    const after = fake.calls.filter((call) => call.startsWith("GET /api/runs/live-0001/snapshot")).length;
    expect(after).toBe(before);
  }, 10000);

  it("Reconnect calls the reconnect endpoint", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Start live" });
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));

    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/live-0001/reconnect"));
  });

  it("a run replacement while a live poll is in flight is refused, and the stale run's data never lands on the new run", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Start live" });
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));

    const release = fake.hold("GET /api/runs/live-0001/snapshot");
    await waitFor(() =>
      expect(fake.calls.filter((call) => call === "GET /api/runs/live-0001/snapshot")).toHaveLength(1),
    );

    // Stop is queued (runQueued: never silently dropped) behind the in-flight
    // poll, so it cannot land until the held poll settles.
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await sleep(100);
    expect(fake.calls).not.toContain("POST /api/runs/live-0001/stop");

    release();
    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/live-0001/stop"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start live" })).toBeInTheDocument());

    // Starting a second live run now succeeds, and it - not the discarded
    // live-0001 - is what's displayed.
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));
    await waitFor(() => expect(screen.getByText(/run live-0002/)).toBeInTheDocument());
  }, 10000);

  it("shows persons on the map for a live store with no fixtures", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Start live" });
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));
    expect(await screen.findByTestId("store-map")).toBeInTheDocument();
    // Cart/inspector panels tolerate an empty run without throwing.
    expect(within(screen.getByTestId("cart-panel")).getByText("No carts.")).toBeInTheDocument();
  });
});
