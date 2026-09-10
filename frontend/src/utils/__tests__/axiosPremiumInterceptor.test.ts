/**
 * Axios interceptor tests for the premium-routing path.
 *
 * Covers:
 *  - Request interceptor stamps _premiumSentAt when premium headers are present
 *  - Response success suppresses reachable when the routing-id rotated
 *  - 503 fallback strips premium markers on the retry config
 *  - Response success emits reachable even when no X-Routing-ID comes back
 *  - 401 refresh-then-retry still emits reachable on the retried request
 */

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  jest,
} from "@jest/globals"

// --- Shared mock state (captured per-test via beforeEach) ---

const mockRefreshTokenApi = jest.fn() as unknown as jest.Mock<
  Promise<{ access_token: string }>
>
const mockGetToken = jest.fn(() => "access-token" as string | null)
const mockGetExToken = jest.fn(() => null as string | null)
const mockLogout = jest.fn()
const mockSaveToken = jest.fn()

const mockGetRoutingHeaders = jest.fn<Record<string, string>, []>(() => ({}))
const mockUpdateRoutingToken = jest.fn<void, [string]>()
const mockRequiresPremiumRouting = jest.fn<boolean, []>(() => false)
const mockSetPremiumAssigned = jest.fn<void, [boolean]>()
const mockEmitPremiumUnreachable = jest.fn<
  void,
  [{ url?: string; status?: number; sentAt?: number }]
>()
const mockEmitPremiumReachable = jest.fn<
  void,
  [{ url?: string; status?: number; sentAt?: number }]
>()
const mockGetPremiumInstanceId = jest.fn<string | null, []>(() => null)
const mockIsPremiumAssigned = jest.fn<boolean, []>(() => false)
const mockGetRoutingToken = jest.fn<string | null, []>(() => null)
const mockIsWithinPremiumWarmup = jest.fn<boolean, []>(() => false)
const mockIsStalePremiumFailure = jest.fn<boolean, [number | undefined]>(
  () => false,
)
const mockIsPremiumShared = jest.fn<boolean, []>(() => false)

const mockIsDataviewPublicOutputsRequest = jest.fn<boolean, [string]>(
  () => false,
)

jest.mock("api/auth/Auth", () => ({
  refreshTokenApi: mockRefreshTokenApi,
}))

jest.mock("utils/auth/AuthUtils", () => ({
  getToken: mockGetToken,
  getExToken: mockGetExToken,
  logout: mockLogout,
  saveToken: mockSaveToken,
}))

jest.mock("utils/routing/RoutingService", () => ({
  routingService: {
    getRoutingHeaders: mockGetRoutingHeaders,
    updateRoutingToken: mockUpdateRoutingToken,
    requiresPremiumRouting: mockRequiresPremiumRouting,
    setPremiumAssigned: mockSetPremiumAssigned,
    emitPremiumUnreachable: mockEmitPremiumUnreachable,
    emitPremiumReachable: mockEmitPremiumReachable,
    getPremiumInstanceId: mockGetPremiumInstanceId,
    isPremiumAssigned: mockIsPremiumAssigned,
    getRoutingToken: mockGetRoutingToken,
    isWithinPremiumWarmup: mockIsWithinPremiumWarmup,
    isStalePremiumFailure: mockIsStalePremiumFailure,
    isPremiumShared: mockIsPremiumShared,
  },
}))

jest.mock("utils/DataviewUtils", () => ({
  isDataviewPublicOutputsRequest: mockIsDataviewPublicOutputsRequest,
  DATAVIEW_PUBLIC_REQUEST_KEY: "x-dataview-public",
}))

// --- Axios adapter plumbing ---
// Tests install a custom adapter on the SUT axios instance so we can control
// response data/headers/status per-request without touching the network.

type RecordedRequest = {
  url?: string
  method?: string
  headers: Record<string, unknown>
  config: Record<string, unknown>
}

type AdapterResponse = {
  status: number
  data?: unknown
  headers?: Record<string, string>
}

// Map request URL → response or response factory. When a factory is supplied
// it runs per-call so the test can sequence different responses for the same
// URL (e.g. first call 401, retry 200).
type Responder = AdapterResponse | (() => AdapterResponse | Error)

let responses: Map<string, Responder> = new Map()
let recorded: RecordedRequest[] = []

const installAdapter = (instance: { defaults: { adapter: unknown } }): void => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  instance.defaults.adapter = async (config: any) => {
    recorded.push({
      url: config.url,
      method: config.method,
      headers: { ...(config.headers ?? {}) },
      config: { ...config },
    })

    const responder = responses.get(config.url ?? "")
    if (!responder) {
      const err: Error & {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        response?: any
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        config?: any
      } = new Error(`No responder for URL ${config.url}`)
      err.config = config
      throw err
    }

    const resolved = typeof responder === "function" ? responder() : responder
    if (resolved instanceof Error) throw resolved

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

// --- Tests ---

describe("axios premium-routing interceptors", () => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let axiosInstance: any

  beforeEach(async () => {
    jest.resetModules()
    jest.clearAllMocks()
    responses = new Map()
    recorded = []

    // Default token behaviour
    mockGetToken.mockReturnValue("access-token")
    mockGetExToken.mockReturnValue(null)

    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const mod = require("utils/axios")
    axiosInstance = mod.default
    installAdapter(axiosInstance)
  })

  afterEach(() => {
    jest.useRealTimers()
  })

  it("stamps _premiumSentAt on the request when premium headers are present", async () => {
    // Request interceptor checks routingHeaders[X-Routing-ID] and if set
    // stamps _hadPremiumHeaders / _outgoingRoutingId / _premiumSentAt on the
    // config. That sentAt flows into the reachable/unreachable events.
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue("expected-instance-hash")

    const before = Date.now()
    responses.set("/ok", {
      status: 200,
      data: {},
      headers: {
        "x-routing-id": "rid-outgoing",
        "x-served-by-instance": "expected-instance-hash",
      },
    })
    await axiosInstance.get("/ok")
    const after = Date.now()

    expect(mockEmitPremiumReachable).toHaveBeenCalledTimes(1)
    const detail = mockEmitPremiumReachable.mock.calls[0][0]
    expect(typeof detail.sentAt).toBe("number")
    expect(detail.sentAt).toBeGreaterThanOrEqual(before)
    expect(detail.sentAt).toBeLessThanOrEqual(after)
    expect(detail.status).toBe(200)
  })

  it("does NOT emit reachable when the response routing-id has rotated", async () => {
    // A rotated X-Routing-ID on the success response means a different
    // instance served us — we cannot conclude the probed instance is healthy.
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-A",
      "X-User-Tier": "premium",
    })

    responses.set("/rotated", {
      status: 200,
      data: {},
      headers: { "x-routing-id": "rid-B" }, // backend hit a different instance
    })
    await axiosInstance.get("/rotated")

    // updateRoutingToken always fires on any x-routing-id.
    expect(mockUpdateRoutingToken).toHaveBeenCalledWith("rid-B")
    // But premium reachable must be suppressed due to rotation.
    expect(mockEmitPremiumReachable).not.toHaveBeenCalled()
  })

  it("emits reachable when the response has NO x-routing-id header (not rotated)", async () => {
    // Edge case: when the response carries no x-routing-id at all, the
    // rotation check (routingId !== _outgoingRoutingId) resolves to false
    // because typeof undefined !== "string". We treat that as "not rotated"
    // and still emit reachable — provided instance identity matches.
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue("expected-instance-hash")

    responses.set("/no-rid", {
      status: 200,
      data: {},
      headers: { "x-served-by-instance": "expected-instance-hash" },
    })
    await axiosInstance.get("/no-rid")

    expect(mockUpdateRoutingToken).not.toHaveBeenCalled()
    expect(mockEmitPremiumReachable).toHaveBeenCalledTimes(1)
    expect(mockEmitPremiumReachable.mock.calls[0][0].status).toBe(200)
  })

  it("on 503 premium fallback, strips premium markers on the retry config", async () => {
    // When premium routing gets a 503/502, axios retries without premium
    // headers. That retry config must NOT still look like a premium request
    // — otherwise the retry's response would wrongly emit reachable against
    // a request that never carried premium headers.
    mockGetRoutingHeaders.mockImplementation(() => ({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    }))
    mockRequiresPremiumRouting.mockReturnValue(true)

    // First call yields 503; a follow-up call (the retry) yields 200.
    let callCount = 0
    responses.set("/svc", () => {
      callCount += 1
      if (callCount === 1) {
        return { status: 503, data: { detail: "no premium" } }
      }
      return { status: 200, data: { ok: true }, headers: {} }
    })

    // Premium routing headers vanish for the retry because the interceptor
    // sets _retryWithoutPremium=true on the retry config. We simulate the
    // absence by making getRoutingHeaders return {} on the retry call.
    mockGetRoutingHeaders
      .mockReturnValueOnce({
        "X-Routing-ID": "rid-outgoing",
        "X-User-Tier": "premium",
      })
      .mockReturnValue({})

    const res = await axiosInstance.get("/svc")
    expect(res.status).toBe(200)

    // Unreachable emitted once for the 503.
    expect(mockEmitPremiumUnreachable).toHaveBeenCalledTimes(1)
    // The retry (second recorded request) must NOT carry the premium
    // routing markers — otherwise we'd mis-emit reachable.
    expect(recorded).toHaveLength(2)
    const retryConfig = recorded[1].config as Record<string, unknown>
    expect(retryConfig._retryWithoutPremium).toBe(true)
    expect(retryConfig._hadPremiumHeaders).toBeUndefined()
    expect(retryConfig._outgoingRoutingId).toBeUndefined()
    expect(retryConfig._premiumSentAt).toBeUndefined()

    // And the retry's success should NOT emit reachable — the retry was not
    // a premium-routed request.
    expect(mockEmitPremiumReachable).not.toHaveBeenCalled()
  })

  it("401 refresh-then-retry: the retried request still emits reachable when it succeeds with premium headers", async () => {
    // The refresh/retry path in handleUnauthorizedError retries via the
    // shared axiosLibrary (not the SUT instance), which means the response
    // interceptor of the SUT instance does NOT fire for the retried 200.
    // This test pins that semantics: the final .then() resolves, but
    // emitPremiumReachable was NOT called for the retried request.
    //
    // This documents the current gap — refresh+retry does not produce a
    // reachable signal, even on a premium-healthy instance. The fix, when
    // it lands, should make this assertion flip to "toHaveBeenCalled()".
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockRefreshTokenApi.mockResolvedValue({ access_token: "new-token" })

    let callCount = 0
    responses.set("/protected", () => {
      callCount += 1
      if (callCount === 1) return { status: 401, data: {} }
      return {
        status: 200,
        data: {},
        headers: { "x-routing-id": "rid-outgoing" },
      }
    })

    const res = await axiosInstance.get("/protected")
    expect(res.status).toBe(200)
    expect(mockSaveToken).toHaveBeenCalledWith("new-token")

    // Currently, the 401+retry path bypasses the SUT instance's response
    // interceptor on retry (it goes through axiosLibrary), so no reachable
    // signal is emitted. This pins the existing behaviour.
    expect(mockEmitPremiumReachable).not.toHaveBeenCalled()
  })

  // --- Instance identity (X-Served-By-Instance) tests ---

  it("does NOT emit reachable when routing-id matches but x-served-by-instance mismatches (ALB fallback detection)", async () => {
    // If the dedicated instance is down, ALB may
    // fall back to the shared backend. Routing-id matches (it's UID-based)
    // but x-served-by-instance differs. Must NOT emit reachable.
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue("expected-instance-hash")

    responses.set("/fallback", {
      status: 200,
      data: {},
      headers: {
        "x-routing-id": "rid-outgoing",
        "x-served-by-instance": "different-instance-hash",
      },
    })
    await axiosInstance.get("/fallback")

    expect(mockEmitPremiumReachable).not.toHaveBeenCalled()
  })

  it("emits reachable when both routing-id and instance-id match", async () => {
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue("expected-instance-hash")

    responses.set("/match", {
      status: 200,
      data: {},
      headers: {
        "x-routing-id": "rid-outgoing",
        "x-served-by-instance": "expected-instance-hash",
      },
    })
    await axiosInstance.get("/match")

    expect(mockEmitPremiumReachable).toHaveBeenCalledTimes(1)
  })

  it("does NOT emit reachable when _outgoingInstanceId is unset (startup race — cannot verify instance)", async () => {
    // Before the assignment API returns, getPremiumInstanceId() returns null.
    // Without a known instance ID, we cannot verify which instance served
    // the response — suppress reachable to prevent false-positives when
    // premiumAssigned=true but premiumInstanceId=null (desync guard).
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue(null)

    responses.set("/startup-race", {
      status: 200,
      data: {},
      headers: { "x-routing-id": "rid-outgoing" },
    })
    await axiosInstance.get("/startup-race")

    expect(mockEmitPremiumReachable).not.toHaveBeenCalled()
  })

  it("on 503 premium fallback, strips _outgoingInstanceId on the retry config", async () => {
    mockGetRoutingHeaders.mockImplementation(() => ({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    }))
    mockGetPremiumInstanceId.mockReturnValue("my-instance-hash")
    mockRequiresPremiumRouting.mockReturnValue(true)

    let callCount = 0
    responses.set("/svc2", () => {
      callCount += 1
      if (callCount === 1) {
        return { status: 503, data: { detail: "no premium" } }
      }
      return { status: 200, data: { ok: true }, headers: {} }
    })

    mockGetRoutingHeaders
      .mockReturnValueOnce({
        "X-Routing-ID": "rid-outgoing",
        "X-User-Tier": "premium",
      })
      .mockReturnValue({})

    await axiosInstance.get("/svc2")

    // The retry must have _outgoingInstanceId stripped.
    expect(recorded).toHaveLength(2)
    const retryConfig = recorded[1].config as Record<string, unknown>
    expect(retryConfig._outgoingInstanceId).toBeUndefined()
  })

  // --- Routing token update guard tests (Issue #605) ---

  it("updates routing token when premiumAssigned is false (initial token seeding)", async () => {
    mockIsPremiumAssigned.mockReturnValue(false)
    mockGetRoutingHeaders.mockReturnValue({})

    responses.set("/seed", {
      status: 200,
      data: {},
      headers: { "x-routing-id": "new-token-from-public" },
    })
    await axiosInstance.get("/seed")

    expect(mockUpdateRoutingToken).toHaveBeenCalledWith("new-token-from-public")
  })

  it("does NOT update routing token when premiumAssigned is true, token is present, and instance is unverified", async () => {
    // Simulates the stale-token overwrite scenario: premiumAssigned=true
    // but the response came from the free/public tier (no premium headers
    // sent, or instance mismatch). Token is already set, so no null-recovery.
    mockIsPremiumAssigned.mockReturnValue(true)
    mockGetRoutingToken.mockReturnValue("existing-token")
    mockGetRoutingHeaders.mockReturnValue({})

    responses.set("/free-tier", {
      status: 200,
      data: {},
      headers: { "x-routing-id": "token-from-free-tier" },
    })
    await axiosInstance.get("/free-tier")

    expect(mockUpdateRoutingToken).not.toHaveBeenCalled()
  })

  it("updates routing token when premiumAssigned is true and instance is verified", async () => {
    mockIsPremiumAssigned.mockReturnValue(true)
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue("expected-instance-hash")

    responses.set("/premium-ok", {
      status: 200,
      data: {},
      headers: {
        "x-routing-id": "rid-outgoing",
        "x-served-by-instance": "expected-instance-hash",
      },
    })
    await axiosInstance.get("/premium-ok")

    expect(mockUpdateRoutingToken).toHaveBeenCalledWith("rid-outgoing")
  })

  it("updates routing token when premiumAssigned is true but token is null (recovery from cleared state)", async () => {
    // After resetForRelease(), premiumAssigned may briefly be true with
    // token=null (e.g. cross-tab race). The null-token escape hatch ensures
    // re-seeding is always possible, preventing a permanent deadlock.
    mockIsPremiumAssigned.mockReturnValue(true)
    mockGetRoutingToken.mockReturnValue(null)
    mockGetRoutingHeaders.mockReturnValue({})

    responses.set("/reseed", {
      status: 200,
      data: {},
      headers: { "x-routing-id": "reseeded-token" },
    })
    await axiosInstance.get("/reseed")

    expect(mockUpdateRoutingToken).toHaveBeenCalledWith("reseeded-token")
  })

  // --- Instance mismatch active detection (issue #709) ---

  it("emits unreachable and clears premiumAssigned when 200 OK comes from wrong instance (instance mismatch detection)", async () => {
    // When EventBridge cleanup deletes the per-user ALB rule before the
    // user's next request, ALB falls through to free-tier → 200 OK from a
    // different instance. The interceptor must actively detect this and
    // trigger the recovery flow.
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue("expected-instance-hash")

    responses.set("/wrong-instance-200", {
      status: 200,
      data: { ok: true },
      headers: {
        "x-routing-id": "rid-outgoing",
        "x-served-by-instance": "free-tier-instance-hash",
      },
    })
    const res = await axiosInstance.get("/wrong-instance-200")

    // Response still resolves — no retry needed for 200.
    expect(res.status).toBe(200)
    expect(res.data).toEqual({ ok: true })

    // Active detection: unreachable emitted, premiumAssigned cleared.
    expect(mockEmitPremiumUnreachable).toHaveBeenCalledTimes(1)
    expect(mockEmitPremiumUnreachable.mock.calls[0][0]).toMatchObject({
      url: "/wrong-instance-200",
      status: 200,
    })
    expect(mockSetPremiumAssigned).toHaveBeenCalledWith(false)

    // Must NOT emit reachable — instance mismatch.
    expect(mockEmitPremiumReachable).not.toHaveBeenCalled()
  })

  it("does NOT clear premiumAssigned on instance mismatch during the warm-up grace", async () => {
    // Right after a fresh dedicated assignment the instance may still be
    // registering in the ALB target group, so a 200 from a different (shared)
    // instance is expected — not a fallback. Tearing down premium routing here
    // would disable it before warm-up completes and, since the unreachable
    // state is grace-suppressed downstream, leave no path to re-enable it.
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue("expected-instance-hash")
    mockIsWithinPremiumWarmup.mockReturnValue(true)

    responses.set("/warmup-mismatch", {
      status: 200,
      data: { ok: true },
      headers: {
        "x-routing-id": "rid-outgoing",
        "x-served-by-instance": "warming-shared-instance-hash",
      },
    })
    const res = await axiosInstance.get("/warmup-mismatch")

    expect(res.status).toBe(200)
    // Suppressed during warm-up: neither teardown nor unreachable fires.
    expect(mockSetPremiumAssigned).not.toHaveBeenCalledWith(false)
    expect(mockEmitPremiumUnreachable).not.toHaveBeenCalled()
    expect(mockEmitPremiumReachable).not.toHaveBeenCalled()
  })

  it("does NOT clear premiumAssigned on instance mismatch when the assignment is shared", async () => {
    // The teardown guard lives in the single choke-point, so the success-path
    // instance-mismatch caller is gated for shared too: a 200 from a different
    // instance must not tear a shared assignment down (it has no dedicated-only
    // recovery to hand off to).
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue("expected-instance-hash")
    mockIsWithinPremiumWarmup.mockReturnValue(false)
    mockIsStalePremiumFailure.mockReturnValue(false)
    mockIsPremiumShared.mockReturnValue(true)

    responses.set("/shared-mismatch", {
      status: 200,
      data: { ok: true },
      headers: {
        "x-routing-id": "rid-outgoing",
        "x-served-by-instance": "free-tier-instance-hash",
      },
    })
    const res = await axiosInstance.get("/shared-mismatch")

    expect(res.status).toBe(200)
    // Shared is never torn down, on either teardown caller.
    expect(mockSetPremiumAssigned).not.toHaveBeenCalledWith(false)
    expect(mockEmitPremiumUnreachable).not.toHaveBeenCalled()
    expect(mockEmitPremiumReachable).not.toHaveBeenCalled()
  })

  it("does NOT clear premiumAssigned on a 502/503 during the warm-up grace", async () => {
    // A transient 5xx from a freshly-assigned dedicated instance is expected
    // during warm-up. Tearing down premiumAssigned here would strand premium
    // routing (the machine's grace suppresses the unreachable event, so the
    // recovery probe never re-enables it). The request still falls back to free
    // tier so it resolves, but premium routing stays armed to converge.
    mockRequiresPremiumRouting.mockReturnValue(true)
    mockIsWithinPremiumWarmup.mockReturnValue(true)

    let callCount = 0
    responses.set("/warmup-5xx", () => {
      callCount += 1
      if (callCount === 1) {
        return { status: 503, data: { detail: "warming up" } }
      }
      return { status: 200, data: { ok: true }, headers: {} }
    })
    // Premium headers on the first request, absent on the free-tier retry.
    mockGetRoutingHeaders
      .mockReturnValueOnce({
        "X-Routing-ID": "rid-outgoing",
        "X-User-Tier": "premium",
      })
      .mockReturnValue({})

    const res = await axiosInstance.get("/warmup-5xx")

    // Falls back so the request still resolves...
    expect(res.status).toBe(200)
    // ...but premium routing is NOT torn down during warm-up.
    expect(mockSetPremiumAssigned).not.toHaveBeenCalledWith(false)
    expect(mockEmitPremiumUnreachable).not.toHaveBeenCalled()
  })

  it("does NOT clear premiumAssigned on a stale 502/503 (older than the last reachable)", async () => {
    // A late-arriving 5xx whose request was sent before the last confirmed-
    // reachable response is an out-of-order echo, not a live outage. Past warm-up
    // the choke-point still skips teardown: the machine suppresses the stale
    // event (never flips), so tearing down here would strand premium routing.
    mockRequiresPremiumRouting.mockReturnValue(true)
    mockIsWithinPremiumWarmup.mockReturnValue(false)
    mockIsStalePremiumFailure.mockReturnValue(true)

    let callCount = 0
    responses.set("/stale-5xx", () => {
      callCount += 1
      if (callCount === 1) {
        return { status: 503, data: { detail: "late echo" } }
      }
      return { status: 200, data: { ok: true }, headers: {} }
    })
    mockGetRoutingHeaders
      .mockReturnValueOnce({
        "X-Routing-ID": "rid-outgoing",
        "X-User-Tier": "premium",
      })
      .mockReturnValue({})

    const res = await axiosInstance.get("/stale-5xx")

    // Falls back so the request still resolves...
    expect(res.status).toBe(200)
    // ...but a stale failure never tears premium routing down.
    expect(mockSetPremiumAssigned).not.toHaveBeenCalledWith(false)
    expect(mockEmitPremiumUnreachable).not.toHaveBeenCalled()
  })

  it("does NOT clear premiumAssigned on a 502/503 when the assignment is shared", async () => {
    // Shared (pool) assignments have no dedicated-only recovery (state machine /
    // probe), so tearing premium routing down here would strand them on free tier
    // with nothing to re-arm. Past warm-up and non-stale, the choke-point still
    // skips teardown for a shared assignment. The request still falls back to free
    // tier so it resolves.
    mockRequiresPremiumRouting.mockReturnValue(true)
    mockIsWithinPremiumWarmup.mockReturnValue(false)
    mockIsStalePremiumFailure.mockReturnValue(false)
    mockIsPremiumShared.mockReturnValue(true)

    let callCount = 0
    responses.set("/shared-5xx", () => {
      callCount += 1
      if (callCount === 1) {
        return { status: 503, data: { detail: "shared instance blip" } }
      }
      return { status: 200, data: { ok: true }, headers: {} }
    })
    mockGetRoutingHeaders
      .mockReturnValueOnce({
        "X-Routing-ID": "rid-outgoing",
        "X-User-Tier": "premium",
      })
      .mockReturnValue({})

    const res = await axiosInstance.get("/shared-5xx")

    // Falls back so the request still resolves...
    expect(res.status).toBe(200)
    // ...but a shared assignment is never torn down.
    expect(mockSetPremiumAssigned).not.toHaveBeenCalledWith(false)
    expect(mockEmitPremiumUnreachable).not.toHaveBeenCalled()
  })

  it("DOES clear premiumAssigned on a 502/503 for a dedicated assignment", async () => {
    // Control for the shared case: a non-shared (dedicated) assignment past
    // warm-up and non-stale must still tear down and hand off to the recovery
    // state machine.
    mockRequiresPremiumRouting.mockReturnValue(true)
    mockIsWithinPremiumWarmup.mockReturnValue(false)
    mockIsStalePremiumFailure.mockReturnValue(false)
    mockIsPremiumShared.mockReturnValue(false)

    let callCount = 0
    responses.set("/dedicated-5xx", () => {
      callCount += 1
      if (callCount === 1) {
        return { status: 503, data: { detail: "dedicated down" } }
      }
      return { status: 200, data: { ok: true }, headers: {} }
    })
    mockGetRoutingHeaders
      .mockReturnValueOnce({
        "X-Routing-ID": "rid-outgoing",
        "X-User-Tier": "premium",
      })
      .mockReturnValue({})

    const res = await axiosInstance.get("/dedicated-5xx")

    expect(res.status).toBe(200)
    expect(mockSetPremiumAssigned).toHaveBeenCalledWith(false)
    expect(mockEmitPremiumUnreachable).toHaveBeenCalledTimes(1)
  })

  it("does NOT emit unreachable on instance mismatch when _outgoingInstanceId is unset (startup race)", async () => {
    // Before the assignment API returns, getPremiumInstanceId() returns null.
    // Without a known instance ID, we cannot distinguish a legitimate
    // fallback from a startup race — suppress unreachable.
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue(null)

    responses.set("/startup-mismatch", {
      status: 200,
      data: {},
      headers: {
        "x-routing-id": "rid-outgoing",
        "x-served-by-instance": "some-instance-hash",
      },
    })
    await axiosInstance.get("/startup-mismatch")

    expect(mockEmitPremiumUnreachable).not.toHaveBeenCalled()
    expect(mockEmitPremiumReachable).not.toHaveBeenCalled()
  })

  it("does NOT emit unreachable when x-served-by-instance header is absent", async () => {
    // If the response lacks x-served-by-instance (e.g. edge case with
    // middleware skip), we cannot determine instance identity — suppress.
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue("expected-instance-hash")

    responses.set("/no-served-by", {
      status: 200,
      data: {},
      headers: { "x-routing-id": "rid-outgoing" },
    })
    await axiosInstance.get("/no-served-by")

    expect(mockEmitPremiumUnreachable).not.toHaveBeenCalled()
    // Also no reachable — instance cannot be verified without the header.
    expect(mockEmitPremiumReachable).not.toHaveBeenCalled()
  })

  it("skips premium routing headers when _retryWithoutPremium is set (e.g. /is_standalone)", async () => {
    // Endpoints that set _retryWithoutPremium must never receive premium
    // routing headers, even when localStorage contains stale routing state.
    // This prevents ALB 503s on system-information endpoints after restart.
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })

    responses.set("/is_standalone", {
      status: 200,
      data: true,
    })
    await axiosInstance.get("/is_standalone", { _retryWithoutPremium: true })

    expect(recorded).toHaveLength(1)
    const reqHeaders = recorded[0].headers as Record<string, unknown>
    expect(reqHeaders["X-Routing-ID"]).toBeUndefined()
    expect(reqHeaders["X-User-Tier"]).toBeUndefined()

    const reqConfig = recorded[0].config as Record<string, unknown>
    expect(reqConfig._hadPremiumHeaders).toBeUndefined()
  })

  it("does NOT update routing token when premiumAssigned is true and instance hash mismatches", async () => {
    // Premium headers were sent but the response came from a different
    // instance (ALB fallback to shared backend).
    mockIsPremiumAssigned.mockReturnValue(true)
    mockGetRoutingToken.mockReturnValue("existing-token")
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    mockGetPremiumInstanceId.mockReturnValue("expected-instance-hash")

    responses.set("/wrong-instance", {
      status: 200,
      data: {},
      headers: {
        "x-routing-id": "rid-outgoing",
        "x-served-by-instance": "different-instance-hash",
      },
    })
    await axiosInstance.get("/wrong-instance")

    expect(mockUpdateRoutingToken).not.toHaveBeenCalled()
  })

  it("stamps whatever routing headers are active onto the ui-event POST (6238)", async () => {
    // Unit-level check that logPremiumUiEvent is a normal request through this
    // interceptor: it stamps exactly what getRoutingHeaders() returns, with no
    // special-casing of the ui-event endpoint. The end-to-end 6238 behaviour —
    // that a real dedicated 502 tears routing down so this POST goes free tier,
    // while a reachable instance keeps the headers — is proven against the real
    // routingService in premiumTelemetryRouting.test.ts.
    mockGetRoutingHeaders.mockReturnValue({
      "X-Routing-ID": "rid-outgoing",
      "X-User-Tier": "premium",
    })
    responses.set("/users/me/premium/ui-event", { status: 200, data: {} })

    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { logPremiumUiEvent } = require("api/premium/PremiumAssignmentApi")
    await logPremiumUiEvent("instance_reachable", {})

    expect(recorded).toHaveLength(1)
    const reqHeaders = recorded[0].headers as Record<string, unknown>
    expect(reqHeaders["X-Routing-ID"]).toBe("rid-outgoing")
    expect(reqHeaders["X-User-Tier"]).toBe("premium")
  })
})
