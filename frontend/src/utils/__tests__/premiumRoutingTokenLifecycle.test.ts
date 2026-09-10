/**
 * Premium routing token: seed / clear / re-seed across a full session.
 *
 * Other suites cover the pieces in isolation — some mock routingService and
 * assert the interceptor CALLS updateRoutingToken; another drives the real
 * service but seeds the token by hand. This test drives the REAL axios
 * response interceptor AND the REAL routingService singleton through
 * login -> release -> reassign -> logout, so the token is seeded only by
 * production code. A regression in the interceptor's seed path, or in
 * resetForRelease's teardown, fails here.
 */

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  jest,
} from "@jest/globals"

import { RoutingHeaders } from "const/Subscription"

const mockRefreshTokenApi = jest.fn()
const mockGetToken = jest.fn(() => "access-token" as string | null)
const mockGetExToken = jest.fn(() => null as string | null)
const mockLogout = jest.fn()
const mockSaveToken = jest.fn()
const mockIsDataviewPublicOutputsRequest = jest.fn(() => false)

jest.mock("api/auth/Auth", () => ({ refreshTokenApi: mockRefreshTokenApi }))
jest.mock("utils/auth/AuthUtils", () => ({
  getToken: mockGetToken,
  getExToken: mockGetExToken,
  logout: mockLogout,
  saveToken: mockSaveToken,
}))
jest.mock("utils/DataviewUtils", () => ({
  isDataviewPublicOutputsRequest: mockIsDataviewPublicOutputsRequest,
  DATAVIEW_PUBLIC_REQUEST_KEY: "x-dataview-public",
}))
// utils/routing/RoutingService is intentionally NOT mocked — the whole point is
// to exercise the real singleton so the token state actually transitions.

type AdapterResponse = {
  status: number
  data?: unknown
  headers?: Record<string, string>
}
let responses: Map<string, AdapterResponse>

// Return canned responses (with headers) for the SUT axios instance without
// touching the network, so the real interceptors run against them.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const installAdapter = (instance: any): void => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  instance.defaults.adapter = async (config: any) => {
    const r = responses.get(config.url ?? "")
    if (!r) {
      throw Object.assign(new Error(`no responder for ${config.url}`), {
        config,
      })
    }
    return {
      data: r.data ?? {},
      status: r.status,
      statusText: "OK",
      headers: r.headers ?? {},
      config,
      request: {},
    }
  }
}

describe("premium routing token — seed / clear / re-seed across the lifecycle", () => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let axiosInstance: any
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let routingService: any

  beforeEach(() => {
    jest.resetModules()
    jest.clearAllMocks()
    localStorage.clear()
    responses = new Map()
    // Require both after resetModules so the interceptor and this test share
    // the same freshly-constructed singleton.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    axiosInstance = require("utils/axios").default
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    routingService = require("utils/routing/RoutingService").routingService
    routingService.clearRoutingInfo()
    installAdapter(axiosInstance)
  })

  afterEach(() => {
    routingService.clearRoutingInfo()
  })

  // The unrecoverable pair: premium routing claimed with no token to route by.
  const expectNoDeadlock = () =>
    expect(
      routingService.isPremiumAssigned() &&
        routingService.getRoutingToken() === null,
    ).toBe(false)

  const respondWithRoutingId = async (url: string, token: string) => {
    responses.set(url, { status: 200, headers: { "x-routing-id": token } })
    await axiosInstance.get(url)
  }

  it("seeds on assign, clears on release, re-seeds on reassign, clears on logout", async () => {
    const STATUS = "/users/me/premium/status"

    // Assign: the first routed status response seeds the token through the real
    // interceptor (premiumAssigned still false — the initial-seed condition),
    // then the context adopts the assignment.
    await respondWithRoutingId(STATUS, "token-A")
    expect(routingService.getRoutingToken()).toBe("token-A")

    routingService.setPremiumAssigned(true)
    routingService.setPremiumInstanceId("hash-A")
    expect(routingService.getRoutingHeaders()[RoutingHeaders.ROUTING_ID]).toBe(
      "token-A",
    )
    expectNoDeadlock()

    // Release: resetForRelease clears the flag and the token together, so no
    // headers are emitted.
    routingService.resetForRelease()
    expect(routingService.getRoutingToken()).toBeNull()
    expect(routingService.isPremiumAssigned()).toBe(false)
    expect(routingService.getRoutingHeaders()).toEqual({})
    expectNoDeadlock()

    // Reassign: the flag flips true while the token is still null (the exact
    // deadlock-prone pair). The next routed response re-seeds it via the
    // interceptor's null-token recovery path, not a manual write.
    routingService.setPremiumAssigned(true)
    routingService.setPremiumInstanceId("hash-B")
    expect(routingService.isPremiumAssigned()).toBe(true)
    expect(routingService.getRoutingToken()).toBeNull()

    await respondWithRoutingId(STATUS, "token-B")
    expect(routingService.getRoutingToken()).toBe("token-B")
    expect(routingService.getRoutingHeaders()[RoutingHeaders.ROUTING_ID]).toBe(
      "token-B",
    )
    expectNoDeadlock()

    // Logout: full teardown clears everything.
    routingService.clearRoutingInfo()
    expect(routingService.getRoutingToken()).toBeNull()
    expect(routingService.isPremiumAssigned()).toBe(false)
    expect(routingService.getRoutingHeaders()).toEqual({})
    expectNoDeadlock()
  })

  it("a free-tier response does not overwrite a live token while assigned", async () => {
    // Once assigned with a token, a 200 from the free/public tier (no premium
    // headers sent) must not clobber the routing token, checked against real
    // service state.
    await respondWithRoutingId("/users/me/premium/status", "token-A")
    routingService.setPremiumAssigned(true)
    routingService.setPremiumInstanceId("hash-A")
    expect(routingService.getRoutingToken()).toBe("token-A")

    // A later free-tier 200 carrying a different routing-id must be ignored:
    // premiumAssigned is true, the token is present, and the instance is
    // unverified (no x-served-by-instance match).
    responses.set("/free", {
      status: 200,
      headers: { "x-routing-id": "token-from-free-tier" },
    })
    await axiosInstance.get("/free")

    expect(routingService.getRoutingToken()).toBe("token-A")
    expectNoDeadlock()
  })
})
