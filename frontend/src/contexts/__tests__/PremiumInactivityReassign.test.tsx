/**
 * Tests for the inactivity-release → reassign-on-activity flow (Issue #594).
 *
 * Covers:
 *  1. Clicks after 2h inactivity auto-release re-fire assignPremiumInstance.
 *  2. Cross-tab PREMIUM_RELEASED primes the receiving tab to reassign on click.
 *  3. Clicks while assignment is active do not trigger redundant re-assignment.
 *  4. Normal login does not produce duplicate /assign calls from DOM events.
 */

import React from "react"

import { SnackbarProvider } from "notistack"

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  jest,
  test,
} from "@jest/globals"
import { act, render, waitFor } from "@testing-library/react"

import type {
  PremiumAssignmentResult,
  PremiumReleaseResult,
  PremiumStatusResult,
  PremiumHeartbeatResult,
  RoutingInfo,
} from "api/premium/PremiumAssignmentApi"
import { UserTier } from "const/Subscription"
import type { TabSyncMessage, TabSyncMessageType } from "utils/crossTabSync"

// --- Module mocks (must precede provider import) ---

const mockUser = {
  id: 1,
  uid: "test-uid",
  subscription_plan_name: "Premium",
  subscription_status: "Premium",
}

const mockDispatchFn = jest.fn(() => Promise.resolve())
const mockLogoutFn = jest.fn()

jest.mock("react-redux", () => ({
  useSelector: (selector: (s: unknown) => unknown) =>
    selector({
      user: { currentUser: mockUser, logoutGeneration: 0 },
      pipeline: { run: { status: "StartUninitialized" } },
    }),
  useDispatch: () => mockDispatchFn,
}))

jest.mock("store/slice/User/UserActions", () => ({
  __esModule: true,
  getMe: () => ({ type: "user/getMe" }),
}))

jest.mock("utils/auth/AuthUtils", () => ({
  __esModule: true,
  logout: mockLogoutFn,
}))

const mockAssignPremiumInstance = jest.fn<
  Promise<PremiumAssignmentResult>,
  []
>()
const mockReleasePremiumInstance = jest.fn<Promise<PremiumReleaseResult>, []>()
const mockGetPremiumStatus = jest.fn<Promise<PremiumStatusResult>, []>()
const mockGetBeaconTokenApi = jest
  .fn<Promise<{ data: { token: string } }>, []>()
  .mockResolvedValue({ data: { token: "t" } })
const mockSendPremiumHeartbeat = jest
  .fn<Promise<PremiumHeartbeatResult>, []>()
  .mockResolvedValue({} as PremiumHeartbeatResult)
const mockGetRoutingInfo = jest
  .fn<Promise<RoutingInfo | null>, []>()
  .mockResolvedValue(null)
const mockLogPremiumUiEvent = jest.fn<
  Promise<void>,
  [string, Record<string, unknown>?]
>()

jest.mock("api/premium/PremiumAssignmentApi", () => ({
  __esModule: true,
  assignPremiumInstance: mockAssignPremiumInstance,
  releasePremiumInstance: mockReleasePremiumInstance,
  getPremiumStatus: mockGetPremiumStatus,
  getBeaconTokenApi: mockGetBeaconTokenApi,
  sendPremiumHeartbeat: mockSendPremiumHeartbeat,
  getRoutingInfo: mockGetRoutingInfo,
  logPremiumUiEvent: mockLogPremiumUiEvent,
}))

jest.mock("hooks/useSleepDetection", () => ({
  __esModule: true,
  useSleepDetection: () => undefined,
}))

const mockBroadcastPremiumReleased = jest.fn()
const mockTabSyncHandlers: Map<
  TabSyncMessageType,
  Set<(msg: TabSyncMessage) => void>
> = new Map()

jest.mock("utils/crossTabSync", () => ({
  __esModule: true,
  tabSync: {
    broadcast: () => {},
    broadcastLogout: () => {},
    broadcastPremiumReleased: mockBroadcastPremiumReleased,
    on: (type: TabSyncMessageType, handler: (m: TabSyncMessage) => void) => {
      if (!mockTabSyncHandlers.has(type))
        mockTabSyncHandlers.set(type, new Set())
      mockTabSyncHandlers.get(type)!.add(handler)
      return () => mockTabSyncHandlers.get(type)?.delete(handler)
    },
    onAny: () => () => {},
    destroy: () => {},
  },
  syncActivityAcrossTabs: () => {},
  getLastActivityFromAnyTab: () => 0,
  onActivityFromOtherTab: () => () => {},
  CrossTabLeaderElection: class {
    constructor(onBecomeLeader: () => void) {
      setTimeout(onBecomeLeader, 0)
    }
    getIsLeader() {
      return true
    }
    destroy() {}
  },
}))

// require() not import — static imports hoist above the mock vars.
const { PremiumAssignmentProvider, usePremiumAssignment } =
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  require("contexts/PremiumAssignmentContext")

// --- Helpers ---

type Ctx = ReturnType<typeof usePremiumAssignment>

const Harness: React.FC<{ ctxRef: { current: Ctx | null } }> = ({ ctxRef }) => {
  ctxRef.current = usePremiumAssignment()
  return null
}

const renderProvider = () => {
  const ctxRef: { current: Ctx | null } = { current: null }
  render(
    <SnackbarProvider maxSnack={3}>
      <PremiumAssignmentProvider>
        <Harness ctxRef={ctxRef} />
      </PremiumAssignmentProvider>
    </SnackbarProvider>,
  )
  return ctxRef
}

const dedicatedAssignment: PremiumAssignmentResult = {
  message: "dedicated",
  instance_id: "inst-A",
  assigned: true,
  is_shared: false,
  assignment_source: "existing",
}

const dedicatedStatus: PremiumStatusResult = {
  subscription_type: UserTier.PREMIUM,
  is_premium: true,
  assignment: {
    instance_id: "inst-A",
    is_shared: false,
    assigned_at: "2026-05-12T00:00:00Z",
    status: "active",
  },
}

/** Simulate a pointerdown event on the window (as a user click). */
const simulateClick = () => {
  window.dispatchEvent(new Event("pointerdown"))
}

/** Dispatch the PREMIUM_RELEASED tab-sync message to all registered handlers. */
const firePremiumReleased = () => {
  const handlers = mockTabSyncHandlers.get("PREMIUM_RELEASED")
  if (handlers) {
    handlers.forEach((h) => h({ type: "PREMIUM_RELEASED" }))
  }
}

// --- Tests ---

describe("PremiumAssignmentProvider — inactivity re-assignment", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    jest.useFakeTimers()
    mockTabSyncHandlers.clear()
    localStorage.clear()
    sessionStorage.clear()
  })

  afterEach(() => {
    jest.clearAllTimers()
    jest.useRealTimers()
  })

  test("clicks after 2h inactivity auto-release re-fire assignPremiumInstance", async () => {
    // autoAssignOnLogin first checks /status, sees existing assignment.
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    const ctxRef = renderProvider()

    // Wait for initial assignment to be adopted.
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Fast-forward 2h to trigger auto-release.
    await act(async () => {
      jest.advanceTimersByTime(2 * 60 * 60 * 1000 + 5000)
      await Promise.resolve()
    })

    // Assignment should be cleared.
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult).toBeNull()
    })

    // broadcastPremiumReleased should have been called.
    expect(mockBroadcastPremiumReleased).toHaveBeenCalled()

    // sessionStorage hasAttempted should be cleared.
    expect(sessionStorage.getItem("premium_hasAttempted")).toBeNull()

    // Now set up mocks for the reassignment path.
    mockGetPremiumStatus.mockClear()
    mockAssignPremiumInstance.mockClear()
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    // Simulate user activity (click) — should trigger reassignment.
    act(() => {
      simulateClick()
    })

    // autoAssignOnLogin should re-fire via the autoAssignGeneration bump.
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Verify /status was called (autoAssignOnLogin checks status first).
    expect(mockGetPremiumStatus).toHaveBeenCalled()
  })

  test("cross-tab PREMIUM_RELEASED primes the receiving tab to reassign on click", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Simulate receiving a PREMIUM_RELEASED broadcast from another tab.
    act(() => {
      firePremiumReleased()
    })

    // Assignment should be cleared by the handler.
    expect(ctxRef.current?.assignmentResult).toBeNull()
    expect(sessionStorage.getItem("premium_hasAttempted")).toBeNull()

    // Set up mocks for reassignment.
    mockGetPremiumStatus.mockClear()
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    // Click should trigger reassignment.
    act(() => {
      simulateClick()
    })

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    expect(mockGetPremiumStatus).toHaveBeenCalled()
  })

  test("clicks while assignment is active do not trigger redundant re-assignment", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Record how many times /status has been called so far (initial assign).
    const callsAfterMount = mockGetPremiumStatus.mock.calls.length

    // Simulate several clicks during an active session.
    act(() => {
      simulateClick()
      simulateClick()
      simulateClick()
    })

    // Flush any pending microtasks.
    await act(async () => {
      await Promise.resolve()
    })

    // No additional /status or /assign calls should have been made.
    expect(mockGetPremiumStatus.mock.calls.length).toBe(callsAfterMount)
    expect(mockAssignPremiumInstance).not.toHaveBeenCalled()
  })

  test("normal login does not produce duplicate /assign calls from DOM events", async () => {
    // This test guards against the PR #603 regression: DOM listeners must
    // NOT bump autoAssignGeneration during initial mount.
    mockGetPremiumStatus.mockResolvedValue({
      subscription_type: UserTier.PREMIUM,
      is_premium: true,
      assignment: null,
    })
    mockAssignPremiumInstance.mockResolvedValue(dedicatedAssignment)

    renderProvider()

    // Simulate rapid clicks during the initial assignment flow.
    act(() => {
      simulateClick()
      simulateClick()
      simulateClick()
    })

    // Wait for the assignment to complete.
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })

    // assignPremiumInstance should be called exactly once (from autoAssignOnLogin).
    // The DOM clicks must NOT have caused additional /assign calls.
    expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)
  })

  test("auto-release clears the routing token from localStorage (issue #605)", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Pre-seed routing token (the mocked APIs don't go through the real
    // axios interceptor, so we simulate the token being set as it would
    // be in production after the first premium-routed response).
    localStorage.setItem("routing_id", "d0250e94fae8595c")

    // Fast-forward 2h to trigger auto-release.
    await act(async () => {
      jest.advanceTimersByTime(2 * 60 * 60 * 1000 + 5000)
      await Promise.resolve()
    })

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult).toBeNull()
    })

    // Routing token must be cleared on release to prevent stale reuse.
    expect(localStorage.getItem("routing_id")).toBeNull()
  })

  test("cross-tab PREMIUM_RELEASED clears routing token from localStorage (issue #605)", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Pre-seed routing state as it would be in production after assignment.
    localStorage.setItem("routing_id", "d0250e94fae8595c")
    localStorage.setItem("premium_assigned", "true")
    localStorage.setItem("premium_instance_id", "inst-hash-A")

    // Simulate receiving a PREMIUM_RELEASED broadcast from another tab.
    act(() => {
      firePremiumReleased()
    })

    expect(ctxRef.current?.assignmentResult).toBeNull()
    // resetForRelease() must clear all three: token, assigned flag, and instance ID.
    // Without this, the receiving tab enters (premiumAssigned=true, token=null)
    // — an unrecoverable deadlock where the interceptor guard blocks re-seeding.
    expect(localStorage.getItem("routing_id")).toBeNull()
    expect(localStorage.getItem("premium_assigned")).toBe("false")
    expect(localStorage.getItem("premium_instance_id")).toBeNull()
  })
})
