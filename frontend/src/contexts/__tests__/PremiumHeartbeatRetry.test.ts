/**
 * Tests for the heartbeat retry inside PremiumAssignmentContext.recordActivity.
 *
 * Drives the real provider, so the retry count, the growing wait between
 * attempts, and the terminal rethrow are read off the actual implementation
 * rather than a copy of it kept in this file.
 *
 * Covers:
 *  1. Success on the first attempt: one request, warning dismissed, not failing.
 *  2. A transient failure recovers, and the wait before each retry grows.
 *  3. Every attempt failing: a bounded number of requests, the failure rethrown
 *     so callers such as InactivityWarning can react, and the activity clock
 *     still advanced so an unreachable backend cannot spin the warning.
 *  4. A later success clears the failing flag.
 *  5. Free-tier users send no heartbeat at all.
 *  6. A device wake sends a bare heartbeat (no retry, no clock reset), and is
 *     only armed while a premium assignment is held.
 */

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  jest,
  test,
} from "@jest/globals"
import { renderHook, act } from "@testing-library/react"

import type { PremiumHeartbeatResult } from "api/premium/PremiumAssignmentApi"

// --- Module mocks (must precede the provider import) ---

const mockUser = {
  id: 1,
  uid: "test-uid",
  subscription_plan_name: "Premium",
  subscription_status: "Premium",
}

// Mutable so one test can drop the user to the free tier.
const mockReduxState = {
  user: {
    currentUser: mockUser as typeof mockUser | null,
    logoutGeneration: 0,
  },
  pipeline: { run: { status: "StartUninitialized" } },
}

const mockDispatchFn = jest.fn(() => Promise.resolve())

jest.mock("react-redux", () => ({
  useSelector: (selector: (s: unknown) => unknown) => selector(mockReduxState),
  useDispatch: () => mockDispatchFn,
}))

jest.mock("store/slice/User/UserActions", () => ({
  __esModule: true,
  getMe: () => ({ type: "user/getMe" }),
}))

const mockLogoutFn = jest.fn()

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

// Captures what the provider hands the sleep detector, so a test can play the
// part of a device wake. Prefixed with `mock` for jest's factory scope guard.
const mockSleepDetection: {
  onWake: (() => void) | null
  enabled: boolean | undefined
} = { onWake: null, enabled: undefined }

jest.mock("hooks/useSleepDetection", () => ({
  __esModule: true,
  useSleepDetection: (onWake: () => void, options?: { enabled?: boolean }) => {
    mockSleepDetection.onWake = onWake
    mockSleepDetection.enabled = options?.enabled
  },
}))

// Every timestamp the provider published to the other tabs.
const mockSyncedTimestamps: number[] = []

jest.mock("utils/crossTabSync", () => ({
  __esModule: true,
  tabSync: {
    broadcast: () => {},
    broadcastLogout: () => {},
    broadcastPremiumReleased: () => {},
    on: () => () => {},
    onAny: () => () => {},
    destroy: () => {},
  },
  syncActivityAcrossTabs: (timestamp: number) =>
    mockSyncedTimestamps.push(timestamp),
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

// --- Helpers ---

// Upper bounds for stepping the fake clock. The assertions read the real retry
// count and delays through the provider's behaviour, not through these.
const MAX_ATTEMPTS = 3
const RETRY_DELAY_MS = 1000

const heartbeatOk = {} as PremiumHeartbeatResult

const renderProvider = () =>
  renderHook(() => usePremiumAssignment(), {
    wrapper: PremiumAssignmentProvider,
  })

/**
 * Lets the provider's pending `catch` bodies run before the clock moves. Jest 27
 * has no async timer API, so the backoff `setTimeout` only exists once the
 * rejection has been handled.
 */
const flushMicrotasks = async (): Promise<void> => {
  for (let i = 0; i < 8; i++) {
    await Promise.resolve()
  }
}

const advanceBy = async (ms: number): Promise<void> => {
  await flushMicrotasks()
  jest.advanceTimersByTime(ms)
  await flushMicrotasks()
}

/** Runs recordActivity to completion, stepping the clock over every backoff. */
const runRecordActivity = async (
  recordActivity: () => Promise<void>,
): Promise<unknown> => {
  let caught: unknown = null
  await act(async () => {
    const pending = recordActivity().catch((e: unknown) => {
      caught = e
    })
    for (let attempt = 1; attempt < MAX_ATTEMPTS; attempt++) {
      await advanceBy(RETRY_DELAY_MS * attempt)
    }
    await pending
  })
  return caught
}

// --- Tests ---

describe("recordActivity heartbeat retry", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    jest.useFakeTimers()
    installPremiumApiDefaults()
    mockPremiumApi.getPremiumStatus.mockResolvedValue({
      subscription_type: "premium",
      is_premium: true,
      assignment: null,
    })
    mockPremiumApi.assignPremiumInstance.mockResolvedValue({
      message: "not assigned",
      instance_id: "",
      assigned: false,
    })
    mockSyncedTimestamps.length = 0
    mockSleepDetection.onWake = null
    mockSleepDetection.enabled = undefined
    mockReduxState.user.currentUser = mockUser
  })

  afterEach(() => {
    jest.clearAllTimers()
    jest.useRealTimers()
  })

  test("a heartbeat that lands on the first attempt is sent once", async () => {
    mockPremiumApi.sendPremiumHeartbeat.mockResolvedValue(heartbeatOk)
    const { result } = renderProvider()

    const error = await runRecordActivity(result.current.recordActivity)

    expect(error).toBeNull()
    expect(mockPremiumApi.sendPremiumHeartbeat).toHaveBeenCalledTimes(1)
    expect(result.current.heartbeatFailing).toBe(false)
    expect(result.current.showInactivityWarning).toBe(false)
    expect(mockSyncedTimestamps).toHaveLength(1)
  })

  test("the wait before each retry grows, and a recovered attempt stops the loop", async () => {
    mockPremiumApi.sendPremiumHeartbeat
      .mockRejectedValueOnce(new Error("503"))
      .mockRejectedValueOnce(new Error("503"))
      .mockResolvedValue(heartbeatOk)
    const { result } = renderProvider()

    let caught: unknown = null
    await act(async () => {
      const pending = result.current.recordActivity().catch((e: unknown) => {
        caught = e
      })

      // After the first failure nothing is re-sent until the delay elapses.
      await advanceBy(RETRY_DELAY_MS - 1)
      expect(mockPremiumApi.sendPremiumHeartbeat).toHaveBeenCalledTimes(1)
      await advanceBy(1)
      expect(mockPremiumApi.sendPremiumHeartbeat).toHaveBeenCalledTimes(2)

      // The second wait is longer: one delay is not enough to fire attempt 3.
      await advanceBy(RETRY_DELAY_MS)
      expect(mockPremiumApi.sendPremiumHeartbeat).toHaveBeenCalledTimes(2)
      await advanceBy(RETRY_DELAY_MS)
      await pending
    })

    expect(caught).toBeNull()
    expect(mockPremiumApi.sendPremiumHeartbeat).toHaveBeenCalledTimes(3)
    expect(result.current.heartbeatFailing).toBe(false)
  })

  test("every attempt failing rethrows, flags the failure, and still advances the clock", async () => {
    const failure = new Error("401")
    mockPremiumApi.sendPremiumHeartbeat.mockRejectedValue(failure)
    const { result } = renderProvider()

    const error = await runRecordActivity(result.current.recordActivity)

    // Rethrown so InactivityWarning can show Session Expired on a 401.
    expect(error).toBe(failure)
    // Bounded: an unreachable backend cannot retry forever.
    expect(mockPremiumApi.sendPremiumHeartbeat).toHaveBeenCalledTimes(
      MAX_ATTEMPTS,
    )
    expect(result.current.heartbeatFailing).toBe(true)
    // The local clock still moves, so the 30s inactivity check cannot re-raise
    // the warning immediately while the backend is down.
    expect(mockSyncedTimestamps).toHaveLength(1)
  })

  test("a later success clears the failing flag", async () => {
    mockPremiumApi.sendPremiumHeartbeat.mockRejectedValue(new Error("503"))
    const { result } = renderProvider()

    await runRecordActivity(result.current.recordActivity)
    expect(result.current.heartbeatFailing).toBe(true)

    mockPremiumApi.sendPremiumHeartbeat.mockReset()
    mockPremiumApi.sendPremiumHeartbeat.mockResolvedValue(heartbeatOk)

    const error = await runRecordActivity(result.current.recordActivity)

    expect(error).toBeNull()
    expect(result.current.heartbeatFailing).toBe(false)
  })

  test("a device wake heartbeats once without touching the inactivity clock", async () => {
    mockPremiumApi.getPremiumStatus.mockResolvedValue({
      subscription_type: "premium",
      is_premium: true,
      assignment: {
        instance_id: "inst-A",
        instance_id_hash: "hash-A",
        assigned_at: "2026-07-30T00:00:00Z",
        status: "active",
        is_shared: false,
      },
    })
    mockPremiumApi.sendPremiumHeartbeat.mockResolvedValue(heartbeatOk)
    const { result } = renderProvider()

    await act(async () => {
      await flushMicrotasks()
    })
    expect(result.current.assignmentResult?.instance_id).toBe("inst-A")
    expect(mockSleepDetection.enabled).toBe(true)
    mockPremiumApi.sendPremiumHeartbeat.mockClear()
    mockSyncedTimestamps.length = 0

    await act(async () => {
      mockSleepDetection.onWake?.()
      await flushMicrotasks()
    })

    // One bare send: waking is not a user gesture, so the 2h countdown must not
    // restart and other tabs must not be told there was activity.
    expect(mockPremiumApi.sendPremiumHeartbeat).toHaveBeenCalledTimes(1)
    expect(mockSyncedTimestamps).toHaveLength(0)
  })

  test("the sleep detector stays disarmed while no assignment is held", async () => {
    const { result } = renderProvider()

    await act(async () => {
      await flushMicrotasks()
    })

    expect(result.current.assignmentResult).toBeNull()
    expect(mockSleepDetection.enabled).toBe(false)

    // Even if it fires, a wake without an assignment sends nothing.
    await act(async () => {
      mockSleepDetection.onWake?.()
      await flushMicrotasks()
    })
    expect(mockPremiumApi.sendPremiumHeartbeat).not.toHaveBeenCalled()
  })

  test("a free-tier user sends no heartbeat", async () => {
    mockReduxState.user.currentUser = {
      ...mockUser,
      subscription_plan_name: "Free",
      subscription_status: "Free",
    }
    mockPremiumApi.sendPremiumHeartbeat.mockResolvedValue(heartbeatOk)
    const { result } = renderProvider()

    const error = await runRecordActivity(result.current.recordActivity)

    expect(error).toBeNull()
    expect(mockPremiumApi.sendPremiumHeartbeat).not.toHaveBeenCalled()
  })
})
