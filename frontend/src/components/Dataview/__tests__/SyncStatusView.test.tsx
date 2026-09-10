/**
 * Sync status display and manual retry.
 *
 * `useSyncRetry` decides the state and `SyncStatusView` renders it, so they are
 * exercised together through the same wiring `BaseNodesView` uses: one is the
 * branch table, the other is what the user sees for each branch.
 */

import {
  describe,
  it,
  expect,
  jest,
  beforeEach,
  afterEach,
} from "@jest/globals"
import "@testing-library/jest-dom"
import { render, screen, act, fireEvent } from "@testing-library/react"

import { SyncStatusView } from "components/Dataview/SyncStatusView"
import { useSyncRetry } from "components/Dataview/useSyncRetry"

// Must match the constants in useSyncRetry.ts
const RETRY_MAX_COUNT = 30
const RETRY_INTERVAL = 10000

const httpError = (status: number, message?: string) => ({
  response: { status, data: message ? { message } : undefined },
})

// The wiring BaseNodesView and WorkflowDetailsView both use: the hook decides,
// the view renders. What the consumers put in the resolved slot is their own
// business, so this harness renders nothing there and the tests assert that the
// sync UI is gone rather than that some replacement appeared.
const Harness = ({ fetchFn }: { fetchFn: () => Promise<unknown> }) => {
  const { syncStatus, handleRetry } = useSyncRetry({
    is_public: true,
    fetchFn,
    shouldFetch: true,
  })
  return <SyncStatusView syncStatus={syncStatus} onRetry={handleRetry} />
}

// One rejected fetch settles in a microtask, so flushing promises is enough
const renderHarness = async (fetchFn: () => Promise<unknown>) => {
  await act(async () => {
    render(<Harness fetchFn={fetchFn} />)
  })
}

// MUI's error Alert renders an ErrorOutlineIcon of its own, so exclude the
// icons that live inside role="alert"
const standaloneIcons = (testId: string) =>
  screen
    .getAllByTestId(testId)
    .filter((icon) => icon.closest("[role=alert]") === null)

describe("Dataview sync status", () => {
  beforeEach(() => {
    jest.useFakeTimers()
  })

  afterEach(() => {
    jest.useRealTimers()
  })

  describe("pending branches", () => {
    it.each([202, 423])(
      "shows the auto-retry notice with no Retry button on %i",
      async (status) => {
        const fetchFn = jest
          .fn<Promise<unknown>, []>()
          .mockRejectedValue(httpError(status, "Sync in progress"))
        await renderHarness(fetchFn)

        expect(screen.getByText("Sync in progress")).toBeInTheDocument()
        expect(
          screen.getByText("This page will auto-retry."),
        ).toBeInTheDocument()
        expect(standaloneIcons("HourglassEmptyIcon")).toHaveLength(1)
        // A pending experiment retries itself; offering Retry would be noise
        expect(
          screen.queryByRole("button", { name: "Retry" }),
        ).not.toBeInTheDocument()
      },
    )

    it("re-fires fetchFn once per retry interval while pending", async () => {
      const fetchFn = jest
        .fn<Promise<unknown>, []>()
        .mockRejectedValue(httpError(202))
      await renderHarness(fetchFn)
      expect(fetchFn).toHaveBeenCalledTimes(1)

      await act(async () => {
        jest.advanceTimersByTime(RETRY_INTERVAL)
      })
      expect(fetchFn).toHaveBeenCalledTimes(2)

      // Not on a shorter tick: the bump is what re-runs the effect
      await act(async () => {
        jest.advanceTimersByTime(RETRY_INTERVAL - 1)
      })
      expect(fetchFn).toHaveBeenCalledTimes(2)
    })

    it("stops retrying and turns terminal at the retry ceiling", async () => {
      const fetchFn = jest
        .fn<Promise<unknown>, []>()
        .mockRejectedValue(httpError(202))
      await renderHarness(fetchFn)

      for (let i = 0; i < RETRY_MAX_COUNT; i++) {
        await act(async () => {
          jest.advanceTimersByTime(RETRY_INTERVAL)
        })
      }

      // The ceiling attempt itself still runs; the one after it is refused
      expect(fetchFn).toHaveBeenCalledTimes(RETRY_MAX_COUNT + 1)
      expect(
        screen.getByText(
          "Loading is taking longer than expected. Please try again later.",
        ),
      ).toBeInTheDocument()
      expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument()

      await act(async () => {
        jest.advanceTimersByTime(RETRY_INTERVAL * 5)
      })
      expect(fetchFn).toHaveBeenCalledTimes(RETRY_MAX_COUNT + 1)
    })
  })

  describe("error branches", () => {
    it.each([
      [
        httpError(503),
        "Experiment temporarily unavailable, please try again later.",
      ],
      [httpError(500), "Failed to load experiment (500). Please try again."],
      [
        new Error("Network Error"),
        "Failed to load experiment. Please check your connection and try again.",
      ],
    ])("renders icon, alert and Retry for %#", async (error, message) => {
      const fetchFn = jest.fn<Promise<unknown>, []>().mockRejectedValue(error)
      await renderHarness(fetchFn)

      expect(standaloneIcons("ErrorOutlineIcon")).toHaveLength(1)
      expect(screen.getByRole("alert")).toHaveTextContent(message)
      expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument()
    })

    it("prefers the server's own message over the fallback", async () => {
      const fetchFn = jest
        .fn<Promise<unknown>, []>()
        .mockRejectedValue(httpError(503, "S3 output experiment.yaml missing"))
      await renderHarness(fetchFn)

      expect(
        screen.getByText("S3 output experiment.yaml missing"),
      ).toBeInTheDocument()
    })
  })

  describe("manual retry", () => {
    it("re-attempts the fetch and clears the sync UI once it succeeds", async () => {
      const fetchFn = jest
        .fn<Promise<unknown>, []>()
        .mockRejectedValueOnce(httpError(503))
        .mockResolvedValue({})
      await renderHarness(fetchFn)
      expect(standaloneIcons("ErrorOutlineIcon")).toHaveLength(1)

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }))
      })

      expect(fetchFn).toHaveBeenCalledTimes(2)
      expect(screen.queryByRole("alert")).not.toBeInTheDocument()
      expect(
        screen.queryByRole("button", { name: "Retry" }),
      ).not.toBeInTheDocument()
    })

    it("keeps the error state when the retry fails again", async () => {
      const fetchFn = jest
        .fn<Promise<unknown>, []>()
        .mockRejectedValue(httpError(503))
      await renderHarness(fetchFn)

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Retry" }))
      })

      expect(fetchFn).toHaveBeenCalledTimes(2)
      expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument()
    })
  })
})
