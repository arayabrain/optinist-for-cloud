/**
 * Premium telemetry routing (case 6238).
 *
 * When the premium instance becomes unreachable, the UI logs telemetry via
 * logPremiumUiEvent. That POST must reach the backend through the FREE tier —
 * carrying no premium routing headers — otherwise it would be routed to the
 * dead premium instance and lost.
 *
 * Unlike the mocked interceptor suite, these tests drive the REAL routingService
 * singleton AND the REAL axios interceptor. Nothing stubs getRoutingHeaders():
 * the emptiness (or presence) of the outgoing routing headers is produced by the
 * production teardown path reacting to a real 502/503, then read back off the
 * actually-captured /users/me/premium/ui-event request. A test that mocked
 * getRoutingHeaders would be asserting its own premise.
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
// utils/routing/RoutingService is intentionally NOT mocked — these tests exist
// to exercise the real singleton so premium routing state actually transitions.

const UI_EVENT_URL = "/users/me/premium/ui-event"

type RecordedRequest = {
  url?: string
  headers: Record<string, unknown>
  config: Record<string, unknown>
}

type AdapterResponse = {
  status: number
  data?: unknown
  headers?: Record<string, string>
}

// A response, or a factory that returns a different response per call so a URL
// can yield 503 first and 200 on the free-tier retry.
type Responder = AdapterResponse | (() => AdapterResponse)

let responses: Map<string, Responder>
let recorded: RecordedRequest[]

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const installAdapter = (instance: any): void => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  instance.defaults.adapter = async (config: any) => {
    recorded.push({
      url: config.url,
      headers: { ...(config.headers ?? {}) },
      config: { ...config },
    })

    const responder = responses.get(config.url ?? "")
    if (!responder) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const err: any = new Error(`no responder for ${config.url}`)
      err.config = config
      throw err
    }
    const resolved = typeof responder === "function" ? responder() : responder

    if (resolved.status >= 400) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const err: any = new Error(`HTTP ${resolved.status}`)
      err.isAxiosError = true
      err.response = {
        status: resolved.status,
        data: resolved.data ?? {},
        headers: resolved.headers ?? {},
        config,
      }
      err.config = config
      throw err
    }

    return {
      data: resolved.data ?? {},
      status: resolved.status,
      statusText: "OK",
      headers: resolved.headers ?? {},
      config,
      request: {},
    }
  }
}

describe("premium telemetry routing (6238)", () => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let axiosInstance: any
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let routingService: any
  let logPremiumUiEvent: (
    eventType: string,
    details?: Record<string, unknown>,
  ) => Promise<void>

  beforeEach(() => {
    jest.resetModules()
    jest.clearAllMocks()
    localStorage.clear()
    responses = new Map()
    recorded = []

    // Require after resetModules so the interceptor, the API module, and this
    // test all share the same freshly-constructed axios instance + singleton.
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    axiosInstance = require("utils/axios").default
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    routingService = require("utils/routing/RoutingService").routingService
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const premiumApi = require("api/premium/PremiumAssignmentApi")
    logPremiumUiEvent = premiumApi.logPremiumUiEvent

    routingService.clearRoutingInfo()
    installAdapter(axiosInstance)

    responses.set(UI_EVENT_URL, { status: 200, data: {} })
  })

  afterEach(() => {
    routingService.clearRoutingInfo()
  })

  // Seed the routing token the way production does: a routed 200 carrying an
  // X-Routing-ID flows through the real response interceptor while premiumAssigned
  // is still false (the initial-seed condition).
  const seedToken = async (token: string) => {
    responses.set("/seed", { status: 200, headers: { "x-routing-id": token } })
    await axiosInstance.get("/seed")
  }

  const findUiEvent = () => recorded.find((r) => r.url === UI_EVENT_URL)

  it("routes instance_unreachable telemetry via free tier after a dedicated 502 tears premium routing down", async () => {
    // Live dedicated routed state: token seeded, premium assigned, not shared.
    await seedToken("token-A")
    routingService.setPremiumAssigned(true)
    routingService.setPremiumShared(false)
    expect(routingService.getRoutingHeaders()[RoutingHeaders.ROUTING_ID]).toBe(
      "token-A",
    )

    // Mirror the production wiring: the unreachable listener (the state machine
    // in the app) logs telemetry the instant premium routing is torn down.
    let uiEventDone: Promise<void> | undefined
    const unsub = routingService.onPremiumUnreachable(
      (detail: { url?: string; status?: number }) => {
        uiEventDone = logPremiumUiEvent("instance_unreachable", {
          url: detail.url ?? null,
          status: detail.status ?? null,
        })
      },
    )

    // Drive the real unreachable transition: a 502 through the interceptor, then
    // a free-tier retry that succeeds.
    let calls = 0
    responses.set("/premium/call", () => {
      calls += 1
      return calls === 1
        ? { status: 502, data: { detail: "premium down" } }
        : { status: 200, data: { ok: true }, headers: {} }
    })

    const res = await axiosInstance.get("/premium/call")
    await uiEventDone
    unsub()

    // Request still resolves via free-tier fallback.
    expect(res.status).toBe(200)
    // Dedicated 502 tore premium routing down.
    expect(routingService.isPremiumAssigned()).toBe(false)

    // The telemetry POST fired and carried NO premium routing headers, so it
    // reaches the backend via free tier rather than the dead premium instance.
    const uiEvent = findUiEvent()
    expect(uiEvent).toBeDefined()
    expect(uiEvent!.headers[RoutingHeaders.ROUTING_ID]).toBeUndefined()
    expect(uiEvent!.headers[RoutingHeaders.USER_TIER]).toBeUndefined()
  })

  it("keeps instance_reachable telemetry on premium routing when the instance is reachable", async () => {
    // Live dedicated routed state with a known instance hash.
    await seedToken("token-A")
    routingService.setPremiumAssigned(true)
    routingService.setPremiumShared(false)
    routingService.setPremiumInstanceId("hash-A")

    // Mirror the production wiring: the reachable listener logs telemetry when a
    // response confirms the assigned instance served it.
    let uiEventDone: Promise<void> | undefined
    const unsub = routingService.onPremiumReachable(() => {
      uiEventDone = logPremiumUiEvent("instance_reachable", {})
    })

    // A healthy 200 from the assigned instance (routing-id unchanged, served-by
    // matches) drives the real reachable emit.
    responses.set("/premium/call", {
      status: 200,
      data: { ok: true },
      headers: {
        "x-routing-id": "token-A",
        "x-served-by-instance": "hash-A",
      },
    })

    await axiosInstance.get("/premium/call")
    await uiEventDone
    unsub()

    expect(routingService.isPremiumAssigned()).toBe(true)

    // The telemetry POST carried X-Routing-ID — the header the ALB routes on — so
    // it lands on the user's premium instance. Same code, opposite outcome from
    // the unreachable case: proof the routing is driven by real state, not a
    // hardcoded endpoint.
    const uiEvent = findUiEvent()
    expect(uiEvent).toBeDefined()
    expect(uiEvent!.headers[RoutingHeaders.ROUTING_ID]).toBe("token-A")
  })

  it("does NOT tear down or emit unreachable on a shared 502 — premium routing persists", async () => {
    // Shared (pool) assignment: a single 502 is a transient blip with no
    // dedicated-only recovery, so routing is intentionally left armed.
    await seedToken("token-A")
    routingService.setPremiumAssigned(true)
    routingService.setPremiumShared(true)

    let fired = false
    const unsub = routingService.onPremiumUnreachable(() => {
      fired = true
      logPremiumUiEvent("instance_unreachable", {})
    })

    let calls = 0
    responses.set("/premium/call", () => {
      calls += 1
      return calls === 1
        ? { status: 502, data: { detail: "shared blip" } }
        : { status: 200, data: { ok: true }, headers: {} }
    })

    const res = await axiosInstance.get("/premium/call")
    unsub()

    expect(res.status).toBe(200)
    // Shared routing is never torn down, and no unreachable telemetry is emitted.
    expect(fired).toBe(false)
    expect(findUiEvent()).toBeUndefined()
    expect(routingService.isPremiumAssigned()).toBe(true)
    expect(routingService.getRoutingHeaders()[RoutingHeaders.ROUTING_ID]).toBe(
      "token-A",
    )
  })

  it("does NOT tear down or emit unreachable on a 502 during the dedicated warm-up grace", async () => {
    // A freshly-assigned dedicated instance is expected to flap while it
    // registers in the ALB target group. setPremiumInstanceId arms the warm-up
    // window, which suppresses teardown for a transient 5xx.
    await seedToken("token-A")
    routingService.setPremiumAssigned(true)
    routingService.setPremiumShared(false)
    routingService.setPremiumInstanceId("hash-A")
    expect(routingService.isWithinPremiumWarmup()).toBe(true)

    let fired = false
    const unsub = routingService.onPremiumUnreachable(() => {
      fired = true
      logPremiumUiEvent("instance_unreachable", {})
    })

    let calls = 0
    responses.set("/premium/call", () => {
      calls += 1
      return calls === 1
        ? { status: 503, data: { detail: "warming up" } }
        : { status: 200, data: { ok: true }, headers: {} }
    })

    const res = await axiosInstance.get("/premium/call")
    unsub()

    expect(res.status).toBe(200)
    // Warm-up suppresses teardown; no unreachable telemetry is emitted.
    expect(fired).toBe(false)
    expect(findUiEvent()).toBeUndefined()
    expect(routingService.isPremiumAssigned()).toBe(true)
  })
})
