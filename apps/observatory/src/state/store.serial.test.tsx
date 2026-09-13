import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ReplayControls } from "@/components/ReplayControls";
import { ObservatoryProvider, TICK_MS, useActions, useObservatory } from "@/state/store";
import type { ObservatoryActions } from "@/state/store";
import { installFakeApi } from "@/test/fixtures";

let fake: ReturnType<typeof installFakeApi>;

beforeEach(() => {
  fake = installFakeApi();
});

afterEach(() => {
  fake.restore();
  vi.useRealTimers();
});

type ActionsRef = { current: ObservatoryActions | null };

function Harness({ actionsRef }: { actionsRef: ActionsRef }) {
  const actions = useActions();
  const { busy, playing, run, error } = useObservatory();
  actionsRef.current = actions;
  return (
    <div>
      <span data-testid="busy">{String(busy)}</span>
      <span data-testid="playing">{String(playing)}</span>
      <span data-testid="time">{run ? run.time_s.toFixed(2) : "none"}</span>
      <span data-testid="run-id">{run ? run.run_id : "none"}</span>
      <span data-testid="error">{error ?? ""}</span>
    </div>
  );
}

function renderHarness(): ActionsRef {
  const actionsRef: ActionsRef = { current: null };
  render(
    <ObservatoryProvider>
      <Harness actionsRef={actionsRef} />
    </ObservatoryProvider>,
  );
  return actionsRef;
}

/** Same as renderHarness but also mounts ReplayControls, for UI-state assertions. */
function renderHarnessWithControls(): ActionsRef {
  const actionsRef: ActionsRef = { current: null };
  render(
    <ObservatoryProvider>
      <Harness actionsRef={actionsRef} />
      <ReplayControls />
    </ObservatoryProvider>,
  );
  return actionsRef;
}

/** Selects scenario 01 and starts a run (run-0001), awaiting both. */
async function selectAndStart(actionsRef: ActionsRef) {
  await act(async () => {
    await actionsRef.current!.selectScenario("01");
  });
  await act(async () => {
    await actionsRef.current!.startRun();
  });
  await waitFor(() => expect(screen.getByTestId("run-id")).toHaveTextContent("run-0001"));
}

function callsTo(route: string): number {
  return fake.calls.filter((call) => call === route).length;
}

const SEEK = "POST /api/runs/run-0001/seek";
const STEP = "POST /api/runs/run-0001/step";
const ADVANCE = "POST /api/runs/run-0001/advance";
const RESET = "POST /api/runs/run-0001/reset";
const CREATE_RUN = "POST /api/runs";

describe("serial executor", () => {
  it("three queued seeks are strictly serialized", async () => {
    const actionsRef = renderHarness();
    await selectAndStart(actionsRef);

    const release1 = fake.hold(SEEK);
    const release2 = fake.hold(SEEK);
    const release3 = fake.hold(SEEK);

    act(() => {
      void actionsRef.current!.seek(5);
      void actionsRef.current!.seek(9);
      void actionsRef.current!.seek(3);
    });

    await waitFor(() => expect(callsTo(SEEK)).toBe(1));
    release1();
    await waitFor(() => expect(callsTo(SEEK)).toBe(2));
    release2();
    await waitFor(() => expect(callsTo(SEEK)).toBe(3));
    release3();

    await waitFor(() => expect(screen.getByTestId("time")).toHaveTextContent("3.00"));
  });

  it("busy stays true across queued operations until the last completes", async () => {
    const actionsRef = renderHarness();
    await selectAndStart(actionsRef);

    const release1 = fake.hold(SEEK);
    const release2 = fake.hold(SEEK);
    const release3 = fake.hold(SEEK);

    act(() => {
      void actionsRef.current!.seek(5);
      void actionsRef.current!.seek(9);
      void actionsRef.current!.seek(3);
    });
    expect(screen.getByTestId("busy")).toHaveTextContent("true");

    await waitFor(() => expect(callsTo(SEEK)).toBe(1));
    release1();
    await waitFor(() => expect(callsTo(SEEK)).toBe(2));
    expect(screen.getByTestId("busy")).toHaveTextContent("true");

    release2();
    await waitFor(() => expect(callsTo(SEEK)).toBe(3));
    expect(screen.getByTestId("busy")).toHaveTextContent("true");

    release3();
    await waitFor(() => expect(screen.getByTestId("busy")).toHaveTextContent("false"));
  });

  it("a failed queued request does not poison the queue", async () => {
    const actionsRef = renderHarness();
    await selectAndStart(actionsRef);

    // Hold both seeks so the second cannot overwrite the error dispatched by
    // the first's failure before we get a chance to observe it.
    const release1 = fake.hold(SEEK);
    const release2 = fake.hold(SEEK);
    fake.failNext(SEEK);
    act(() => {
      void actionsRef.current!.seek(2);
      void actionsRef.current!.seek(4);
    });

    await waitFor(() => expect(callsTo(SEEK)).toBe(1));
    release1();
    await waitFor(() => expect(callsTo(SEEK)).toBe(2));
    await waitFor(() => expect(screen.getByTestId("error")).not.toHaveTextContent(""));

    release2();
    await waitFor(() => expect(screen.getByTestId("time")).toHaveTextContent("4.00"));
    await waitFor(() => expect(screen.getByTestId("busy")).toHaveTextContent("false"));

    // A later step still works: the failed seek did not poison the chain.
    act(() => {
      void actionsRef.current!.step();
    });
    await waitFor(() => expect(screen.getByTestId("time")).toHaveTextContent("4.25"));
  });

  it("a stale response for an older run/generation is ignored", async () => {
    const actionsRef = renderHarness();
    await selectAndStart(actionsRef);

    const releaseStep = fake.hold(STEP);
    act(() => {
      void actionsRef.current!.step();
    });
    await waitFor(() => expect(callsTo(STEP)).toBe(1));

    // reset is refused outright: runExclusive sees a task already queued/running.
    let refused: Promise<void> | undefined;
    act(() => {
      refused = actionsRef.current!.reset();
    });
    await refused;
    expect(fake.calls).not.toContain(RESET);

    releaseStep();
    await waitFor(() => expect(screen.getByTestId("time")).toHaveTextContent("0.25"));

    // Now nothing is pending: reset proceeds, bumping the generation.
    act(() => {
      void actionsRef.current!.reset();
    });
    await waitFor(() => expect(fake.calls).toContain(RESET));
    await waitFor(() => expect(screen.getByTestId("time")).toHaveTextContent("0.00"));

    // A held response landing after a generation bump is not reachable through
    // the public actions: runExclusive/runQueued serialize everything, so a
    // newer exclusive action can never start (and bump generationRef) while an
    // older request is still queued or in flight - the refused reset above
    // never even issued a request. isStaleSnapshot is defense in depth for
    // that unreachable case (and is covered directly by the pure tests in
    // store.test.ts).
  });

  it("a timeline scrub during active playback pauses first and its seek waits for the in-flight tick", async () => {
    const actionsRef = renderHarness();
    await selectAndStart(actionsRef);

    vi.useFakeTimers();
    act(() => {
      actionsRef.current!.play();
    });

    const releaseAdvance = fake.hold(ADVANCE);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TICK_MS);
    });
    expect(callsTo(ADVANCE)).toBe(1);

    // Real timers (not fake ones) drive `waitFor`'s polling, so from here on
    // synchronize on the promises returned by the actions themselves.
    let seekPromise: Promise<void> | undefined;
    act(() => {
      actionsRef.current!.pause();
      seekPromise = actionsRef.current!.seek(1.0);
    });
    expect(screen.getByTestId("playing")).toHaveTextContent("false");
    expect(callsTo(SEEK)).toBe(0);

    releaseAdvance();
    await act(async () => {
      await seekPromise;
    });
    expect(screen.getByTestId("time")).toHaveTextContent("1.00");

    const advancesAfterSeek = callsTo(ADVANCE);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TICK_MS * 3);
    });
    expect(callsTo(ADVANCE)).toBe(advancesAfterSeek);
  });

  it("run replacement while a request is in flight is refused, then succeeds and old run data never lands on the new run", async () => {
    const actionsRef = renderHarness();
    await selectAndStart(actionsRef);

    const releaseStep = fake.hold(STEP);
    act(() => {
      void actionsRef.current!.step();
    });
    await waitFor(() => expect(callsTo(STEP)).toBe(1));

    let refused: Promise<void> | undefined;
    act(() => {
      refused = actionsRef.current!.startRun();
    });
    await refused;
    expect(callsTo(CREATE_RUN)).toBe(1);

    releaseStep();
    await waitFor(() => expect(screen.getByTestId("busy")).toHaveTextContent("false"));

    act(() => {
      void actionsRef.current!.startRun();
    });
    await waitFor(() => expect(callsTo(CREATE_RUN)).toBe(2));
    await waitFor(() => expect(screen.getByTestId("run-id")).toHaveTextContent("run-0002"));
    expect(screen.getByTestId("playing")).toHaveTextContent("false");
  });

  // Also exercises "playback interval follows the active run identity": once
  // the replacement settles, play() binds to run-0002 and every subsequent
  // tick targets it, never the discarded run-0001.
  it("play is refused while a replacement run is pending and never advances the old run", async () => {
    const actionsRef = renderHarness();
    await selectAndStart(actionsRef);

    await act(async () => {
      await actionsRef.current!.step();
    });
    expect(screen.getByTestId("time")).toHaveTextContent("0.25");

    const releaseCreate = fake.hold(CREATE_RUN);
    let startPromise: Promise<void> | undefined;
    act(() => {
      startPromise = actionsRef.current!.startRun();
    });
    expect(screen.getByTestId("busy")).toHaveTextContent("true");
    expect(screen.getByTestId("run-id")).toHaveTextContent("run-0001");

    act(() => {
      actionsRef.current!.play();
    });
    expect(screen.getByTestId("playing")).toHaveTextContent("false");

    vi.useFakeTimers();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TICK_MS * 3);
    });
    expect(fake.calls).not.toContain(ADVANCE);

    releaseCreate();
    await act(async () => {
      await startPromise;
    });
    expect(screen.getByTestId("run-id")).toHaveTextContent("run-0002");
    expect(screen.getByTestId("time")).toHaveTextContent("0.00");

    act(() => {
      actionsRef.current!.play();
    });
    expect(screen.getByTestId("playing")).toHaveTextContent("true");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(TICK_MS * 2);
    });
    const run2Advances = fake.calls.filter((call) => call === "POST /api/runs/run-0002/advance");
    expect(run2Advances.length).toBeGreaterThan(0);
    expect(fake.calls).not.toContain(ADVANCE);

    act(() => {
      actionsRef.current!.pause();
    });
  });

  it("replacing a run while playback is active stops the old loop and the new run starts at t=0", async () => {
    const actionsRef = renderHarness();
    await selectAndStart(actionsRef);

    vi.useFakeTimers();
    act(() => {
      actionsRef.current!.play();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TICK_MS * 2);
    });
    expect(callsTo(ADVANCE)).toBeGreaterThan(0);

    const releaseCreate = fake.hold(CREATE_RUN);
    let startPromise: Promise<void> | undefined;
    act(() => {
      startPromise = actionsRef.current!.startRun();
    });
    expect(screen.getByTestId("playing")).toHaveTextContent("false");

    const advanceCountAtReplacement = callsTo(ADVANCE);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(TICK_MS * 3);
    });
    expect(callsTo(ADVANCE)).toBe(advanceCountAtReplacement);

    releaseCreate();
    await act(async () => {
      await startPromise;
    });
    expect(screen.getByTestId("run-id")).toHaveTextContent("run-0002");
    expect(screen.getByTestId("time")).toHaveTextContent("0.00");
    expect(callsTo(ADVANCE)).toBe(advanceCountAtReplacement);
    expect(screen.getByTestId("playing")).toHaveTextContent("false");
  });

  it("Play button is disabled while a replacement run is pending (busy) and re-enables once it settles", async () => {
    const actionsRef = renderHarnessWithControls();
    await selectAndStart(actionsRef);

    expect(screen.getByRole("button", { name: "Play" })).not.toBeDisabled();

    const releaseCreate = fake.hold(CREATE_RUN);
    act(() => {
      void actionsRef.current!.startRun();
    });
    await waitFor(() => expect(screen.getByTestId("busy")).toHaveTextContent("true"));
    expect(screen.getByRole("button", { name: "Play" })).toBeDisabled();

    releaseCreate();
    await waitFor(() => expect(screen.getByTestId("run-id")).toHaveTextContent("run-0002"));
    expect(screen.getByRole("button", { name: "Play" })).not.toBeDisabled();
  });

  it("Pause button stays enabled while a tick is in flight during playback", async () => {
    const actionsRef = renderHarnessWithControls();
    await selectAndStart(actionsRef);

    const releaseAdvance = fake.hold(ADVANCE);
    act(() => {
      actionsRef.current!.play();
    });
    await waitFor(() => expect(callsTo(ADVANCE)).toBe(1));
    expect(screen.getByTestId("busy")).toHaveTextContent("true");
    expect(screen.getByRole("button", { name: "Pause" })).not.toBeDisabled();

    releaseAdvance();
    await waitFor(() => expect(screen.getByTestId("busy")).toHaveTextContent("false"));
    act(() => {
      actionsRef.current!.pause();
    });
  });
});
