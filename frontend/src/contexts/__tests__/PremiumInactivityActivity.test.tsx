/**
 * Tests for the inactivity-clock reset on genuine activity (Issue #737).
 *
 * Covers:
 *  1. User interaction (pointerdown) resets the clock and prevents the 2h
 *     auto-release.
 *  2. A running workflow keeps the instance active with no direct user input.
 *  3. Activity broadcast by another tab dismisses this tab's warning and
 *     advances this tab's clock, with no interaction in this tab.
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

// --- Module mocks (must precede provider import) ---

const mockUser = {
  id: 1,
  uid: "test-uid",
  subscription_plan_name: "Premium",
  subscription_status: "Premium",
}

// Mutable pipeline status so a test can simulate a running workflow.
// Prefixed with `mock` so the jest.mock factory may reference it.
let mockPipelineStatus = "StartUninitialized"

const mockDispatchFn = jest.fn(() => Promise.resolve())
const mockLogoutFn = jest.fn()
const mockBroadcastPremiumReleased = jest.fn()
// Captures the provider's cross-tab activity subscriber so a test can play the
// part of the other tab. Prefixed with `mock` so the jest.mock factory may
// reference it.
const mockActivityFromOtherTab: Set<(timestamp: number) => void> = new Set()
const mockLogPremiumUiEvent = jest.fn()

jest.mock("react-redux", () => ({
  useSelector: (selector: (s: unknown) => unknown) =>
    selector({
      user: { currentUser: mockUser, logoutGeneration: 0 },
      pipeline: { run: { status: mockPipelineStatus } },
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

jest.mock("utils/crossTabSync", () => ({
  __esModule: true,
  tabSync: {
    broadcast: () => {},
    broadcastLogout: () => {},
    broadcastPremiumReleased: mockBroadcastPremiumReleased,
    on: () => () => {},
    onAny: () => () => {},
    destroy: () => {},
  },
  syncActivityAcrossTabs: () => {},
  getLastActivityFromAnyTab: () => 0,
  onActivityFromOtherTab: (handler: (timestamp: number) => void) => {
    mockActivityFromOtherTab.add(handler)
    return () => mockActivityFromOtherTab.delete(handler)
  },
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

// Re-render trigger: the mocked useSelector reads `mockPipelineStatus` at
// render time only, so a test that flips the status mid-run must force the
// provider to re-render (without touching the activity clock) to pick it up.
// A fresh element tree is required — re-rendering with an unchanged children
// prop would bail out and never re-run the provider.
let triggerRerender: () => void = () => {}

const renderProvider = () => {
  const ctxRef: { current: Ctx | null } = { current: null }
  const buildTree = () => (
    <SnackbarProvider maxSnack={3}>
      <PremiumAssignmentProvider>
        <Harness ctxRef={ctxRef} />
      </PremiumAssignmentProvider>
    </SnackbarProvider>
  )
  const utils = render(buildTree())
  triggerRerender = () => utils.rerender(buildTree())
  return ctxRef
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

const advance = async (ms: number) => {
  await act(async () => {
    jest.advanceTimersByTime(ms)
    await Promise.resolve()
  })
}

const ONE_MIN = 60 * 1000

// --- Tests ---

describe("PremiumAssignmentProvider — activity resets inactivity clock", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    jest.useFakeTimers()
    localStorage.clear()
    sessionStorage.clear()
    mockActivityFromOtherTab.clear()
    mockPipelineStatus = "StartUninitialized"
  })

  afterEach(() => {
    jest.clearAllTimers()
    jest.useRealTimers()
  })

  test("user interaction resets the clock and prevents the 2h auto-release", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Almost 2h with no activity — not released yet.
    await advance(115 * ONE_MIN)
    expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")

    // Genuine user interaction resets the clock.
    act(() => {
      window.dispatchEvent(new Event("pointerdown"))
    })

    // Another ~2h from mount, but under 2h since the interaction.
    await advance(115 * ONE_MIN)

    // Activity prevented the auto-release.
    expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
  })

  test("a running workflow prevents the 2h auto-release with no user input", async () => {
    mockPipelineStatus = "StartSuccess"
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Advance past 2h with zero interaction — workflow keeps it active.
    await advance(2 * 60 * ONE_MIN + 5000)

    expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    expect(ctxRef.current?.showInactivityWarning).toBe(false)
  })

  test.each([["keydown"], ["scroll"]])(
    "%s also resets the clock and prevents the 2h auto-release",
    async (eventName) => {
      mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)
      const ctxRef = renderProvider()

      await waitFor(() => {
        expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
      })

      await advance(115 * ONE_MIN)
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")

      act(() => {
        window.dispatchEvent(new Event(eventName))
      })

      await advance(115 * ONE_MIN)

      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    },
  )

  test("interaction dismisses the 1h inactivity warning", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Cross the 1h threshold (but stay under 2h) — the warning appears.
    await advance(61 * ONE_MIN)
    expect(ctxRef.current?.showInactivityWarning).toBe(true)
    expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")

    // Interacting with the page dismisses the warning (basis for the UI text).
    act(() => {
      window.dispatchEvent(new Event("keydown"))
    })
    expect(ctxRef.current?.showInactivityWarning).toBe(false)
    expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
  })

  test("another tab's activity dismisses this tab's warning without any interaction here", async () => {
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    await advance(61 * ONE_MIN)
    expect(ctxRef.current?.showInactivityWarning).toBe(true)

    // Play the other tab: a Stay Active click there writes the shared activity
    // timestamp, which reaches this tab as a storage event.
    expect(mockActivityFromOtherTab.size).toBeGreaterThan(0)
    act(() => {
      mockActivityFromOtherTab.forEach((handler) => handler(Date.now()))
    })

    expect(ctxRef.current?.showInactivityWarning).toBe(false)
    expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    // No heartbeat of our own: this tab only mirrors the other tab's state.
    expect(mockSendPremiumHeartbeat).not.toHaveBeenCalled()

    // The clock advanced too, so the release deadline moved with it.
    await advance(115 * ONE_MIN)
    expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
  })

  test("countdown resumes and releases 2h after a workflow finishes", async () => {
    mockPipelineStatus = "StartSuccess"
    mockGetPremiumStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")
    })

    // Workflow running: no release even past 2h.
    await advance(2 * 60 * ONE_MIN + 5000)
    expect(ctxRef.current?.assignmentResult?.instance_id).toBe("inst-A")

    // Workflow finishes → the running-workflow guard clears on next render.
    mockPipelineStatus = "Finished"
    act(() => {
      triggerRerender()
    })

    // With no workflow and no activity, the 2h countdown resumes from finish.
    await advance(2 * 60 * ONE_MIN + 5000)
    expect(ctxRef.current?.assignmentResult).toBeNull()
  })
})
