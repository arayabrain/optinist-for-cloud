/**
 * Leader-gated polling in PremiumAssignmentProvider.
 *
 * Every other provider suite stubs the election to "this tab is the leader", so
 * the non-leader half of the gate has no coverage anywhere. Here the stub is a
 * parameter: the same scenario is run as leader and as follower, and the
 * follower must issue zero requests.
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

// The parameter under test. "mock" prefix required by jest's factory guard.
let mockIsLeader = true

jest.mock("utils/crossTabSync", () => ({
  __esModule: true,
  tabSync: {
    broadcast: () => {},
    broadcastLogout: () => {},
    broadcastPremiumReleased: () => {},
    on:
      (_type: TabSyncMessageType, _handler: (m: TabSyncMessage) => void) =>
      () => {},
    onAny: () => () => {},
    destroy: () => {},
  },
  syncActivityAcrossTabs: () => {},
  getLastActivityFromAnyTab: () => 0,
  onActivityFromOtherTab: () => () => {},
  CrossTabLeaderElection: class {
    constructor(onBecomeLeader: () => void) {
      if (mockIsLeader) setTimeout(onBecomeLeader, 0)
    }
    getIsLeader() {
      return mockIsLeader
    }
    destroy() {}
  },
}))

// require() not import: static imports hoist above the mock vars.
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

/** No dedicated instance yet, which is the state a leader keeps polling in. */
const retryableAssignment: PremiumAssignmentResult = {
  message: "scaling in progress",
  instance_id: "",
  assigned: false,
  is_shared: false,
  assignment_source: "pending",
  scaling_in_progress: true,
  retry_after: 30,
}

const nullAssignmentStatus: PremiumStatusResult = {
  subscription_type: UserTier.PREMIUM,
  is_premium: true,
  assignment: null,
}

const advanceOnePollCycle = async () => {
  await act(async () => {
    jest.advanceTimersByTime(60_000)
    await Promise.resolve()
  })
}

const getMeDispatchCount = () =>
  mockDispatchFn.mock.calls.filter(
    (call) => (call[0] as unknown as { type?: string })?.type === "user/getMe",
  ).length

/** Runs the shared setup and returns the provider once assignment settled. */
const settleWithNoInstance = async () => {
  mockGetPremiumStatus.mockResolvedValue(nullAssignmentStatus)
  mockAssignPremiumInstance.mockResolvedValue(retryableAssignment)

  const ctxRef = renderProvider()

  await waitFor(() => {
    expect(ctxRef.current?.assignmentResult).not.toBeNull()
  })
  mockGetPremiumStatus.mockClear()
  return ctxRef
}

// --- Tests ---

describe("PremiumAssignmentProvider: polling is leader-gated (6232)", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    jest.useFakeTimers()
    mockIsLeader = true
    localStorage.clear()
    sessionStorage.clear()
    routingService.clearRoutingInfo()
    routingService.setPremiumAssigned(false)
  })

  afterEach(() => {
    jest.clearAllTimers()
    jest.useRealTimers()
  })

  test("the leader tab polls /status once per interval", async () => {
    await settleWithNoInstance()

    await advanceOnePollCycle()
    await advanceOnePollCycle()
    await advanceOnePollCycle()

    expect(mockGetPremiumStatus).toHaveBeenCalledTimes(3)
  })

  test("a non-leader tab issues zero polls", async () => {
    mockIsLeader = false

    const ctxRef = await settleWithNoInstance()

    for (let cycle = 0; cycle < 10; cycle++) {
      await advanceOnePollCycle()
    }

    expect(mockGetPremiumStatus).not.toHaveBeenCalled()
    expect(ctxRef.current?.assignmentResult?.assigned).toBe(false)
  })

  test("the leader tab refreshes the subscription every five minutes", async () => {
    await settleWithNoInstance()
    const before = getMeDispatchCount()

    await act(async () => {
      jest.advanceTimersByTime(5 * 60 * 1000)
      await Promise.resolve()
    })

    expect(getMeDispatchCount()).toBe(before + 1)
  })

  test("a non-leader tab runs no subscription refresh", async () => {
    mockIsLeader = false

    await settleWithNoInstance()
    const before = getMeDispatchCount()

    await act(async () => {
      jest.advanceTimersByTime(5 * 60 * 1000 * 3)
      await Promise.resolve()
    })

    expect(getMeDispatchCount()).toBe(before)
  })
})
