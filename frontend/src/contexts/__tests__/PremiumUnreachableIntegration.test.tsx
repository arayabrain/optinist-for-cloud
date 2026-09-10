/**
 * Provider-level integration tests for the instance-unreachable state
 * machine. Unlike PremiumInstanceUnreachable.test.ts (pure helpers), these
 * mount PremiumAssignmentProvider with real routingService + a capturable
 * tabSync mock, and drive scenarios through real emit calls.
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

// --- Module mocks (must be declared before importing the provider) ---

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

// Neutralize the dedicated warm-up grace here: these are raw machine-transition
// integration tests that flip unreachable immediately after a fresh dedicated
// assignment. The grace (which now covers the initial undefined → dedicated
// case too) would otherwise suppress that first 5xx. The grace itself is
// covered in useInstanceUnreachableMachineLeader.test.tsx.
// "mock" prefix required for Jest's out-of-scope factory guard.
const mockUnreachableConstants = jest.requireActual(
  "contexts/premium/unreachableConstants",
) as typeof import("contexts/premium/unreachableConstants")
jest.mock("contexts/premium/unreachableConstants", () => ({
  __esModule: true,
  ...mockUnreachableConstants,
  DEDICATED_HANDOFF_GRACE_MS: 0,
}))

// Mock tabSync so tests can invoke handlers directly. "mock" prefix required for Jest's out-of-scope guard.
const mockTabSyncHandlers: Map<
  TabSyncMessageType,
  Set<(msg: TabSyncMessage) => void>
> = new Map()
const mockTabSyncBroadcasts: TabSyncMessage[] = []

jest.mock("utils/crossTabSync", () => ({
  __esModule: true,
  tabSync: {
    broadcast: (msg: TabSyncMessage) => {
      mockTabSyncBroadcasts.push(msg)
    },
    broadcastLogout: () => {},
    broadcastPremiumReleased: () => {},
    on: (type: TabSyncMessageType, handler: (m: TabSyncMessage) => void) => {
      if (!mockTabSyncHandlers.has(type)) {
        mockTabSyncHandlers.set(type, new Set())
      }
      mockTabSyncHandlers.get(type)!.add(handler)
      return () => mockTabSyncHandlers.get(type)?.delete(handler)
    },
    onAny: () => () => {},
    destroy: () => {},
  },
  syncActivityAcrossTabs: () => {},
  getLastActivityFromAnyTab: () => null,
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

// --- Imports (after mocks) ---
// require() not import: static imports hoist above mock vars and cause TDZ when factories run.

const { MAX_FAILED_PROBES } =
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  require("contexts/premium/unreachableConstants")
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

const fireTabSync = (type: TabSyncMessageType, payload: unknown) => {
  const handlers = mockTabSyncHandlers.get(type)
  if (!handlers) return
  handlers.forEach((h) => h({ type, payload }))
}

const mockedGetStatus = mockGetPremiumStatus
const mockedAssign = mockAssignPremiumInstance
const mockedLog = mockLogPremiumUiEvent

const dedicatedStatus: PremiumStatusResult = {
  subscription_type: UserTier.PREMIUM,
  is_premium: true,
  assignment: {
    instance_id: "inst-A",
    is_shared: false,
    assigned_at: "2023-01-01T00:00:00Z",
    status: "active",
  },
}

const sharedStatus: PremiumStatusResult = {
  user_id: 1,
  subscription_type: UserTier.PREMIUM,
  is_premium: true,
  assignment: {
    instance_id: "inst-shared",
    is_shared: true,
    assigned_at: "2023-01-01T00:00:00Z",
    status: "active",
  },
}

// --- Tests ---

describe("PremiumAssignmentProvider — unreachable state machine", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    mockTabSyncHandlers.clear()
    mockTabSyncBroadcasts.length = 0
    localStorage.clear()
    sessionStorage.clear()
    // Real routingService: reset listeners and premiumAssigned
    routingService.clearRoutingInfo()
    routingService.setPremiumAssigned(false)
  })

  afterEach(() => {
    jest.clearAllTimers()
  })

  test("shared /status assignment forwards is_shared to routingService.setPremiumShared", async () => {
    // Wiring guard: the establishment site must forward is_shared so the axios
    // teardown gate (isPremiumShared) sees a shared assignment.
    mockedGetStatus.mockResolvedValue(sharedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.is_shared).toBe(true)
    })
    expect(routingService.isPremiumShared()).toBe(true)
  })

  test("dedicated /status assignment leaves premiumShared false", async () => {
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.is_shared).toBe(false)
    })
    expect(routingService.isPremiumShared()).toBe(false)
  })

  test("HEALTHY → DEGRADED on emitPremiumUnreachable, then clears on emitPremiumReachable", async () => {
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
      expect(ctxRef.current?.assignmentResult?.is_shared).toBe(false)
    })

    act(() => {
      routingService.emitPremiumUnreachable({
        url: "/x",
        status: 503,
        sentAt: 1000,
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })
    expect(ctxRef.current?.unreachable.state.unreachableSince).toBeGreaterThan(
      0,
    )
    expect(ctxRef.current?.unreachable.state.failedProbes).toBe(0)
    expect(ctxRef.current?.unreachable.state.isUnreachableTerminal).toBe(false)

    // Broadcast emitted on HEALTHY → DEGRADED
    expect(
      mockTabSyncBroadcasts.find(
        (m) => m.type === "PREMIUM_INSTANCE_UNREACHABLE",
      ),
    ).toBeTruthy()
    expect(mockedLog).toHaveBeenCalledWith(
      "instance_unreachable",
      expect.objectContaining({ status: 503 }),
    )

    act(() => {
      routingService.emitPremiumReachable({
        url: "/x",
        status: 200,
        sentAt: 2000,
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
    })
    expect(ctxRef.current?.unreachable.state.unreachableSince).toBeNull()
    expect(mockedLog).toHaveBeenCalledWith(
      "instance_reachable",
      expect.objectContaining({ instance_id: "inst-A" }),
    )
  })

  test("stale unreachable (older sentAt than last reachable) is suppressed", async () => {
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Reachable observed at sentAt=2000 — establishes the watermark.
    act(() => {
      routingService.emitPremiumReachable({ status: 200, sentAt: 2000 })
    })

    // Late-arriving failure whose request was sent at t=1500 must be ignored.
    act(() => {
      routingService.emitPremiumUnreachable({ status: 503, sentAt: 1500 })
    })

    // Give any reactive work a chance to flush.
    await act(async () => {
      await Promise.resolve()
    })

    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
    expect(mockedLog).not.toHaveBeenCalledWith(
      "instance_unreachable",
      expect.anything(),
    )
  })

  test("stale failure that tears routing down must not strand premium routing", async () => {
    // Phase 2 repro (grievance B). The axios teardown choke-point has no
    // staleness knowledge: past warm-up it tears premiumAssigned down on any
    // 5xx. The machine, however, suppresses a stale failure (older sentAt than
    // the last reachable) and never flips to unreachable — so no recovery probe
    // arms. Nothing re-arms premiumAssigned → premium routing is stranded on
    // free tier until the next reload.
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Healthy, dedicated, steady state: clear the warm-up window so the
    // interceptor teardown is no longer warm-up-suppressed.
    act(() => {
      routingService.setPremiumAssigned(true)
      routingService.clearPremiumWarmup()
    })
    expect(routingService.isWithinPremiumWarmup()).toBe(false)

    // A successful premium response sets the reachable watermark at sentAt=2000.
    act(() => {
      routingService.emitPremiumReachable({ status: 200, sentAt: 2000 })
    })

    // A request sent earlier (sentAt=1500) 5xxs late. Mirror the axios choke-point
    // (tearDownPremiumRoutingUnlessWarmup): past warm-up it still skips teardown
    // for a stale failure, so routing is left intact.
    act(() => {
      const detail = { status: 503, sentAt: 1500 }
      if (
        !routingService.isWithinPremiumWarmup() &&
        !routingService.isStalePremiumFailure(detail.sentAt)
      ) {
        routingService.setPremiumAssigned(false)
        routingService.emitPremiumUnreachable(detail)
      }
    })
    await act(async () => {
      await Promise.resolve()
    })

    // The failure is recognized as stale (1500 < watermark 2000), so the
    // choke-point never tears routing down and the machine never flips.
    expect(routingService.isStalePremiumFailure(1500)).toBe(true)
    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)

    // Invariant: a stale failure must not downgrade routing to free tier.
    expect(routingService.isPremiumAssigned()).toBe(true)
  })

  test("peer UNREACHABLE broadcast applies state including failed_probes/is_terminal", async () => {
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    act(() => {
      fireTabSync("PREMIUM_INSTANCE_UNREACHABLE", {
        instance_id: "inst-A",
        unreachable_since: 5000,
        failed_probes: 3,
        is_terminal: false,
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })
    expect(ctxRef.current?.unreachable.state.unreachableSince).toBe(5000)
    expect(ctxRef.current?.unreachable.state.failedProbes).toBe(3)
    expect(ctxRef.current?.unreachable.state.isUnreachableTerminal).toBe(false)

    // Peer-initiated broadcasts must NOT re-log or re-broadcast (echo prevention).
    expect(mockedLog).not.toHaveBeenCalledWith(
      "instance_unreachable",
      expect.anything(),
    )
    expect(
      mockTabSyncBroadcasts.find(
        (m) => m.type === "PREMIUM_INSTANCE_UNREACHABLE",
      ),
    ).toBeUndefined()
  })

  test("peer PROBE_UPDATE broadcast updates probe count", async () => {
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Enter unreachable first so PROBE_UPDATE is considered relevant.
    act(() => {
      fireTabSync("PREMIUM_INSTANCE_UNREACHABLE", {
        instance_id: "inst-A",
        unreachable_since: 5000,
        failed_probes: 0,
        is_terminal: false,
      })
    })
    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })

    act(() => {
      fireTabSync("PREMIUM_INSTANCE_PROBE_UPDATE", {
        instance_id: "inst-A",
        failed_probes: MAX_FAILED_PROBES,
        is_terminal: true,
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.failedProbes).toBe(
        MAX_FAILED_PROBES,
      )
    })
    expect(ctxRef.current?.unreachable.state.isUnreachableTerminal).toBe(true)
  })

  test("peer REACHABLE broadcast clears unreachable state", async () => {
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    act(() => {
      fireTabSync("PREMIUM_INSTANCE_UNREACHABLE", {
        instance_id: "inst-A",
        unreachable_since: 5000,
        failed_probes: 2,
        is_terminal: false,
      })
    })
    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })

    act(() => {
      fireTabSync("PREMIUM_INSTANCE_REACHABLE", { instance_id: "inst-A" })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
    })
    expect(ctxRef.current?.unreachable.state.failedProbes).toBe(0)
    expect(ctxRef.current?.unreachable.state.isUnreachableTerminal).toBe(false)
  })

  test("local reachable recovery (concurrent success, no probe) re-arms premiumAssigned", async () => {
    // #754: a concurrent premium failure tore routing down, then a concurrent
    // premium success emits reachable and clears the machine before the
    // half-open probe arms. The exit side must re-arm premiumAssigned, or the
    // user is stranded on free tier until reload.
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Teardown choke-point flips the machine and turns routing off, but keeps
    // the instance identity (only release/logout nulls it).
    act(() => {
      routingService.setPremiumInstanceId("inst-A")
      routingService.setPremiumAssigned(false)
      routingService.emitPremiumUnreachable({ status: 503, sentAt: 1000 })
    })
    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })

    // Concurrent success emits reachable — recovery NOT via the probe.
    act(() => {
      routingService.emitPremiumReachable({ status: 200, sentAt: 2000 })
    })
    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
    })

    expect(routingService.isPremiumAssigned()).toBe(true)
  })

  test("peer reachable recovery also ends with premiumAssigned re-armed", async () => {
    // Guards the leader/peer symmetry: both reachable paths re-arm routing.
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    act(() => {
      routingService.setPremiumAssigned(false)
      fireTabSync("PREMIUM_INSTANCE_UNREACHABLE", {
        instance_id: "inst-A",
        unreachable_since: 5000,
        failed_probes: 0,
        is_terminal: false,
      })
    })
    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })

    act(() => {
      fireTabSync("PREMIUM_INSTANCE_REACHABLE", { instance_id: "inst-A" })
    })
    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
    })

    expect(routingService.isPremiumAssigned()).toBe(true)
  })

  test("late echo of unreachable does not re-log", async () => {
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    act(() => {
      routingService.emitPremiumUnreachable({ status: 503, sentAt: 1000 })
    })
    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })
    expect(mockedLog).toHaveBeenCalledWith(
      "instance_unreachable",
      expect.anything(),
    )
    const firstCallCount = mockedLog.mock.calls.filter(
      ([name]) => name === "instance_unreachable",
    ).length
    expect(firstCallCount).toBe(1)

    // Second emit — already DEGRADED, so handler must be a noop.
    act(() => {
      routingService.emitPremiumUnreachable({ status: 503, sentAt: 1100 })
    })
    const secondCallCount = mockedLog.mock.calls.filter(
      ([name]) => name === "instance_unreachable",
    ).length
    expect(secondCallCount).toBe(1)
  })

  test("snapshot hydration applies on mount when instance_id matches", async () => {
    localStorage.setItem(
      "premium_unreachable_snapshot",
      JSON.stringify({
        instance_id: "inst-A",
        unreachable_since: 1234,
        failed_probes: 2,
        is_terminal: false,
        updated_at: Date.now(),
      }),
    )
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })
    expect(ctxRef.current?.unreachable.state.unreachableSince).toBe(1234)
    expect(ctxRef.current?.unreachable.state.failedProbes).toBe(2)
  })

  test("snapshot hydration rejected when instance_id differs", async () => {
    localStorage.setItem(
      "premium_unreachable_snapshot",
      JSON.stringify({
        instance_id: "inst-other",
        unreachable_since: 1234,
        failed_probes: 2,
        is_terminal: false,
        updated_at: Date.now(),
      }),
    )
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
  })

  test("snapshot hydration rejected when snapshot is older than the 1h TTL", async () => {
    // The snapshot reader treats any entry older than UNREACHABLE_SNAPSHOT_TTL_MS
    // (1 hour) as missing — a stale peer entry must not hydrate a fresh tab
    // into a degraded state that no longer reflects reality.
    const oneHourMs = 60 * 60 * 1000
    localStorage.setItem(
      "premium_unreachable_snapshot",
      JSON.stringify({
        instance_id: "inst-A",
        unreachable_since: Date.now() - oneHourMs - 60_000,
        failed_probes: 2,
        is_terminal: false,
        updated_at: Date.now() - oneHourMs - 60_000, // 1h + 1min old → expired
      }),
    )
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Give the hydration effect a chance to run.
    await act(async () => {
      await Promise.resolve()
    })

    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
    expect(ctxRef.current?.unreachable.state.failedProbes).toBe(0)
  })

  test("snapshot with malformed JSON is treated as missing", async () => {
    // Defensive: a junk value in localStorage must not crash the reader.
    localStorage.setItem("premium_unreachable_snapshot", "not-json")
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
  })

  test("retryUnreachableProbe clears terminal + arms a probe, next failure counts as probe not flip", async () => {
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Drive into terminal via cross-tab sync (fast path — avoids waiting
    // through five real re-arm timers).
    act(() => {
      fireTabSync("PREMIUM_INSTANCE_UNREACHABLE", {
        instance_id: "inst-A",
        unreachable_since: 5000,
        failed_probes: MAX_FAILED_PROBES,
        is_terminal: true,
      })
    })
    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.isUnreachableTerminal).toBe(true)
    })
    expect(ctxRef.current?.unreachable.state.failedProbes).toBe(
      MAX_FAILED_PROBES,
    )

    // User clicks Retry.
    act(() => {
      ctxRef.current?.unreachable.retryProbe()
    })

    // Terminal flag cleared, probe budget reset, unreachable still set.
    expect(ctxRef.current?.unreachable.state.isUnreachableTerminal).toBe(false)
    expect(ctxRef.current?.unreachable.state.failedProbes).toBe(0)
    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)

    expect(mockedLog).toHaveBeenCalledWith(
      "instance_unreachable_manual_retry",
      expect.objectContaining({ instance_id: "inst-A" }),
    )

    // Premium routing is re-enabled so the next request carries premium
    // headers and can serve as the probe.
    expect(routingService.isPremiumAssigned()).toBe(true)

    // Sanity: a subsequent unreachable event counts as a probe failure
    // (not a fresh HEALTHY→DEGRADED flip). Probe failure increments
    // failedProbes; a fresh flip would leave it at 0.
    mockedLog.mockClear()
    act(() => {
      routingService.emitPremiumUnreachable({ status: 503, sentAt: 99999 })
    })
    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.failedProbes).toBe(1)
    })
    // Probe-failure logs — not a fresh instance_unreachable log.
    expect(mockedLog).toHaveBeenCalledWith(
      "instance_probe_failure",
      expect.anything(),
    )
    expect(mockedLog).not.toHaveBeenCalledWith(
      "instance_unreachable",
      expect.anything(),
    )
  })

  test("retryUnreachableProbe is a noop when not currently unreachable", () => {
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    act(() => {
      ctxRef.current?.unreachable.retryProbe?.()
    })
    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
    expect(mockedLog).not.toHaveBeenCalledWith(
      "instance_unreachable_manual_retry",
      expect.anything(),
    )
  })

  test("heartbeat 503 and unreachable machine coexist — both flags set, neither blocks the other", async () => {
    jest.setTimeout(15000)
    // When the backend returns 503 to a heartbeat from the axios interceptor:
    //  - the interceptor emits emitPremiumUnreachable → the machine flips
    //  - the heartbeat call itself rejects → heartbeatFailing goes true
    //    after HEARTBEAT_MAX_RETRIES. The two states are orthogonal; the same
    //    503 sets both, and they must not stomp on each other.
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Simulate the interceptor emission from a 503 to /heartbeat.
    act(() => {
      routingService.emitPremiumUnreachable({
        url: "/users/me/premium/heartbeat",
        status: 503,
        sentAt: 1000,
      })
    })

    // Simulate the heartbeat call rejecting after 3 retries. We drive this
    // via recordActivity(), which is the public entrypoint that sets
    // heartbeatFailing on final failure. mockSendPremiumHeartbeat is
    // rejected for every call so all 3 retries fail.
    mockSendPremiumHeartbeat.mockReset()
    mockSendPremiumHeartbeat.mockRejectedValue(new Error("Service Unavailable"))

    // Retries wait 1s then 2s (3s total). Real timers, but the recordActivity
    // promise rejects at the end so we don't hang past ~3s.
    await act(async () => {
      try {
        await ctxRef.current?.recordActivity()
      } catch {
        /* expected — final rejection */
      }
    })

    // Both flags set, independently.
    expect(ctxRef.current?.heartbeatFailing).toBe(true)
    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    // The machine is in DEGRADED, not TERMINAL — heartbeat failure alone does
    // not consume the probe budget (that's driven by probe re-arms, not the
    // heartbeat directly).
    expect(ctxRef.current?.unreachable.state.failedProbes).toBe(0)
    expect(ctxRef.current?.unreachable.state.isUnreachableTerminal).toBe(false)
  })

  test("unreachable state persists without premiumReachable (ALB fallback contract)", async () => {
    // ALB fallback contract: when the dedicated instance is down and ALB
    // falls back to the shared backend, shouldEmitPremiumReachable() in
    // axios.ts suppresses the reachable signal (x-served-by-instance
    // mismatch). The unreachable state machine must remain in DEGRADED —
    // no spurious CLEAR dispatch.
    //
    // This integration test verifies the Provider-level invariant: only a
    // genuine emitPremiumReachable (from a response where instance identity
    // actually matched) can transition out of the unreachable state. Fallback
    // 2xx responses that bypass the reachable signal leave the state intact.
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Enter DEGRADED: dedicated instance returned 503.
    act(() => {
      routingService.emitPremiumUnreachable({
        url: "/api/test",
        status: 503,
        sentAt: 1000,
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })
    const since = ctxRef.current?.unreachable.state.unreachableSince

    // Simulate fallback 2xx responses arriving from the shared backend.
    // shouldEmitPremiumReachable() suppresses the reachable signal because
    // x-served-by-instance does not match the expected instance hash.
    // No emitPremiumReachable is called here — that is the fix.
    await act(async () => {
      await Promise.resolve()
    })

    // State must remain DEGRADED — fallback responses did NOT clear it.
    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    expect(ctxRef.current?.unreachable.state.unreachableSince).toBe(since)

    // Only when the dedicated instance actually recovers (instance identity
    // matches again) does the axios interceptor call emitPremiumReachable,
    // clearing the state.
    act(() => {
      routingService.emitPremiumReachable({
        url: "/api/recovered",
        status: 200,
        sentAt: 5000,
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
    })
    expect(ctxRef.current?.unreachable.state.failedProbes).toBe(0)
    expect(ctxRef.current?.unreachable.state.unreachableSince).toBeNull()
  })

  test("terminal unreachable state persists without premiumReachable (ALB fallback)", async () => {
    // Extension of the above: verify that terminal state (MAX_FAILED_PROBES
    // exhausted) is also not spuriously cleared by fallback responses.
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    // Drive straight to terminal via cross-tab sync (fast path).
    act(() => {
      fireTabSync("PREMIUM_INSTANCE_UNREACHABLE", {
        instance_id: "inst-A",
        unreachable_since: 5000,
        failed_probes: MAX_FAILED_PROBES,
        is_terminal: true,
      })
    })

    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.isUnreachableTerminal).toBe(true)
    })

    // Allow async work to flush — no emitPremiumReachable is called
    // (simulating continued ALB fallback with instance-id mismatch).
    await act(async () => {
      await Promise.resolve()
    })

    // Terminal state must persist.
    expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    expect(ctxRef.current?.unreachable.state.isUnreachableTerminal).toBe(true)
    expect(ctxRef.current?.unreachable.state.failedProbes).toBe(
      MAX_FAILED_PROBES,
    )
  })

  test("non-dedicated assignment clears unreachable state via mirror effect", async () => {
    mockedGetStatus.mockResolvedValue(dedicatedStatus)
    const ctxRef = renderProvider()

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.assigned).toBe(true)
    })

    act(() => {
      routingService.emitPremiumUnreachable({ status: 503, sentAt: 1000 })
    })
    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(true)
    })

    // Simulate a backend poll reassigning to shared.
    mockedAssign.mockResolvedValue({
      assigned: true,
      is_shared: true,
      message: "shared",
    })

    // Drive a poll turn by directly re-calling assign() — the state
    // change into a shared assignment activates the mirror effect's
    // cleanup path.
    await act(async () => {
      await ctxRef.current?.assign()
    })

    await waitFor(() => {
      expect(ctxRef.current?.assignmentResult?.is_shared).toBe(true)
    })
    await waitFor(() => {
      expect(ctxRef.current?.unreachable.state.instanceUnreachable).toBe(false)
    })
  })
})
