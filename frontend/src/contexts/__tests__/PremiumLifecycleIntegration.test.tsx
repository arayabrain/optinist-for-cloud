/**
 * Lifecycle integration test for premium routing.
 *
 * Drives the real provider through one continuous session — login → assign →
 * 2h inactivity release → reassign on gesture → logout — and asserts the
 * routingService singleton at every boundary, rather than one transition in
 * isolation as the per-concern suites do.
 *
 * Covers:
 *  1. Routing state is cleared at release/logout and re-seeded at assign,
 *     across all four phases of a single run.
 *  2. The (premiumAssigned=true, token=null) deadlock is never observed.
 *  3. A shared assignment carries premium_shared for its whole life and drops
 *     it on release.
 *  4. The explicit release() action tears routing down as fully as the logout
 *     path — no stale token/instance/shared left behind. (release() is an
 *     unused public context method today; this locks its teardown contract.)
 *  5. A cross-tab PREMIUM_RELEASED broadcast received by this tab tears routing
 *     down the same way.
 *  6. Closing the tab beacons the release, and only while there is an
 *     assignment and a beacon token to release with.
 *  7. A re-login inside the release grace adopts the restored assignment rather
 *     than requesting a new one.
 *
 * Cross-tab receive is simulated by invoking the registered handler directly
 * (same-document localStorage writes emit no storage event), matching how the
 * per-concern suites do it. This test drives only the mocked API boundary; the
 * axios-path invariants (premiumShared teardown gate, staleness watermark,
 * warm-up grace suppression) are exercised by axiosPremiumInterceptor,
 * PremiumUnreachableIntegration and useInstanceUnreachableMachineLeader.
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
  PremiumStatusResult,
} from "api/premium/PremiumAssignmentApi"
import { RoutingHeaders, UserTier } from "const/Subscription"
import type { TabSyncMessage, TabSyncMessageType } from "utils/crossTabSync"

// --- Module mocks (must precede provider import) ---

const mockUser = {
  id: 1,
  uid: "test-uid",
  subscription_plan_name: "Premium",
  subscription_status: "Premium",
}

// Mutable so the logout phase can drop the user and bump logoutGeneration the
// way dispatch(logout()) does.
const mockReduxState = {
  user: {
    currentUser: mockUser as typeof mockUser | null,
    logoutGeneration: 0,
  },
  pipeline: { run: { status: "StartUninitialized" } },
}

const mockDispatchFn = jest.fn(() => Promise.resolve())
const mockLogoutFn = jest.fn()

jest.mock("react-redux", () => ({
  useSelector: (selector: (s: unknown) => unknown) => selector(mockReduxState),
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

jest.mock(
  "api/premium/PremiumAssignmentApi",
  () =>
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    require("contexts/testUtils/premiumApiMock").mockPremiumApi,
)

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
const { mockPremiumApi, installPremiumApiDefaults } =
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  require("contexts/testUtils/premiumApiMock")
const { routingService } =
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  require("utils/routing/RoutingService")

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

const TWO_HOURS_MS = 2 * 60 * 60 * 1000

const noAssignmentStatus: PremiumStatusResult = {
  subscription_type: UserTier.PREMIUM,
  is_premium: true,
  assignment: null,
}

const assignedA: PremiumAssignmentResult = {
  message: "assigned",
  instance_id: "inst-A",
  instance_id_hash: "hash-A",
  assigned: true,
  is_shared: false,
  assignment_source: "dedicated",
}

const assignedB: PremiumAssignmentResult = {
  ...assignedA,
  instance_id: "inst-B",
  instance_id_hash: "hash-B",
}

const sharedStatus: PremiumStatusResult = {
  subscription_type: UserTier.PREMIUM,
  is_premium: true,
  assignment: {
    instance_id: "shared-1",
    instance_id_hash: "hash-shared-1",
    assigned_at: "2026-07-30T00:00:00Z",
    status: "active",
    is_shared: true,
    assignment_source: "shared",
  },
}

/**
 * The unrecoverable pair: premium routing claimed with no token to route by.
 * Meaningful only after a token seed or a teardown — a fresh /assign
 * legitimately holds (assigned, null) until the first routed response lands.
 */
const expectNoRoutingDeadlock = () => {
  expect(
    routingService.isPremiumAssigned() &&
      routingService.getRoutingToken() === null,
  ).toBe(false)
}

/** Everything the three teardown paths must leave clear. */
const expectRoutingTornDown = () => {
  expect(routingService.isPremiumAssigned()).toBe(false)
  expect(routingService.getRoutingToken()).toBeNull()
  expect(routingService.getPremiumInstanceId()).toBeNull()
  expect(routingService.isPremiumShared()).toBe(false)
  expect(routingService.isWithinPremiumWarmup()).toBe(false)
  expect(routingService.getRoutingHeaders()).toEqual({})
  expectNoRoutingDeadlock()
}

// jsdom ships no sendBeacon, and the beacon token now resolves, so the release
// paths reach it for real.
const mockSendBeacon = jest.fn(() => true)
Object.defineProperty(navigator, "sendBeacon", {
  value: mockSendBeacon,
  writable: true,
  configurable: true,
})

const flushPromises = async () => {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

// --- Tests ---

describe("PremiumAssignmentProvider — full routing lifecycle", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    jest.useFakeTimers()
    installPremiumApiDefaults()
    mockTabSyncHandlers.clear()
    localStorage.clear()
    sessionStorage.clear()
    routingService.clearRoutingInfo()
    mockReduxState.user.currentUser = mockUser
    mockReduxState.user.logoutGeneration = 0
  })

  afterEach(() => {
    jest.clearAllTimers()
    jest.useRealTimers()
  })

  test("login → assign → inactivity release → reassign → logout keeps routing state consistent", async () => {
    mockPremiumApi.getPremiumStatus.mockResolvedValue(noAssignmentStatus)
    mockPremiumApi.assignPremiumInstance.mockResolvedValue(assignedA)

    const ctxRef: { current: Ctx | null } = { current: null }
    const { rerender } = render(tree(ctxRef))

    // --- Phase 1: login + first assignment ---
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })
    expect(routingService.isPremiumAssigned()).toBe(true)
    expect(routingService.getPremiumInstanceId()).toBe("hash-A")
    expect(routingService.isPremiumShared()).toBe(false)
    expect(routingService.isWithinPremiumWarmup()).toBe(true)

    // The mocked API never runs the axios interceptor, so seed the token the
    // way the first premium-routed response header would.
    routingService.updateRoutingToken("routing-A")
    expect(routingService.getRoutingHeaders()[RoutingHeaders.ROUTING_ID]).toBe(
      "routing-A",
    )
    expectNoRoutingDeadlock()

    // --- Phase 2: 2h idle → auto-release ---
    await act(async () => {
      jest.advanceTimersByTime(TWO_HOURS_MS + 5000)
      await Promise.resolve()
    })

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult).toBeNull()
    })
    expectRoutingTornDown()
    expect(mockBroadcastPremiumReleased).toHaveBeenCalled()
    expect(mockSendBeacon).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem("premium_hasAttempted")).toBeNull()

    // --- Phase 3: gesture → reassignment onto a new instance ---
    mockPremiumApi.assignPremiumInstance.mockResolvedValue(assignedB)

    act(() => {
      window.dispatchEvent(new Event("pointerdown"))
    })

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-B")
    })
    expect(routingService.isPremiumAssigned()).toBe(true)
    expect(routingService.getPremiumInstanceId()).toBe("hash-B")
    expect(routingService.isWithinPremiumWarmup()).toBe(true)

    routingService.updateRoutingToken("routing-B")
    expect(routingService.getRoutingHeaders()[RoutingHeaders.ROUTING_ID]).toBe(
      "routing-B",
    )
    expectNoRoutingDeadlock()

    // --- Phase 4: logout ---
    act(() => {
      ctxRef.current!.autoReleaseOnLogout()
    })
    expectRoutingTornDown()

    mockReduxState.user.currentUser = null
    mockReduxState.user.logoutGeneration = 1
    await act(async () => {
      rerender(tree(ctxRef))
      await Promise.resolve()
    })

    expect(ctxRef.current?.assignmentResult).toBeNull()
    expect(ctxRef.current?.statusResult).toBeNull()
    expect(ctxRef.current?.isPremiumUser).toBe(false)
    expectRoutingTornDown()
  })

  test("a shared assignment holds premium_shared until release clears it", async () => {
    mockPremiumApi.getPremiumStatus.mockResolvedValue(sharedStatus)

    const ctxRef: { current: Ctx | null } = { current: null }
    render(tree(ctxRef))

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.is_shared).toBe(true)
    })
    expect(routingService.isPremiumShared()).toBe(true)
    expect(routingService.isPremiumAssigned()).toBe(true)

    routingService.updateRoutingToken("routing-shared")
    expectNoRoutingDeadlock()

    // Shared assignments keep polling, so step the clock one poll interval at a
    // time and let each continuation settle before the next inactivity check.
    for (let i = 0; i < TWO_HOURS_MS / 60_000 + 1; i++) {
      await act(async () => {
        jest.advanceTimersByTime(60_000)
        await Promise.resolve()
      })
    }
    await flushPromises()
    expect(mockPremiumApi.getPremiumStatus.mock.calls.length).toBeGreaterThan(1)

    // The inactivity release has fired; stop the poll from resurrecting the
    // shared assignment so the terminal state is deterministic (a live shared
    // status would re-populate on the next poll and re-arm inactivity). Also
    // neutralize assign so a stray re-assign cannot resurrect it regardless of
    // the mock default.
    mockPremiumApi.getPremiumStatus.mockResolvedValue(noAssignmentStatus)
    mockPremiumApi.assignPremiumInstance.mockResolvedValue({
      message: "not assigned",
      instance_id: "",
      assigned: false,
    })
    await act(async () => {
      jest.advanceTimersByTime(60_000)
      await Promise.resolve()
    })
    await flushPromises()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult).toBeNull()
    })
    expectRoutingTornDown()
  })

  test("re-login inside the grace window adopts the restored row instead of assigning a new one", async () => {
    // Logout (and a tab close) soft-releases, and a re-login inside the grace
    // restores the same assignment server-side. The frontend half is adoption:
    // /status is read and /assign must never be called, since an /assign is
    // what would strand the restored row behind a second one.
    const restoredStatus: PremiumStatusResult = {
      subscription_type: UserTier.PREMIUM,
      is_premium: true,
      assignment: {
        instance_id: "inst-A",
        instance_id_hash: "hash-A",
        assigned_at: "2026-07-30T00:00:00Z",
        status: "active",
        is_shared: false,
        assignment_source: "existing",
      },
    }
    mockPremiumApi.getPremiumStatus.mockResolvedValue(restoredStatus)

    const ctxRef: { current: Ctx | null } = { current: null }
    const { rerender } = render(tree(ctxRef))

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })
    routingService.updateRoutingToken("routing-A")

    // --- Logout: soft-release beacon + the logout dispatch ---
    act(() => {
      ctxRef.current!.autoReleaseOnLogout()
    })
    expectRoutingTornDown()

    mockReduxState.user.currentUser = null
    mockReduxState.user.logoutGeneration = 1
    await act(async () => {
      rerender(tree(ctxRef))
      await Promise.resolve()
    })
    // The re-login can only re-adopt if the logout dropped this tab's
    // one-shot guard.
    expect(sessionStorage.getItem("premium_hasAttempted")).toBeNull()

    // --- Re-login within the grace window: the row is back to active ---
    mockPremiumApi.assignPremiumInstance.mockClear()
    mockReduxState.user.currentUser = mockUser
    await act(async () => {
      rerender(tree(ctxRef))
      await Promise.resolve()
    })

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })
    expect(mockPremiumApi.assignPremiumInstance).not.toHaveBeenCalled()
    expect(routingService.isPremiumAssigned()).toBe(true)
    expect(routingService.getPremiumInstanceId()).toBe("hash-A")
    expect(routingService.isPremiumShared()).toBe(false)

    routingService.updateRoutingToken("routing-A-restored")
    expectNoRoutingDeadlock()
  })

  test("explicit release() tears routing down as fully as logout", async () => {
    mockPremiumApi.getPremiumStatus.mockResolvedValue(noAssignmentStatus)
    mockPremiumApi.assignPremiumInstance.mockResolvedValue(assignedA)
    mockPremiumApi.releasePremiumInstance.mockResolvedValue({
      message: "released",
      released: true,
    })

    const ctxRef: { current: Ctx | null } = { current: null }
    render(tree(ctxRef))

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })
    routingService.updateRoutingToken("routing-A")
    expect(routingService.getRoutingToken()).toBe("routing-A")

    await act(async () => {
      await ctxRef.current!.release()
    })

    // resetForRelease clears token + instance + shared, not just the assigned
    // flag — the same teardown the beacon/cross-tab paths perform.
    expectRoutingTornDown()
  })

  test("a cross-tab PREMIUM_RELEASED broadcast tears this tab's routing down", async () => {
    mockPremiumApi.getPremiumStatus.mockResolvedValue(noAssignmentStatus)
    mockPremiumApi.assignPremiumInstance.mockResolvedValue(assignedA)

    const ctxRef: { current: Ctx | null } = { current: null }
    render(tree(ctxRef))

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })
    routingService.updateRoutingToken("routing-A")
    expect(routingService.isPremiumAssigned()).toBe(true)
    expect(routingService.getRoutingToken()).toBe("routing-A")

    // Simulate another tab's broadcast by invoking the handler this tab
    // registered (same-document localStorage writes emit no storage event).
    const handlers = mockTabSyncHandlers.get("PREMIUM_RELEASED")
    expect(handlers && handlers.size).toBeGreaterThan(0)
    await act(async () => {
      handlers!.forEach((h) =>
        h({ type: "PREMIUM_RELEASED" } as TabSyncMessage),
      )
      await Promise.resolve()
    })

    expect(ctxRef.current?.assignmentResult).toBeNull()
    expectRoutingTornDown()
  })

  test("closing the tab beacons the release, and only while an assignment is held", async () => {
    mockPremiumApi.getPremiumStatus.mockResolvedValue(noAssignmentStatus)
    mockPremiumApi.assignPremiumInstance.mockResolvedValue(assignedA)

    const ctxRef: { current: Ctx | null } = { current: null }
    render(tree(ctxRef))

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })
    mockSendBeacon.mockClear()

    act(() => {
      window.dispatchEvent(new Event("beforeunload"))
    })

    expect(mockSendBeacon).toHaveBeenCalledTimes(1)
    const [url, body] = mockSendBeacon.mock.calls[0] as [string, Blob]
    expect(url).toMatch(/\/users\/me\/premium\/release-beacon$/)
    expect(body).toBeInstanceOf(Blob)

    // Post-release the handler must stay quiet: no assignment and no beacon
    // token left, so a reload cannot beacon a stale token at the backend.
    await act(async () => {
      await ctxRef.current!.release()
    })
    mockSendBeacon.mockClear()

    act(() => {
      window.dispatchEvent(new Event("beforeunload"))
    })

    expect(mockSendBeacon).not.toHaveBeenCalled()
  })
})
