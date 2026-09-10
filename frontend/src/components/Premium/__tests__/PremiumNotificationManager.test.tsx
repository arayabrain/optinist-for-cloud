/**
 * Tests for PremiumNotificationManager's snackbars.
 *
 * Covers:
 *  - The "preparing dedicated resource" waiting snackbar: its copy, its info /
 *    persist options, and its dismissal once the dedicated instance lands
 *  - Initial non-terminal snackbar (no Retry button)
 *  - Non-terminal → terminal transition swaps the snackbar (close + enqueue)
 *    and the new snackbar carries an actionable Retry button
 *  - Retry click closes the snackbar and invokes retryProbe
 *  - Dismiss logs the correct `reason` for each exit path
 */

import React from "react"

import { beforeEach, describe, expect, jest, test } from "@jest/globals"
import { act, fireEvent, render } from "@testing-library/react"

// --- Snackbar mock -----------------------------------------------------------

type SnackbarCall = {
  key: string | number
  message: string
  options?: {
    variant?: string
    autoHideDuration?: number
    persist?: boolean
    action?: (key: string | number) => React.ReactNode
  }
}

const mockEnqueueSnackbar = jest.fn() as unknown as jest.Mock<
  string | number,
  [string, Record<string, unknown>]
>
const mockCloseSnackbar = jest.fn()

let snackbarLog: SnackbarCall[] = []
let snackbarKeyCounter = 0

jest.mock("notistack", () => ({
  __esModule: true,
  useSnackbar: () => ({
    enqueueSnackbar: mockEnqueueSnackbar,
    closeSnackbar: mockCloseSnackbar,
  }),
  // eslint-disable-next-line react/prop-types
  SnackbarProvider: ({ children }: { children: React.ReactNode }) => (
    <>{children}</>
  ),
}))

// --- Context mock ------------------------------------------------------------

type CtxShape = {
  isPremiumUser: boolean
  assignmentResult: {
    instance_id?: string
    assigned?: boolean
    is_shared?: boolean
  } | null
  error: string | null
  isAssigning: boolean
  isRetryableError: boolean
  unreachable: {
    state: { instanceUnreachable: boolean; isUnreachableTerminal: boolean }
    retryProbe: () => void
  }
}

let mockCtxValue: CtxShape = {
  isPremiumUser: true,
  assignmentResult: null,
  error: null,
  isAssigning: false,
  isRetryableError: false,
  unreachable: {
    state: { instanceUnreachable: false, isUnreachableTerminal: false },
    retryProbe: jest.fn(),
  },
}

jest.mock("contexts/PremiumAssignmentContext", () => ({
  __esModule: true,
  usePremiumAssignment: () => mockCtxValue,
}))

const mockLogPremiumUiEvent = jest.fn<
  Promise<void>,
  [string, Record<string, unknown>?]
>()

jest.mock("api/premium/PremiumAssignmentApi", () => ({
  __esModule: true,
  logPremiumUiEvent: mockLogPremiumUiEvent,
}))

// --- SUT import (after mocks) ------------------------------------------------

const PremiumNotificationManager: React.FC =
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  require("components/Premium/PremiumNotificationManager").default

// --- Helpers -----------------------------------------------------------------

const setCtx = (patch: Partial<CtxShape>) => {
  mockCtxValue = {
    ...mockCtxValue,
    ...patch,
    unreachable: { ...mockCtxValue.unreachable, ...(patch.unreachable ?? {}) },
  }
}

const baseCtx = (): CtxShape => ({
  isPremiumUser: true,
  assignmentResult: {
    instance_id: "inst-A",
    assigned: true,
    is_shared: false,
  },
  error: null,
  isAssigning: false,
  isRetryableError: false,
  unreachable: {
    state: { instanceUnreachable: false, isUnreachableTerminal: false },
    retryProbe: jest.fn(),
  },
})

// --- Tests -------------------------------------------------------------------

describe("PremiumNotificationManager — unreachable snackbar", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    snackbarLog = []
    snackbarKeyCounter = 0
    mockCtxValue = baseCtx()
    // Pre-mark this instance_id as already notified so the success snackbar
    // doesn't fire — focus of this suite is the unreachable snackbar.
    try {
      sessionStorage.clear()
      sessionStorage.setItem(
        "premium_notified_instance_id",
        baseCtx().assignmentResult!.instance_id!,
      )
    } catch {
      /* sessionStorage unavailable */
    }

    mockEnqueueSnackbar.mockImplementation(
      (message: string, options?: Record<string, unknown>) => {
        snackbarKeyCounter += 1
        const key = snackbarKeyCounter
        snackbarLog.push({
          key,
          message,
          options: options as SnackbarCall["options"],
        })
        return key
      },
    )
  })

  test("non-terminal unreachable: enqueues a warning snackbar without Retry action", () => {
    setCtx({
      unreachable: {
        state: { instanceUnreachable: true, isUnreachableTerminal: false },
        retryProbe: jest.fn(),
      },
    })

    render(<PremiumNotificationManager />)

    const unreachableCall = snackbarLog.find((c) =>
      c.message.includes("temporarily unreachable"),
    )
    expect(unreachableCall).toBeDefined()
    expect(unreachableCall?.options?.variant).toBe("warning")
    expect(unreachableCall?.options?.action).toBeUndefined()
    expect(mockLogPremiumUiEvent).toHaveBeenCalledWith(
      "instance_unreachable_popup_shown",
      expect.objectContaining({ terminal: false, instance_id: "inst-A" }),
    )
  })

  test("non-terminal → terminal transition: closes the old snackbar and enqueues a new one with a Retry action", () => {
    const retry = jest.fn()
    setCtx({
      unreachable: {
        state: { instanceUnreachable: true, isUnreachableTerminal: false },
        retryProbe: retry,
      },
    })

    const { rerender } = render(<PremiumNotificationManager />)
    // First render should have queued one unreachable snackbar (non-terminal).
    expect(snackbarLog).toHaveLength(1)
    const firstKey = snackbarLog[0].key

    // Flip to terminal.
    mockCtxValue = {
      ...mockCtxValue,
      unreachable: {
        state: { instanceUnreachable: true, isUnreachableTerminal: true },
        retryProbe: retry,
      },
    }
    act(() => {
      rerender(<PremiumNotificationManager />)
    })

    // The old snackbar must have been closed before the new one was enqueued.
    expect(mockCloseSnackbar).toHaveBeenCalledWith(firstKey)
    // And a second snackbar enqueued — this one must carry an action fn.
    expect(snackbarLog).toHaveLength(2)
    const terminalCall = snackbarLog[1]
    expect(terminalCall.message).toMatch(/unresponsive/i)
    expect(typeof terminalCall.options?.action).toBe("function")

    // Render the action and click the Retry button.
    const actionEl = terminalCall.options!.action!(terminalCall.key)
    const { getByRole } = render(<>{actionEl}</>)
    fireEvent.click(getByRole("button", { name: /retry/i }))

    expect(mockCloseSnackbar).toHaveBeenCalledWith(terminalCall.key)
    expect(retry).toHaveBeenCalledTimes(1)
  })

  test("unreachable → reachable: dismisses with reason=reachable", () => {
    setCtx({
      unreachable: {
        state: { instanceUnreachable: true, isUnreachableTerminal: false },
        retryProbe: jest.fn(),
      },
    })

    const { rerender } = render(<PremiumNotificationManager />)
    expect(snackbarLog).toHaveLength(1)
    mockLogPremiumUiEvent.mockClear()

    mockCtxValue = {
      ...mockCtxValue,
      unreachable: {
        state: { instanceUnreachable: false, isUnreachableTerminal: false },
        retryProbe: jest.fn(),
      },
    }
    act(() => {
      rerender(<PremiumNotificationManager />)
    })

    expect(mockLogPremiumUiEvent).toHaveBeenCalledWith(
      "instance_unreachable_popup_dismissed",
      expect.objectContaining({
        reason: "reachable",
        instance_id: "inst-A",
      }),
    )
  })

  test("dedicated → shared while unreachable: dismisses with reason=not_dedicated", () => {
    setCtx({
      unreachable: {
        state: { instanceUnreachable: true, isUnreachableTerminal: false },
        retryProbe: jest.fn(),
      },
    })

    const { rerender } = render(<PremiumNotificationManager />)
    mockLogPremiumUiEvent.mockClear()

    mockCtxValue = {
      ...mockCtxValue,
      assignmentResult: {
        instance_id: "inst-A",
        assigned: true,
        is_shared: true, // flipped to shared
      },
    }
    act(() => {
      rerender(<PremiumNotificationManager />)
    })

    expect(mockLogPremiumUiEvent).toHaveBeenCalledWith(
      "instance_unreachable_popup_dismissed",
      expect.objectContaining({ reason: "not_dedicated" }),
    )
  })

  test("premium → non-premium while unreachable: dismisses with reason=not_premium", () => {
    setCtx({
      unreachable: {
        state: { instanceUnreachable: true, isUnreachableTerminal: false },
        retryProbe: jest.fn(),
      },
    })

    const { rerender } = render(<PremiumNotificationManager />)
    mockLogPremiumUiEvent.mockClear()

    mockCtxValue = { ...mockCtxValue, isPremiumUser: false }
    act(() => {
      rerender(<PremiumNotificationManager />)
    })

    expect(mockLogPremiumUiEvent).toHaveBeenCalledWith(
      "instance_unreachable_popup_dismissed",
      expect.objectContaining({ reason: "not_premium" }),
    )
  })
})

describe("PremiumNotificationManager — assignment success and waiting snackbars", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    snackbarLog = []
    snackbarKeyCounter = 0
    mockCtxValue = baseCtx()
    // Clear the per-session notification flag between tests.
    try {
      sessionStorage.clear()
    } catch {
      /* sessionStorage unavailable */
    }

    mockEnqueueSnackbar.mockImplementation(
      (message: string, options?: Record<string, unknown>) => {
        snackbarKeyCounter += 1
        const key = snackbarKeyCounter
        snackbarLog.push({
          key,
          message,
          options: options as SnackbarCall["options"],
        })
        return key
      },
    )
  })

  // Renders on shared, then transitions to dedicated — fires the success branch.
  const renderAndMigrate = () => {
    mockCtxValue = {
      ...baseCtx(),
      assignmentResult: {
        instance_id: "shared-pool",
        assigned: true,
        is_shared: true,
      },
    }
    const { rerender } = render(<PremiumNotificationManager />)

    mockCtxValue = {
      ...mockCtxValue,
      assignmentResult: {
        instance_id: "inst-A",
        assigned: true,
        is_shared: false,
      },
    }
    act(() => {
      rerender(<PremiumNotificationManager />)
    })
  }

  // Asserted verbatim rather than by substring: this is the only confirmation a
  // premium user gets that the dedicated instance is theirs.
  const SUCCESS_COPY =
    "Premium instance assigned successfully! " +
    "You now have dedicated compute resources."

  test("success snackbar persists and inherits the default close action", () => {
    renderAndMigrate()

    const successCall = snackbarLog.find((c) => c.message === SUCCESS_COPY)
    expect(successCall).toBeDefined()
    expect(successCall?.options?.variant).toBe("success")
    expect(successCall?.options?.persist).toBe(true)
    expect(successCall?.options?.autoHideDuration).toBeUndefined()
    expect(successCall?.options?.action).toBeUndefined()
    expect(mockLogPremiumUiEvent).toHaveBeenCalledWith(
      "dedicated_instance_ready",
      { instance_id: "inst-A" },
    )
  })

  // The once-per-session gate. A stale sessionStorage entry silently swallowing
  // the notification is a plausible cause of a "no popup appeared" report, so
  // both directions of the gate are pinned.
  test("the assigned instance id is recorded so a refresh does not re-notify", () => {
    renderAndMigrate()

    expect(sessionStorage.getItem("premium_notified_instance_id")).toBe(
      "inst-A",
    )
  })

  test("no success snackbar when this session already notified for this instance", () => {
    sessionStorage.setItem("premium_notified_instance_id", "inst-A")

    renderAndMigrate()

    expect(snackbarLog.filter((c) => c.message === SUCCESS_COPY)).toHaveLength(
      0,
    )
  })

  test("a different instance in sessionStorage does not suppress the snackbar", () => {
    sessionStorage.setItem("premium_notified_instance_id", "inst-OLD")

    renderAndMigrate()

    expect(snackbarLog.filter((c) => c.message === SUCCESS_COPY)).toHaveLength(
      1,
    )
  })

  // Asserted verbatim: this is the only signal a premium user gets while the
  // dedicated instance is still being prepared.
  const WAITING_COPY =
    "Please wait while your dedicated premium resource is being prepared."

  test("waiting snackbar shows the preparing copy while there is no dedicated instance yet", () => {
    mockCtxValue = { ...baseCtx(), assignmentResult: null, isAssigning: true }

    render(<PremiumNotificationManager />)

    const waitingCall = snackbarLog.find((c) => c.message === WAITING_COPY)
    expect(waitingCall).toBeDefined()
    expect(waitingCall?.options?.variant).toBe("info")
    // Persistent: the wait outlives any autoHide, and only the dedicated
    // handoff (or a hard error) may dismiss it.
    expect(waitingCall?.options?.persist).toBe(true)
    expect(mockLogPremiumUiEvent).toHaveBeenCalledWith(
      "waiting_popup_shown",
      expect.objectContaining({ is_assigning: true, has_assignment: false }),
    )
  })

  test("waiting snackbar also covers a shared-only assignment, and is dismissed once dedicated lands", () => {
    renderAndMigrate()

    const waitingCall = snackbarLog.find((c) => c.message === WAITING_COPY)
    expect(waitingCall).toBeDefined()
    expect(mockCloseSnackbar).toHaveBeenCalledWith(waitingCall!.key)
    expect(mockLogPremiumUiEvent).toHaveBeenCalledWith(
      "waiting_popup_dismissed",
      expect.objectContaining({
        reason: "dedicated_ready",
        instance_id: "inst-A",
      }),
    )
    // Exactly one waiting snackbar for the whole shared → dedicated handoff.
    expect(snackbarLog.filter((c) => c.message === WAITING_COPY)).toHaveLength(
      1,
    )
  })
})
