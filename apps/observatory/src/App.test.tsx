import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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

async function selectAndRun() {
  render(<App />);
  const option = await screen.findByRole("option", { name: /one shopper picks one item/ });
  fireEvent.click(option);
  await screen.findByText(/A enters, walks to F1/);
  fireEvent.click(screen.getByRole("button", { name: /Run scenario 01/ }));
  await screen.findByText(/run run-0001/);
}

describe("App", () => {
  it("lists scenarios and shows the description before running", async () => {
    render(<App />);
    expect(await screen.findByRole("option", { name: /one shopper picks one item/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /ambiguous two-shopper pickup/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run scenario/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("option", { name: /ambiguous two-shopper pickup/ }));
    expect(await screen.findByText(/Two shoppers reach for the same shirt/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Run scenario 12/ })).toBeInTheDocument();
    expect(fake.calls).not.toContain("POST /api/runs");
  });

  it("creates a run at t=0 and renders the store map from the twin", async () => {
    await selectAndRun();
    expect(fake.calls).toContain("POST /api/runs");
    expect(screen.getByTestId("store-map")).toBeInTheDocument();
    expect(screen.getByTestId("fixture-F1")).toBeInTheDocument();
    expect(screen.getByTestId("sim-clock")).toHaveTextContent("t = 0.00s");
  });

  it("step advances simulated time by one interval and reset returns to zero", async () => {
    await selectAndRun();
    fireEvent.click(screen.getByRole("button", { name: "Step" }));
    await waitFor(() => expect(screen.getByTestId("sim-clock")).toHaveTextContent("t = 0.25s"));
    expect(screen.getByTestId("person-P0001")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(screen.getByTestId("sim-clock")).toHaveTextContent("t = 0.00s"));
    expect(screen.queryByTestId("person-P0001")).not.toBeInTheDocument();
    expect(fake.calls).toContain("POST /api/runs/run-0001/reset");
  });

  it("publishes run, events and timeline together so a reset never shows stale events", async () => {
    await selectAndRun();
    const slider = screen.getByRole("slider", { name: "Timeline" });
    fireEvent.change(slider, { target: { value: "9" } });
    fireEvent.mouseUp(slider);
    await waitFor(() => expect(screen.getByTestId("sim-clock")).toHaveTextContent("t = 9.00s"));
    const stream = screen.getByTestId("event-stream");
    expect(within(stream).getAllByRole("row").length).toBeGreaterThan(1);
    fake.failNext("POST /api/runs/run-0001/reset");
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await screen.findByRole("alert");
    // The reset failed: no partial state is published.
    expect(screen.getByTestId("sim-clock")).toHaveTextContent("t = 9.00s");
    expect(within(stream).getAllByRole("row").length).toBeGreaterThan(1);
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(screen.getByTestId("sim-clock")).toHaveTextContent("t = 0.00s"));
    expect(within(stream).queryAllByRole("row")).toHaveLength(2); // header + "No events yet."
  });

  it("locks scenario selection and restart while playing", async () => {
    await selectAndRun();
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    const other = screen.getByRole("option", { name: /ambiguous two-shopper pickup/ });
    expect(other).toBeDisabled();
    expect(screen.getByRole("button", { name: /Restart scenario 01/ })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(other).not.toBeDisabled());
  });

  it("grabbing the timeline pauses playback and the released scrub seeks", async () => {
    await selectAndRun();
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    const slider = screen.getByRole("slider", { name: "Timeline" });
    fireEvent.mouseDown(slider);
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
    fireEvent.change(slider, { target: { value: "4" } });
    fireEvent.mouseUp(slider);
    await waitFor(() => expect(screen.getByTestId("sim-clock")).toHaveTextContent("t = 4.00s"));
    expect(fake.calls).toContain("POST /api/runs/run-0001/seek");
  });

  it("labels an exit hold as a candidate while the cart is still open", async () => {
    fake.exitHold = true;
    await selectAndRun();
    const slider = screen.getByRole("slider", { name: "Timeline" });
    fireEvent.change(slider, { target: { value: "9" } });
    fireEvent.mouseUp(slider);
    await waitFor(() => expect(screen.getByTestId("sim-clock")).toHaveTextContent("t = 9.00s"));
    const cart = screen.getByTestId("cart-cart-P0001");
    expect(within(cart).getByText(/exit candidate/)).toBeInTheDocument();
    expect(within(cart).queryByText(/^exited/)).not.toBeInTheDocument();
  });

  it("play drives the API clock at the selected speed and pause stops it", async () => {
    await selectAndRun();
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "2x" }));
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    const advances = fake.calls.filter((call) => call === "POST /api/runs/run-0001/advance");
    expect(advances.length).toBeGreaterThanOrEqual(3);
    // 2x speed: every 200 ms tick asks the API for 0.4 s of simulated time.
    expect(fake.time()).toBeCloseTo(advances.length * 0.4, 5);
    fireEvent.click(screen.getByRole("button", { name: "Pause" }));
    const before = fake.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(fake.calls.length).toBe(before);
  });

  it("shows the cart, decisions and filtered events once the pick commits", async () => {
    await selectAndRun();
    fireEvent.click(screen.getByRole("button", { name: "Step" }));
    await waitFor(() => expect(screen.getByTestId("sim-clock")).toHaveTextContent("t = 0.25s"));
    // Seek by driving the timeline control.
    const slider = screen.getByRole("slider", { name: "Timeline" });
    fireEvent.change(slider, { target: { value: "9" } });
    fireEvent.mouseUp(slider);
    await waitFor(() => expect(screen.getByTestId("sim-clock")).toHaveTextContent("t = 9.00s"));

    const cart = screen.getByTestId("cart-cart-P0001");
    expect(within(cart).getByText("00A001")).toBeInTheDocument();
    expect(within(cart).getByText("OPEN")).toBeInTheDocument();

    const stream = screen.getByTestId("event-stream");
    expect(within(stream).getAllByRole("row")).toHaveLength(1 + 4); // header + 4 events at t <= 9
    fireEvent.click(within(stream).getByRole("button", { name: "PICK" }));
    const rows = within(stream).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("WAIT");
    expect(rows[1]).toHaveTextContent("COMMIT");

    // Clicking a row selects the EPC and the inspector shows its state.
    fireEvent.click(rows[1]);
    expect(screen.getByTestId("item-details")).toHaveTextContent("CARRIED");
    expect(screen.getByTestId("item-details")).toHaveTextContent("06281234567890");
  });
});
