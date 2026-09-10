/**
 * Tests for subscription expiry auto-logout.
 *
 * Covers:
 *  1. Periodic getMe() dispatch fires at 5-min interval for premium users.
 *  2. Auto-logout when premium → non-premium transition with active assignment.
 *  3. Auto-logout when entering grace period (Limit Grace = free instance).
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

jest.mock("react-redux", () => ({
  useSelector: (selector: (s: unknown) => unknown) =>
    selector({
      user: { currentUser: mockUser, logoutGeneration: 0 },
      pipeline: { run: { status: "StartUninitialized" } },
    }),
  useDispatch: () => mockDispatchFn,
}))

const mockGetMeAction = { type: "user/getMe" }
jest.mock("store/slice/User/UserActions", () => ({
  __esModule: true,
  getMe: () => mockGetMeAction,
}))

const mockAuthLogout = jest.fn()
jest.mock("utils/auth/AuthUtils", () => ({
  __esModule: true,
  logout: mockAuthLogout,
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

const mockBroadcastLogout = jest.fn()
const mockBroadcastPremiumReleased = jest.fn()
const mockTabSyncHandlers: Map<
  TabSyncMessageType,
  Set<(msg: TabSyncMessage) => void>
> = new Map()

jest.mock("utils/crossTabSync", () => ({
  __esModule: true,
  tabSync: {
    broadcast: () => {},
    broadcastLogout: mockBroadcastLogout,
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

const tree = (ctxRef: { current: Ctx | null }) => (
  <SnackbarProvider maxSnack={3}>
    <PremiumAssignmentProvider>
      <Harness ctxRef={ctxRef} />
    </PremiumAssignmentProvider>
  </SnackbarProvider>
)

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

// --- Tests ---

describe("PremiumAssignmentProvider — subscription expiry auto-logout", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    jest.useFakeTimers()
    mockTabSyncHandlers.clear()
    localStorage.clear()
    sessionStorage.clear()
    // Reset mock user to premium
    mockUser.subscription_plan_name = "Premium"
    mockUser.subscription_status = "Premium"
  })

  afterEach(() => {
    jest.clearAllTimers()
    jest.useRealTimers()
  })

  test("dispatches getMe() every 5 minutes for premium users", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    const ctxRef: { current: Ctx | null } = { current: null }
    render(tree(ctxRef))

    // Wait for initial assignment to settle
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Clear dispatch calls from initialization
    mockDispatchFn.mockClear()

    // Advance 4 minutes — should NOT have dispatched getMe yet
    await act(async () => {
      jest.advanceTimersByTime(4 * 60 * 1000)
      await Promise.resolve()
    })
    expect(mockDispatchFn).not.toHaveBeenCalledWith(mockGetMeAction)

    // Advance 1 more minute (total 5) — should dispatch getMe
    await act(async () => {
      jest.advanceTimersByTime(1 * 60 * 1000)
      await Promise.resolve()
    })
    expect(mockDispatchFn).toHaveBeenCalledWith(mockGetMeAction)
  })

  test("auto-logout when subscription expires with active assignment", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    const ctxRef: { current: Ctx | null } = { current: null }
    const { rerender } = render(tree(ctxRef))

    // Wait for initial assignment
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Simulate subscription expiry: change status to "Expired"
    mockUser.subscription_status = "Expired"

    // Force re-render to pick up the changed mock user
    await act(async () => {
      rerender(tree(ctxRef))
      await Promise.resolve()
    })

    // authLogout should have been called
    expect(mockAuthLogout).toHaveBeenCalledTimes(1)
    // broadcastLogout should have been called
    expect(mockBroadcastLogout).toHaveBeenCalledTimes(1)
    // Assignment should be cleared (by autoReleaseOnLogout)
    expect(ctxRef.current?.assignmentResult).toBeNull()
  })

  test("auto-logout when entering grace period (Limit Grace)", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    const ctxRef: { current: Ctx | null } = { current: null }
    const { rerender } = render(tree(ctxRef))

    // Wait for initial assignment
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Transition to grace period — no longer considered premium
    mockUser.subscription_status = "Limit Grace"

    await act(async () => {
      rerender(tree(ctxRef))
      await Promise.resolve()
    })

    // Should trigger auto-logout (grace period = free instance, not premium)
    expect(mockAuthLogout).toHaveBeenCalledTimes(1)
    expect(mockBroadcastLogout).toHaveBeenCalledTimes(1)
    // Assignment should be cleared
    expect(ctxRef.current?.assignmentResult).toBeNull()
  })

  test("auto-logout skips the free-logout endpoint (premium release path)", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    const ctxRef: { current: Ctx | null } = { current: null }
    const { rerender } = render(tree(ctxRef))

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    mockUser.subscription_status = "Expired"
    await act(async () => {
      rerender(tree(ctxRef))
      await Promise.resolve()
    })

    // authLogout must be told to skip the backend free-logout call,
    // otherwise both release-beacon AND /free/logout fire.
    expect(mockAuthLogout).toHaveBeenCalledWith({ skipBackendLogout: true })
  })

  test("does NOT auto-logout when there is no active assignment", async () => {
    // Status with no assignment → goes through the assign path.
    mockGetPremiumStatus.mockResolvedValue({
      subscription_type: UserTier.PREMIUM,
      is_premium: true,
    } as PremiumStatusResult)
    // Assignment fails (non-retryable) so assignmentResult stays null.
    mockAssignPremiumInstance.mockResolvedValue({
      assigned: false,
    } as PremiumAssignmentResult)

    const ctxRef: { current: Ctx | null } = { current: null }
    const { rerender } = render(tree(ctxRef))

    await act(async () => {
      await Promise.resolve()
    })
    expect(ctxRef.current?.assignmentResult).toBeNull()

    // Subscription expires, but with no assignment the auto-logout guard
    // (state.assignmentResult) must prevent the forced logout.
    mockUser.subscription_status = "Expired"
    await act(async () => {
      rerender(tree(ctxRef))
      await Promise.resolve()
    })

    expect(mockAuthLogout).not.toHaveBeenCalled()
    expect(mockBroadcastLogout).not.toHaveBeenCalled()
  })

  test("does NOT poll getMe() on non-leader tabs", async () => {
    // Override leader election so this tab is a follower.
    const crossTab = jest.requireMock("utils/crossTabSync") as {
      CrossTabLeaderElection: new (
        onLeader: () => void,
        onFollower?: () => void,
      ) => { getIsLeader(): boolean; destroy(): void }
    }
    const realImpl = crossTab.CrossTabLeaderElection
    crossTab.CrossTabLeaderElection = class {
      constructor() {
        // never becomes leader
      }
      getIsLeader() {
        return false
      }
      destroy() {}
    } as typeof realImpl

    try {
      mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)
      const ctxRef: { current: Ctx | null } = { current: null }
      render(tree(ctxRef))

      await act(async () => {
        await Promise.resolve()
      })
      mockDispatchFn.mockClear()

      // Advance well past the 5-min interval — no getMe() on a follower tab.
      await act(async () => {
        jest.advanceTimersByTime(11 * 60 * 1000)
        await Promise.resolve()
      })
      expect(mockDispatchFn).not.toHaveBeenCalledWith(mockGetMeAction)
    } finally {
      crossTab.CrossTabLeaderElection = realImpl
    }
  })
})
