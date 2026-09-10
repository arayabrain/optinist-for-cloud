/**
 * Tests for the shared-assignment polling stall fix.
 *
 * A shared assignment must keep polling /status until the backend upgrades it
 * to dedicated. The poll effect re-schedules only when a dependency changes;
 * once pollInterval saturates at MAX_POLL_INTERVAL_MS a stable shared
 * assignment left every other dependency unchanged and the loop stalled after
 * ~3 polls. Incrementing pollAttempts for shared too keeps a dependency moving
 * so the loop stays alive.
 *
 * Covers:
 *  1. Shared status keeps being polled past the pollInterval cap (no stall).
 *  2. A delayed shared→dedicated upgrade is still finalized and clears
 *     premium_shared.
 *  3. The dedicated MAX_POLL_ATTEMPTS stop is unchanged.
 *  4. A shared-origin user whose /status goes null keeps polling and never
 *     stops (the MAX_POLL_ATTEMPTS stop stays excluded for shared).
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

const mockTabSyncHandlers: Map<
  TabSyncMessageType,
  Set<(msg: TabSyncMessage) => void>
> = new Map()

jest.mock("utils/crossTabSync", () => ({
  __esModule: true,
  tabSync: {
    broadcast: () => {},
    broadcastLogout: () => {},
    broadcastPremiumReleased: () => {},
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
const { routingService } =
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  require("utils/routing/RoutingService")

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

/** Status endpoint returning a stable shared assignment. */
const sharedStatus: PremiumStatusResult = {
  subscription_type: UserTier.PREMIUM,
  is_premium: true,
  assignment: {
    instance_id: "shared-1",
    is_shared: true,
    assigned_at: "2026-06-22T00:00:00Z",
    status: "active",
  },
}

/** Status endpoint returning a dedicated assignment (the upgrade). */
const dedicatedStatus: PremiumStatusResult = {
  subscription_type: UserTier.PREMIUM,
  is_premium: true,
  assignment: {
    instance_id: "inst-A",
    is_shared: false,
    assigned_at: "2026-06-22T00:00:00Z",
    status: "active",
  },
}

/** Status endpoint returning null assignment (no assignment exists). */
const nullAssignmentStatus: PremiumStatusResult = {
  subscription_type: UserTier.PREMIUM,
  is_premium: true,
  assignment: null,
}

/** A retryable assign response (assigned: false, scaling_in_progress: true). */
const retryableAssignment: PremiumAssignmentResult = {
  message: "scaling in progress",
  instance_id: "",
  assigned: false,
  is_shared: false,
  assignment_source: "pending",
  scaling_in_progress: true,
  retry_after: 30,
}

/**
 * Advance timers and flush microtasks to simulate one polling cycle.
 * Each call advances 60s (≥ any backoff interval, incl. the saturated cap).
 */
const advanceOnePollCycle = async () => {
  await act(async () => {
    jest.advanceTimersByTime(60_000)
    await Promise.resolve()
  })
}

// --- Tests ---

describe("PremiumAssignmentProvider — shared polling does not stall at the cap", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    jest.useFakeTimers()
    mockTabSyncHandlers.clear()
    localStorage.clear()
    sessionStorage.clear()
    routingService.clearRoutingInfo()
    routingService.setPremiumAssigned(false)
  })

  afterEach(() => {
    jest.clearAllTimers()
    jest.useRealTimers()
  })

  test("keeps polling /status past the pollInterval cap for a stable shared assignment", async () => {
    // Every /status returns the same shared assignment — pollInterval saturates
    // (30000 → 45000 → 60000 → 60000…) after ~3 polls. Pre-fix, once the
    // interval stopped changing no dependency moved and polling stalled at 3.
    mockGetPremiumStatus.mockResolvedValue(sharedStatus)

    const ctxRef = renderProvider()

    // autoAssignOnLogin consumes the first /status and records the shared state.
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.is_shared).toBe(true)
    })
    expect(mockGetPremiumStatus).toHaveBeenCalledTimes(1)

    // Advance well past the point where pollInterval saturates. Pre-fix this
    // would stall at 4 total /status calls (1 mount + 3 polls); with the fix
    // the loop keeps re-scheduling.
    for (let i = 0; i < 6; i++) {
      await advanceOnePollCycle()
    }

    // Still on shared, and /status kept being polled beyond the ~3-poll stall.
    expect(ctxRef.current?.assignmentResult?.is_shared).toBe(true)
    expect(mockGetPremiumStatus.mock.calls.length).toBeGreaterThan(4)
  })

  test("finalizes a delayed shared→dedicated upgrade and clears premium_shared", async () => {
    // Start on a stable shared assignment.
    mockGetPremiumStatus.mockResolvedValue(sharedStatus)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.is_shared).toBe(true)
    })
    expect(routingService.isPremiumShared()).toBe(true)

    // Poll past the interval cap while still shared — loop must survive.
    for (let i = 0; i < 5; i++) {
      await advanceOnePollCycle()
    }
    expect(ctxRef.current?.assignmentResult?.is_shared).toBe(true)

    // Backend completes the migration: /status now returns dedicated.
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)
    await advanceOnePollCycle()

    // The dedicated assignment is finalized without a browser reload.
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
      expect(ctxRef.current?.assignmentResult?.is_shared).toBe(false)
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })
    expect(routingService.isPremiumShared()).toBe(false)
    expect(routingService.isPremiumAssigned()).toBe(true)

    // Polling stops now that the assignment is dedicated and healthy.
    mockGetPremiumStatus.mockClear()
    await advanceOnePollCycle()
    await advanceOnePollCycle()
    expect(mockGetPremiumStatus).not.toHaveBeenCalled()
  })

  test("dedicated MAX_POLL_ATTEMPTS stop is unchanged", async () => {
    // A non-shared retry loop (null status + retryable assign) must still stop
    // at MAX_POLL_ATTEMPTS — the shared fix must not remove that stop.
    mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)
    mockAssignPremiumInstance.mockResolvedValue(retryableAssignment)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)
    })

    // Advance through all 40 poll cycles to reach MAX_POLL_ATTEMPTS.
    for (let i = 0; i < 40; i++) {
      await advanceOnePollCycle()
    }

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult).toBeNull()
      expect(ctxRef.current?.error).toContain(
        "No premium instance available after extended wait",
      )
    })
  })

  test("shared-origin user whose /status goes null keeps polling and never stops", async () => {
    // The MAX_POLL_ATTEMPTS stop is gated on the STORED assignment
    // (state.assignmentResult?.is_shared). The null-status branch only updates
    // statusResult and preserves assignmentResult, so a shared-origin user
    // stays shared even after /status returns null — isOnShared stays true and
    // the stop never fires, no matter how high the (now-incrementing) counter
    // climbs. Guards against a future change that clears assignmentResult on
    // null and thereby re-enables the stop for these users.
    mockGetPremiumStatus.mockResolvedValue(sharedStatus)
    // Re-trigger assign (fires in the null branch) stays retryable — never
    // finalizes a dedicated instance, so the assignment remains shared.
    mockAssignPremiumInstance.mockResolvedValue(retryableAssignment)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.is_shared).toBe(true)
    })

    // Assignment is lost on the backend: /status now returns null.
    mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)

    // Advance well past MAX_POLL_ATTEMPTS (40) — the stop must not fire.
    for (let i = 0; i < 45; i++) {
      await advanceOnePollCycle()
    }

    // Still shared (assignmentResult preserved), no error surfaced, and polling
    // kept running the whole time.
    expect(ctxRef.current?.assignmentResult?.is_shared).toBe(true)
    expect(ctxRef.current?.assignmentResult).not.toBeNull()
    expect(ctxRef.current?.error).toBeNull()
    expect(mockGetPremiumStatus.mock.calls.length).toBeGreaterThan(40)
  })
})
