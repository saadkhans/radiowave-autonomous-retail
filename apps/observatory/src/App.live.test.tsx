import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import App from "@/App";
import { api } from "@/lib/api";
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

  it("invariant 17: starting a replay scenario while LIVE is active stops the live run first", async () => {
    render(<App />);
    // Scenario is selected before LIVE starts; the selection (and its "Run
    // scenario" button) survives starting a live run untouched.
    fireEvent.click(await screen.findByRole("option", { name: /one shopper picks one item/ }));
    await screen.findByRole("button", { name: /Run scenario 01/ });

    fireEvent.click(screen.getByRole("button", { name: "Start live" }));
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));
    // With a run active, the scenario panel's action relabels to "Restart" but
    // still calls the same startRun().
    await screen.findByRole("button", { name: /Restart scenario 01/ });

    const releaseStop = fake.hold("POST /api/runs/live-0001/stop");
    fireEvent.click(screen.getByRole("button", { name: /Restart scenario 01/ }));

    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/live-0001/stop"));
    // The replay run is not created while the stop is still held.
    expect(fake.calls).not.toContain("POST /api/runs");

    releaseStop();
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("REPLAY"));
    await screen.findByText(/run run-0001/);
    expect(screen.queryByTestId("live-status-panel")).not.toBeInTheDocument();
    expect(screen.queryByTestId("live-controls")).not.toBeInTheDocument();

    // The old live run's poll loop is torn down: no further snapshot polls for it.
    const before = fake.calls.filter((call) => call === "GET /api/runs/live-0001/snapshot").length;
    await sleep(LIVE_POLL_MS * 3);
    const after = fake.calls.filter((call) => call === "GET /api/runs/live-0001/snapshot").length;
    expect(after).toBe(before);
  }, 10000);

  it("invariant 17: a failed stop keeps the live run and never creates the replay run", async () => {
    render(<App />);
    fireEvent.click(await screen.findByRole("option", { name: /one shopper picks one item/ }));
    await screen.findByRole("button", { name: /Run scenario 01/ });

    fireEvent.click(screen.getByRole("button", { name: "Start live" }));
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));
    await screen.findByRole("button", { name: /Restart scenario 01/ });

    fake.failNext("POST /api/runs/live-0001/stop");
    fireEvent.click(screen.getByRole("button", { name: /Restart scenario 01/ }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(fake.calls).not.toContain("POST /api/runs");
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE");
    expect(screen.getByText(/run live-0001/)).toBeInTheDocument();
  });

  it("invariant 18: a live store fetch failure stops the new live run and surfaces the error", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Start live" });
    fake.failNext("GET /api/runs/live-0001/store");
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));

    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/live-0001/stop"));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("button", { name: "Start live" })).toBeEnabled());

    // Mode never became LIVE, so no live polling ever starts.
    expect(screen.queryByTestId("mode-badge")).not.toBeInTheDocument();
    expect(fake.calls.filter((call) => call === "GET /api/runs/live-0001/snapshot")).toHaveLength(0);
    await sleep(LIVE_POLL_MS * 3);
    expect(fake.calls.filter((call) => call === "GET /api/runs/live-0001/snapshot")).toHaveLength(0);
  }, 10000);

  it("invariant 18: a live store fetch failure AND a failed stop surface the run id and both errors", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Start live" });
    fake.failNext("GET /api/runs/live-0001/store");
    fake.failNext("POST /api/runs/live-0001/stop");
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));

    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/live-0001/stop"));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("live run live-0001");
    expect(alert).toHaveTextContent("could not be stopped after its store failed to load");
    expect(alert).toHaveTextContent("stop failed");
    expect(alert).toHaveTextContent("stop it manually");
    // The queue is no longer stuck (not busy) and the Start panel is back -
    // the injected stop failure means the fake server genuinely never
    // released the sensor, so Start correctly stays disabled with a reason
    // rather than silently pretending the run was cleaned up.
    await waitFor(() => expect(screen.getByRole("button", { name: "Start live" })).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Start live" })).toBeDisabled();
    expect(await screen.findByTestId("live-unavailable-reason")).toBeInTheDocument();

    // Mode never became LIVE, so no live polling ever starts.
    expect(screen.queryByTestId("mode-badge")).not.toBeInTheDocument();
    expect(fake.calls.filter((call) => call === "GET /api/runs/live-0001/snapshot")).toHaveLength(0);
    await sleep(LIVE_POLL_MS * 3);
    expect(fake.calls.filter((call) => call === "GET /api/runs/live-0001/snapshot")).toHaveLength(0);
  }, 10000);

  it("invariant 19: an active run reported by another client offers a resume control and keeps Start live disabled", async () => {
    // Simulates the server already owning a LIVE run (e.g. from before a page
    // reload) that this fresh client is not bound to.
    await api.createLiveRun(false);

    render(<App />);

    const resumeButton = await screen.findByRole("button", { name: /Resume live run live-0001/ });
    expect(resumeButton).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start live" })).toBeDisabled();
    expect(screen.queryByTestId("mode-badge")).not.toBeInTheDocument();
  });

  it("invariant 19: clicking resume binds the client to the existing run so Stop and Reconnect operate on it", async () => {
    await api.createLiveRun(false);

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: /Resume live run live-0001/ }));

    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));
    expect(screen.queryByRole("button", { name: /Resume live run/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Reconnect" }));
    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/live-0001/reconnect"));

    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(fake.calls).toContain("POST /api/runs/live-0001/stop"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Start live" })).toBeInTheDocument());
  });

  it("invariant 19: a failed store fetch during adopt surfaces the error, does not bind the run, and leaves it adoptable", async () => {
    await api.createLiveRun(false);

    render(<App />);
    fake.failNext("GET /api/runs/live-0001/store");
    fireEvent.click(await screen.findByRole("button", { name: /Resume live run live-0001/ }));

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    // Not bound: no mode badge, and the failed adopt must not have stopped
    // the run (unlike startLiveRun, adopt never tears down a run it did not
    // create).
    expect(screen.queryByTestId("mode-badge")).not.toBeInTheDocument();
    expect(fake.calls).not.toContain("POST /api/runs/live-0001/stop");
    // Still adoptable: the resume control is still offered for the same run.
    expect(await screen.findByRole("button", { name: /Resume live run live-0001/ })).toBeInTheDocument();
  });

  it("invariant 19: the resume control is not shown once the client is already bound to that run id", async () => {
    render(<App />);
    await screen.findByRole("button", { name: "Start live" });
    fireEvent.click(screen.getByRole("button", { name: "Start live" }));
    await waitFor(() => expect(screen.getByTestId("mode-badge")).toHaveTextContent("LIVE"));

    expect(screen.queryByRole("button", { name: /Resume live run/i })).not.toBeInTheDocument();
  });
});
