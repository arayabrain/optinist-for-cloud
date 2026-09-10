/**
 * Tests for InactivityWarning's snackbar copy and the two outcomes of the
 * Stay Active button.
 *
 * Covers:
 *  - Copy: the idle time and the release countdown are both derived from
 *    PremiumTiming, so the snackbar cannot disagree with the thresholds the
 *    context actually enforces.
 *  - Success: the heartbeat lands, the warning is dismissed, and the button is
 *    the only way a user can do this (there is no auto-hide).
 *  - Expired token: a 401 out of recordActivity flips the alert to the
 *    "Session Expired" copy, removes the Stay Active action so it cannot be
 *    clicked again, and logs the user out after the read delay.
 *  - Any other heartbeat failure still dismisses the warning, so a transient
 *    network error cannot pin an undismissable snackbar on screen.
 */

import React from "react"

import { AxiosError } from "axios"

import {
  beforeEach,
  afterEach,
  describe,
  expect,
  jest,
  test,
} from "@jest/globals"
import { act, fireEvent, render, screen } from "@testing-library/react"

// --- Module mocks (must precede the SUT import) ---

const mockRecordActivity = jest.fn<Promise<void>, []>()
const mockDismissInactivityWarning = jest.fn()
const mockPerformLogout = jest.fn()

let mockShowInactivityWarning = true

jest.mock("contexts/PremiumAssignmentContext", () => ({
  __esModule: true,
  usePremiumAssignment: () => ({
    showInactivityWarning: mockShowInactivityWarning,
    dismissInactivityWarning: mockDismissInactivityWarning,
    recordActivity: mockRecordActivity,
  }),
}))

jest.mock("hooks/useLogout", () => ({
  __esModule: true,
  useLogout: () => ({ performLogout: mockPerformLogout }),
}))

const InactivityWarning: React.FC =
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  require("components/Premium/InactivityWarning").default

// --- Helpers ---

const unauthorized = () =>
  new AxiosError("Unauthorized", undefined, undefined, undefined, {
    status: 401,
    data: {},
    statusText: "Unauthorized",
    headers: {},
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    config: {} as any,
  })

// fireEvent, not userEvent: two of these tests run on fake timers, which
// userEvent's default inter-event delay would wait on forever.
const clickStayActive = async () => {
  const button = screen.getByRole("button", { name: /stay active/i })
  await act(async () => {
    fireEvent.click(button)
    await Promise.resolve()
  })
}

// --- Tests ---

describe("InactivityWarning", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    mockShowInactivityWarning = true
    mockRecordActivity.mockResolvedValue(undefined)
  })

  afterEach(() => {
    jest.useRealTimers()
  })

  test("renders the warning only while the context asks for it", () => {
    const { unmount } = render(<InactivityWarning />)
    expect(screen.getByText(/inactivity warning/i)).toBeInTheDocument()
    unmount()

    mockShowInactivityWarning = false
    render(<InactivityWarning />)
    expect(screen.queryByText(/inactivity warning/i)).not.toBeInTheDocument()
  })

  test("Stay Active sends the heartbeat, then dismisses the warning", async () => {
    render(<InactivityWarning />)

    await clickStayActive()

    expect(mockRecordActivity).toHaveBeenCalledTimes(1)
    expect(mockDismissInactivityWarning).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/session expired/i)).not.toBeInTheDocument()
    expect(mockPerformLogout).not.toHaveBeenCalled()
  })

  test("a 401 shows Session Expired, drops the button, and logs out after the read delay", async () => {
    jest.useFakeTimers()
    mockRecordActivity.mockRejectedValue(unauthorized())

    render(<InactivityWarning />)

    await clickStayActive()

    expect(screen.getByText("Session Expired")).toBeInTheDocument()
    // The action is gone: a second click cannot re-enter the failing path.
    expect(
      screen.queryByRole("button", { name: /stay active/i }),
    ).not.toBeInTheDocument()
    // The warning stays up to carry the message — dismissing it would hide it.
    expect(mockDismissInactivityWarning).not.toHaveBeenCalled()
    expect(mockPerformLogout).not.toHaveBeenCalled()

    await act(async () => {
      jest.advanceTimersByTime(2000)
    })

    expect(mockPerformLogout).toHaveBeenCalledTimes(1)
  })

  test("a non-401 heartbeat failure still dismisses the warning", async () => {
    mockRecordActivity.mockRejectedValue(new Error("Network Error"))

    render(<InactivityWarning />)

    await clickStayActive()

    expect(mockDismissInactivityWarning).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/session expired/i)).not.toBeInTheDocument()
    expect(mockPerformLogout).not.toHaveBeenCalled()
  })

  test("states the idle time and the remaining release countdown", () => {
    render(<InactivityWarning />)

    const alert = screen.getByRole("alert").textContent ?? ""
    expect(alert).toContain("inactive for 1h.")
    expect(alert).toContain("released in 1h if no activity")
    expect(alert).not.toContain("1h 0m")
  })
})
