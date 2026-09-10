/**
 * Tests for the re-trigger assign logic during polling.
 *
 * Covers:
 *  1. Re-trigger fires at correct interval (every ASSIGN_RETRY_POLL_THRESHOLD polls).
 *  2. Re-trigger success transitions state correctly.
 *  3. Re-trigger failure continues polling without crash.
 *  4. Normal assign (assigned: true) does NOT fire re-trigger (regression test).
 *  5. MAX_POLL_ATTEMPTS resets flags for retryable cases.
 *  6. User gesture recovery after MAX_POLL_ATTEMPTS exhaustion.
 *  7. Instance-lost recovery: re-trigger fires when assigned instance is
 *     stopped/terminated externally.
 *  8. Instance-lost MAX_POLL_ATTEMPTS: flag reset enables user-gesture
 *     recovery after stop/terminate exhausts polling.
 *  9. Re-trigger stops after MAX_RETRIGGER_ATTEMPTS (bounded counter).
 * 10. Release during poll prevents re-trigger (stale closure prevention).
 * 11. Same-id restart: unreachable persists until confirmed reachable.
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

// Neutralize the dedicated warm-up grace here: these tests flip unreachable
// immediately after a fresh dedicated assignment. The grace (which now covers
// the initial undefined → dedicated case too) would otherwise suppress that
// first 5xx. The grace itself is covered in
// useInstanceUnreachableMachineLeader.test.tsx.
// "mock" prefix required for Jest's out-of-scope factory guard.
const mockUnreachableConstants = jest.requireActual(
  "contexts/premium/unreachableConstants",
) as typeof import("contexts/premium/unreachableConstants")
jest.mock("contexts/premium/unreachableConstants", () => ({
  __esModule: true,
  ...mockUnreachableConstants,
  DEDICATED_HANDOFF_GRACE_MS: 0,
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

/** A successful dedicated assignment. */
const dedicatedAssignment: PremiumAssignmentResult = {
  message: "dedicated",
  instance_id: "inst-A",
  instance_id_hash: "hash-A",
  assigned: true,
  is_shared: false,
  assignment_source: "existing",
}

/** Status endpoint returning null assignment (no assignment exists). */
const nullAssignmentStatus: PremiumStatusResult = {
  subscription_type: UserTier.PREMIUM,
  is_premium: true,
  assignment: null,
}

/** Status endpoint returning a dedicated assignment. */
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

/** As above, plus the hash the real endpoint returns, so routing can re-arm. */
const dedicatedStatusWithHash: PremiumStatusResult = {
  subscription_type: UserTier.PREMIUM,
  is_premium: true,
  assignment: {
    ...dedicatedStatus.assignment!,
    instance_id_hash: "hash-A",
  },
}

/** Simulate a pointerdown event on the window (as a user click). */
const simulateClick = () => {
  window.dispatchEvent(new Event("pointerdown"))
}

/**
 * Advance timers and flush microtasks to simulate polling cycles.
 * Each call advances 60s (enough for any backoff interval).
 */
const advanceOnePollCycle = async () => {
  await act(async () => {
    jest.advanceTimersByTime(60_000)
    await Promise.resolve()
  })
}

// --- Tests ---

describe("PremiumAssignmentProvider — re-trigger assign during polling", () => {
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

  test("re-trigger fires at correct interval (every 3 polls with null status)", async () => {
    // autoAssignOnLogin: /status returns null → calls /assign → retryable
    mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)
    mockAssignPremiumInstance.mockResolvedValue(retryableAssignment)

    renderProvider()

    // Wait for autoAssignOnLogin to complete with retryable response.
    await waitFor(() => {
      expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)
    })

    // Clear to track only polling-phase calls.
    mockAssignPremiumInstance.mockClear()
    // Keep returning null status so re-trigger fires.
    mockAssignPremiumInstance.mockResolvedValue(retryableAssignment)

    // Poll 1: no re-trigger expected
    await advanceOnePollCycle()
    expect(mockAssignPremiumInstance).not.toHaveBeenCalled()

    // Poll 2: no re-trigger expected
    await advanceOnePollCycle()
    expect(mockAssignPremiumInstance).not.toHaveBeenCalled()

    // Poll 3: re-trigger expected (pollAttempts=2, (2+1)%3===0)
    await advanceOnePollCycle()
    expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)

    mockAssignPremiumInstance.mockClear()

    // Polls 4-5: no re-trigger
    await advanceOnePollCycle()
    await advanceOnePollCycle()
    expect(mockAssignPremiumInstance).not.toHaveBeenCalled()

    // Poll 6: re-trigger again
    await advanceOnePollCycle()
    expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)
  })

  test("re-trigger success transitions state and stops polling", async () => {
    // autoAssignOnLogin: retryable
    mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)
    mockAssignPremiumInstance.mockResolvedValue(retryableAssignment)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)
    })

    // After polls 1-2 return null, poll 3 re-triggers assign.
    // This time, assign succeeds.
    mockAssignPremiumInstance.mockResolvedValue(dedicatedAssignment)

    // Advance through 3 poll cycles.
    await advanceOnePollCycle() // poll 1
    await advanceOnePollCycle() // poll 2
    await advanceOnePollCycle() // poll 3 → re-trigger → success

    // State should reflect the dedicated assignment.
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Routing should be configured.
    expect(routingService.isPremiumAssigned()).toBe(true)

    // Error should be cleared.
    expect(ctxRef.current?.error).toBeNull()

    // Beacon token should have been acquired.
    expect(mockGetBeaconTokenApi).toHaveBeenCalled()
  })

  test("re-trigger failure continues polling without crash", async () => {
    // autoAssignOnLogin: retryable
    mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)
    mockAssignPremiumInstance.mockResolvedValue(retryableAssignment)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)
    })

    // Re-trigger will throw on poll 3.
    mockAssignPremiumInstance.mockRejectedValue(new Error("Lock contention"))

    // Advance through 3 polls → re-trigger fires and fails.
    await advanceOnePollCycle()
    await advanceOnePollCycle()
    await advanceOnePollCycle()

    // Polling should continue — no crash.
    // assignmentResult should still be the retryable one (not cleared).
    expect(ctxRef.current?.assignmentResult?.assigned).toBe(false)
    // error is the original retryable message from autoAssignOnLogin — the
    // re-trigger failure must NOT replace or escalate it.
    expect(ctxRef.current?.error).toBe("scaling in progress")

    // Advance one more cycle to confirm polling continues.
    mockAssignPremiumInstance.mockClear()
    await advanceOnePollCycle() // poll 4
    // Still polling (getPremiumStatus called).
    expect(mockGetPremiumStatus.mock.calls.length).toBeGreaterThan(3)
  })

  test("normal assign (assigned: true) does NOT fire re-trigger", async () => {
    // autoAssignOnLogin: /status returns null → calls /assign → succeeds
    mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)
    mockAssignPremiumInstance.mockResolvedValue(dedicatedAssignment)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Polling should not be active (dedicated assignment found).
    // assignPremiumInstance should have been called exactly once.
    expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)

    mockAssignPremiumInstance.mockClear()
    mockGetPremiumStatus.mockClear()

    // Advance several cycles — no re-trigger or polling should fire.
    await advanceOnePollCycle()
    await advanceOnePollCycle()
    await advanceOnePollCycle()

    expect(mockAssignPremiumInstance).not.toHaveBeenCalled()
  })

  test("MAX_POLL_ATTEMPTS resets flags for retryable cases", async () => {
    // Cannot pre-seed pollAttempts via sessionStorage because the reset
    // effect clears it when assignmentResult.is_shared changes on mount.
    // Instead, loop through all 40 poll cycles (fast with fake timers).
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

    // State should show error and assignmentResult should be null (polling stopped).
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult).toBeNull()
      expect(ctxRef.current?.error).toContain(
        "No premium instance available after extended wait",
      )
    })

    // sessionStorage hasAttempted should be cleared (allowing fresh retry).
    expect(sessionStorage.getItem("premium_hasAttempted")).toBeNull()
  })

  test("user gesture recovery after MAX_POLL_ATTEMPTS exhaustion", async () => {
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
    })

    // Now set up mocks for the recovery path: assign succeeds.
    mockAssignPremiumInstance.mockClear()
    mockGetPremiumStatus.mockClear()
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    // Simulate user gesture (click) — should trigger fresh autoAssignOnLogin.
    act(() => {
      simulateClick()
    })

    // autoAssignOnLogin should re-fire via autoAssignGeneration bump.
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Error should be cleared.
    expect(ctxRef.current?.error).toBeNull()
  })

  test("instance-lost: re-trigger fires when assigned instance is stopped/terminated", async () => {
    // autoAssignOnLogin: /status returns dedicated → state has assigned:true
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)
    mockAssignPremiumInstance.mockResolvedValue(dedicatedAssignment)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Simulate instance stopped/terminated: status now returns null assignment.
    mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)
    mockAssignPremiumInstance.mockClear()

    // The new dedicated instance after re-assign.
    const newDedicated: PremiumAssignmentResult = {
      message: "dedicated",
      instance_id: "inst-B",
      instance_id_hash: "hash-B",
      assigned: true,
      is_shared: false,
      assignment_source: "new",
    }
    mockAssignPremiumInstance.mockResolvedValue(newDedicated)

    // Trigger the unreachable state so shouldPoll returns true even though
    // we have a dedicated assignment. This simulates the 502/503 handler
    // calling emitPremiumUnreachable() when the dead instance is hit.
    act(() => {
      routingService.emitPremiumUnreachable({
        url: "/api/test",
        status: 502,
        sentAt: Date.now(),
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })

    // Poll 1: no re-trigger (pollAttempts=0)
    await advanceOnePollCycle()
    expect(mockAssignPremiumInstance).not.toHaveBeenCalled()

    // Poll 2: no re-trigger (pollAttempts=1)
    await advanceOnePollCycle()
    expect(mockAssignPremiumInstance).not.toHaveBeenCalled()

    // Poll 3: re-trigger fires (pollAttempts=2, (2+1)%3===0)
    await advanceOnePollCycle()
    expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)

    // State should reflect the new dedicated assignment.
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-B")
    })

    // Routing should be configured.
    expect(routingService.isPremiumAssigned()).toBe(true)
  })

  test("instance-lost: MAX_POLL_ATTEMPTS resets flags for user-gesture recovery", async () => {
    // autoAssignOnLogin: /status returns dedicated → state has assigned:true
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)
    mockAssignPremiumInstance.mockResolvedValue(dedicatedAssignment)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Simulate instance lost: status returns null.
    mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)
    // Re-trigger assign also fails (keeps returning retryable).
    mockAssignPremiumInstance.mockResolvedValue(retryableAssignment)

    // Trigger unreachable to enable polling for the dedicated assignment.
    act(() => {
      routingService.emitPremiumUnreachable({
        url: "/api/test",
        status: 503,
        sentAt: Date.now(),
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })

    // Advance through all 40 poll cycles to reach MAX_POLL_ATTEMPTS.
    for (let i = 0; i < 40; i++) {
      await advanceOnePollCycle()
    }

    // State should show error and assignmentResult should be null.
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult).toBeNull()
      expect(ctxRef.current?.error).toContain(
        "No premium instance available after extended wait",
      )
    })

    // hasAttempted should be cleared — user gesture can trigger recovery.
    expect(sessionStorage.getItem("premium_hasAttempted")).toBeNull()

    // Set up mocks for recovery: status returns a new dedicated instance.
    const newDedicatedStatus: PremiumStatusResult = {
      subscription_type: UserTier.PREMIUM,
      is_premium: true,
      assignment: {
        instance_id: "inst-C",
        is_shared: false,
        assigned_at: "2026-06-23T00:00:00Z",
        status: "active",
      },
    }
    mockGetPremiumStatus.mockResolvedValue(newDedicatedStatus)

    // Simulate user gesture (click) — should trigger fresh autoAssignOnLogin.
    act(() => {
      simulateClick()
    })

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-C")
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    expect(ctxRef.current?.error).toBeNull()
  })

  test("re-trigger stops after MAX_RETRIGGER_ATTEMPTS (bounded counter)", async () => {
    // autoAssignOnLogin: retryable
    mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)
    mockAssignPremiumInstance.mockResolvedValue(retryableAssignment)

    renderProvider()

    await waitFor(() => {
      expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)
    })

    // Clear to track only re-trigger calls.
    mockAssignPremiumInstance.mockClear()
    mockAssignPremiumInstance.mockResolvedValue(retryableAssignment)

    // Re-trigger fires every 3 polls. With MAX_RETRIGGER_ATTEMPTS=5,
    // re-triggers fire at polls 3, 6, 9, 12, 15 (retriggerCount 1-5)
    // then stop. Advance 18 polls to exceed the limit.
    for (let i = 0; i < 18; i++) {
      await advanceOnePollCycle()
    }

    // Exactly 5 re-trigger calls should have fired.
    expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(5)

    // Advance 3 more polls (poll 19-21) — no further re-triggers.
    mockAssignPremiumInstance.mockClear()
    for (let i = 0; i < 3; i++) {
      await advanceOnePollCycle()
    }
    expect(mockAssignPremiumInstance).not.toHaveBeenCalled()
  })

  test("release during poll prevents re-trigger (stale closure prevention)", async () => {
    // Start with dedicated assignment (autoAssignOnLogin takes already-assigned path).
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Trigger unreachable → polling starts.
    act(() => {
      routingService.emitPremiumUnreachable({
        url: "/api/test",
        status: 502,
        sentAt: Date.now(),
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })

    // Configure getPremiumStatus:
    //  - Polls 1-2: return null normally (pollAttempts climbs to 2)
    //  - Poll 3: fire cross-tab PREMIUM_RELEASED before returning null
    // Poll 3 is where re-trigger would fire (pollAttempts=2, (2+1)%3=0).
    // The release increments releaseGenerationRef synchronously, so the
    // post-await liveness check detects the mismatch and bails before
    // reaching the re-trigger section.
    let statusCallCount = 0
    mockGetPremiumStatus.mockImplementation(async () => {
      statusCallCount++
      if (statusCallCount === 3) {
        const handlers = mockTabSyncHandlers.get("PREMIUM_RELEASED")
        handlers?.forEach((h) => h({} as TabSyncMessage))
      }
      return nullAssignmentStatus
    })

    // Clear assign mock — no re-trigger calls expected.
    mockAssignPremiumInstance.mockClear()
    mockAssignPremiumInstance.mockResolvedValue(dedicatedAssignment)

    // Advance 3 polls.
    await advanceOnePollCycle() // poll 1 (normal)
    await advanceOnePollCycle() // poll 2 (normal)
    await advanceOnePollCycle() // poll 3 (release during status → bail)

    // Without the liveness check, the stale closure would have read
    // state.assignmentResult as non-null and fired assignPremiumInstance,
    // resurrecting the released instance.
    expect(mockAssignPremiumInstance).not.toHaveBeenCalled()

    // assignmentResult cleared by the PREMIUM_RELEASED handler.
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult).toBeNull()
    })
  })

  test("same-id restart: unreachable persists until confirmed reachable", async () => {
    // autoAssignOnLogin: dedicated inst-A (already-assigned path).
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Instance goes unreachable (stopped/crashed).
    mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)
    mockAssignPremiumInstance.mockClear()

    // Same instance restarted — same instance_id, different source.
    const restartedSameInstance: PremiumAssignmentResult = {
      message: "dedicated",
      instance_id: "inst-A",
      instance_id_hash: "hash-A",
      assigned: true,
      is_shared: false,
      assignment_source: "restarted_instance",
    }
    mockAssignPremiumInstance.mockResolvedValue(restartedSameInstance)

    // Trigger unreachable.
    act(() => {
      routingService.emitPremiumUnreachable({
        url: "/api/test",
        status: 502,
        sentAt: Date.now(),
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })

    // Advance 3 polls → re-trigger fires → returns same inst-A.
    await advanceOnePollCycle()
    await advanceOnePollCycle()
    await advanceOnePollCycle()

    expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)

    // Assignment updates to the restarted instance.
    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
      expect(ctxRef.current?.assignmentResult?.assignment_source).toBe(
        "restarted_instance",
      )
    })

    // Unreachable persists — same instance_id means the CLEAR branch
    // in useInstanceUnreachableMachine is a no-op. The "unresponsive"
    // snackbar stays visible until a real premium 200 fires
    // emitPremiumReachable.
    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
  })

  // An ECS task crash takes the container down but leaves the EC2 instance and
  // the assignment row intact, so recovery must be a plain 200 with no new row.
  test("ECS task crash: a 200 from the same instance recovers with no re-assign", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatusWithHash)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })
    mockAssignPremiumInstance.mockClear()

    // The ALB answers 502 while ECS places the replacement task, which is what
    // the axios teardown turns into an unreachable emission.
    act(() => {
      routingService.setPremiumAssigned(false)
      routingService.emitPremiumUnreachable({
        url: "/api/test",
        status: 502,
        sentAt: 1000,
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })
    expect(routingService.isPremiumAssigned()).toBe(false)

    // Replacement task is HEALTHY: the next request is served by the same
    // instance hash, so routing re-arms without touching /premium/assign.
    act(() => {
      routingService.emitPremiumReachable({
        url: "/api/test",
        status: 200,
        sentAt: 2000,
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
    })
    expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    expect(ctxRef.current?.unreachable.state.failedProbes).toBe(0)
    expect(routingService.isPremiumAssigned()).toBe(true)
    expect(mockAssignPremiumInstance).not.toHaveBeenCalled()
  })

  // Chain B: after EventBridge cleanup the per-user ALB rule is gone, so the
  // request succeeds (200) but from the wrong instance. Terminate is
  // irreversible, so recovery has to land on a different instance.
  test("terminated instance, Chain B detection: a wrong-instance 200 drives recovery onto a new instance", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatusWithHash)

    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // The row is gone from the backend, and /assign will hand out a new one.
    mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)
    mockAssignPremiumInstance.mockClear()
    mockAssignPremiumInstance.mockResolvedValue({
      message: "dedicated",
      instance_id: "inst-B",
      instance_id_hash: "hash-B",
      assigned: true,
      is_shared: false,
      assignment_source: "new",
    })

    // Chain B carries a 200, not a 5xx: the response arrived, just not from us.
    act(() => {
      routingService.setPremiumAssigned(false)
      routingService.emitPremiumUnreachable({
        url: "/api/test",
        status: 200,
        sentAt: 1000,
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })

    await advanceOnePollCycle()
    await advanceOnePollCycle()
    expect(mockAssignPremiumInstance).not.toHaveBeenCalled()

    await advanceOnePollCycle()
    expect(mockAssignPremiumInstance).toHaveBeenCalledTimes(1)

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-B")
    })
    // A different instance clears the unreachable state outright, unlike the
    // same-id restart above.
    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
    expect(routingService.isPremiumAssigned()).toBe(true)
  })
})
