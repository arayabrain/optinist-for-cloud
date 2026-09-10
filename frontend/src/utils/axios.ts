import axiosLibrary, {
  AxiosError,
  AxiosResponse,
  InternalAxiosRequestConfig,
} from "axios"

import { refreshTokenApi } from "api/auth/Auth"
import { API_TIMEOUT, BASE_URL } from "const/API"
import { RoutingHeaders } from "const/Subscription"
import { getExToken, getToken, logout, saveToken } from "utils/auth/AuthUtils"
import {
  isDataviewPublicOutputsRequest,
  DATAVIEW_PUBLIC_REQUEST_KEY,
} from "utils/DataviewUtils"
import {
  routingService,
  type PremiumUnreachableDetail,
} from "utils/routing/RoutingService"

// Extend AxiosRequestConfig to include custom retry property
interface CustomAxiosRequestConfig extends InternalAxiosRequestConfig {
  _retry?: boolean
  _retryWithoutPremium?: boolean
  _hadPremiumHeaders?: boolean
  _outgoingRoutingId?: string
  _outgoingInstanceId?: string
  _premiumSentAt?: number
}

const axios = axiosLibrary.create({
  baseURL: BASE_URL,
  timeout: API_TIMEOUT.DEFAULT,
  headers: {
    Accept: "application/json",
    "Content-Type": "application/json",
  },
})

axios.interceptors.request.use(
  async (config) => {
    // Add authentication headers (skip if null to avoid "Bearer null")
    const token = getToken()
    const exToken = getExToken()

    if (token) {
      config.headers!.Authorization = `Bearer ${token}`
    }
    if (exToken) {
      config.headers!.ExToken = exToken
    }

    // Add premium routing headers for ALB-based routing
    // Skip if this is a free-tier fallback retry
    if (!(config as CustomAxiosRequestConfig)._retryWithoutPremium) {
      const routingHeaders = routingService.getRoutingHeaders()
      Object.assign(config.headers!, routingHeaders)
      const outgoingRoutingId = routingHeaders[RoutingHeaders.ROUTING_ID]
      if (outgoingRoutingId) {
        ;(config as CustomAxiosRequestConfig)._hadPremiumHeaders = true
        ;(config as CustomAxiosRequestConfig)._outgoingRoutingId =
          outgoingRoutingId
        ;(config as CustomAxiosRequestConfig)._premiumSentAt = Date.now()
        const instanceId = routingService.getPremiumInstanceId()
        if (instanceId) {
          ;(config as CustomAxiosRequestConfig)._outgoingInstanceId = instanceId
        }
      }
    }

    // Check whether the access is to public output data (HTTP header setting)
    if (config.url && isDataviewPublicOutputsRequest(config.url)) {
      config.headers![DATAVIEW_PUBLIC_REQUEST_KEY] = "true"
    }

    return config
  },
  (error) => Promise.reject(error),
)

// Track if we're already refreshing to prevent multiple refresh attempts
let isRefreshing = false
// Track if user is logging out to prevent token refresh during logout
let isLoggingOut = false
// Promise to track when logout completes
let logoutCompletePromise: Promise<void> | null = null
let resolveLogoutComplete: (() => void) | null = null

let failedQueue: Array<{
  resolve: (value?: unknown) => void
  reject: (reason?: unknown) => void
}> = []

const processQueue = (error: unknown, token: string | null = null) => {
  failedQueue.forEach((prom) => {
    if (error) {
      prom.reject(error)
    } else {
      prom.resolve(token)
    }
  })
  failedQueue = []
}

// Export function to set logout state - called by logout functions
export const setLoggingOut = (value: boolean) => {
  isLoggingOut = value
  if (value) {
    // Create a promise that will resolve when logout completes
    logoutCompletePromise = new Promise<void>((resolve) => {
      resolveLogoutComplete = resolve
    })
    // Clear the refresh queue when logging out
    processQueue(new Error("User is logging out"), null)
    isRefreshing = false
  } else {
    // Logout complete - resolve the promise
    if (resolveLogoutComplete) {
      resolveLogoutComplete()
      resolveLogoutComplete = null
      logoutCompletePromise = null
    }
  }
}

// Export function to wait for logout to complete
export const waitForLogoutComplete = async () => {
  if (logoutCompletePromise) {
    await logoutCompletePromise
  }
}

/**
 * Handle 401 Unauthorized errors by refreshing the access token and retrying the request
 */
const handleUnauthorizedError = async (
  error: AxiosError,
): Promise<AxiosResponse> => {
  // Guard: originalRequest must exist to proceed
  if (!error.config) {
    return Promise.reject(error)
  }

  const originalRequest = error.config as CustomAxiosRequestConfig

  // eslint-disable-next-line no-console
  console.error(
    "401 error detected:",
    originalRequest.url,
    error.response?.data,
  )

  // Prevent token refresh during logout
  if (isLoggingOut) {
    return Promise.reject(error)
  }

  // Prevent refresh loop - don't retry refresh endpoint itself
  if (originalRequest.url?.includes("/auth/refresh")) {
    // eslint-disable-next-line no-console
    console.error("Refresh token is invalid or expired, logging out")
    logout()
    return Promise.reject(error)
  }

  // Prevent infinite retry loops
  if (originalRequest._retry) {
    // eslint-disable-next-line no-console
    console.error("Token refresh retry failed, logging out")
    logout()
    return Promise.reject(error)
  }

  if (isRefreshing) {
    return new Promise((resolve, reject) => {
      failedQueue.push({ resolve, reject })
    })
      .then((token) => {
        originalRequest.headers.Authorization = `Bearer ${token}`
        return axiosLibrary(originalRequest)
      })
      .catch((err) => {
        return Promise.reject(err)
      })
  }

  originalRequest._retry = true
  isRefreshing = true

  try {
    const { access_token } = await refreshTokenApi()
    saveToken(access_token)
    originalRequest.headers.Authorization = `Bearer ${access_token}`

    processQueue(null, access_token)
    isRefreshing = false

    return axiosLibrary(originalRequest)
  } catch (e) {
    // eslint-disable-next-line no-console
    console.error("Token refresh failed:", e)
    processQueue(e, null)
    isRefreshing = false

    if (
      axiosLibrary.isAxiosError(e) &&
      (e?.response?.status === 400 ||
        e?.response?.status === 401 ||
        e?.response?.status === 422)
    ) {
      // eslint-disable-next-line no-console
      console.error("Invalid refresh token, logging out")
      logout()
    }
    throw e
  }
}

/**
 * Single guarded choke-point for tearing down premium routing after a transient
 * premium-routing failure (wrong-instance 200 or 502/503).
 *   within warm-up → no-op: a freshly-assigned instance is expected to flap;
 *                    tearing down would strand routing (the machine grace-
 *                    suppresses the unreachable event, so recovery never fires).
 *   after warm-up  → tear down + emit so the recovery flow runs.
 * Routing every failure site through here prevents a new site from forgetting
 * the warm-up check.
 */
const tearDownPremiumRoutingUnlessWarmup = (
  detail: PremiumUnreachableDetail,
): void => {
  if (routingService.isWithinPremiumWarmup()) return
  // Stale/out-of-order failure (request sent before the last confirmed-reachable
  // response) — an echo, not a live outage. Tearing down here would strand
  // routing, since the state machine suppresses the event and never arms a
  // recovery probe.
  if (routingService.isStalePremiumFailure(detail.sentAt)) return
  // Shared assignments have no dedicated-only recovery (state machine / probe),
  // so a permanent downgrade here would strand them on free tier with nothing to
  // re-arm. The per-request free-tier retry in the caller still serves the request.
  if (routingService.isPremiumShared()) return
  routingService.setPremiumAssigned(false)
  routingService.emitPremiumUnreachable(detail)
}

/**
 * Handle 503 Service Unavailable errors for premium routing by falling back to free tier
 */
const handlePremiumRoutingError = async (
  error: AxiosError,
): Promise<AxiosResponse> => {
  // Guard: originalRequest must exist to proceed
  if (!error.config) {
    return Promise.reject(error)
  }

  const originalRequest = error.config as CustomAxiosRequestConfig

  // Prevent infinite retry loops
  if (originalRequest._retryWithoutPremium) {
    return Promise.reject(error)
  }

  // Premium instance not ready — fall back to free tier for this request.
  // Teardown is warm-up-gated (see tearDownPremiumRoutingUnlessWarmup).
  tearDownPremiumRoutingUnlessWarmup({
    url: originalRequest.url,
    status: error.response?.status,
    sentAt: originalRequest._premiumSentAt,
  })

  const retryConfig = { ...originalRequest }
  delete retryConfig.headers[RoutingHeaders.USER_TIER]
  delete retryConfig.headers[RoutingHeaders.ROUTING_ID]
  retryConfig._retryWithoutPremium = true
  // Strip premium markers — retry is shared-tier, must not emit reachable.
  delete retryConfig._hadPremiumHeaders
  delete retryConfig._outgoingRoutingId
  delete retryConfig._outgoingInstanceId
  delete retryConfig._premiumSentAt

  try {
    // eslint-disable-next-line no-console
    console.warn("Using free tier while premium instance provisions")
    return await axios(retryConfig)
  } catch (retryError) {
    // eslint-disable-next-line no-console
    console.error("Free tier fallback also failed:", retryError)
    return Promise.reject(error)
  }
}

/**
 * Determines whether a successful response confirms the premium
 * dedicated instance is reachable.
 *
 * Returns true only when ALL of:
 *  1. The request carried premium routing headers
 *  2. The routing-id was NOT rotated (same user identity)
 *  3. The expected instance ID is known (not null/undefined)
 *  4. The serving instance matches the expected dedicated instance
 *
 * When the expected instance ID is unknown (e.g. startup race before the
 * assignment API returns, or backend returned instance_id_hash=null),
 * this function returns false — we cannot verify which instance served
 * the response. This closes the desync gap where premiumAssigned=true
 * with premiumInstanceId=null would silently revert to routing-id-only
 * matching.
 *
 * Note: SecureRoutingMiddleware attaches x-served-by-instance to every
 * authenticated response. The only paths that skip the middleware are
 * unauthenticated endpoints (SKIP_AUTH_PATHS: /health, /auth/login,
 * /auth/refresh) and requests with missing/invalid JWT — none of which
 * are routed through the dedicated instance. Therefore, a legitimate
 * dedicated-instance 200 will always carry the header.
 */
function shouldEmitPremiumReachable(
  res: AxiosResponse,
  cfg: CustomAxiosRequestConfig | undefined,
): boolean {
  if (!cfg?._hadPremiumHeaders) return false

  const routingIdHeader = RoutingHeaders.ROUTING_ID.toLowerCase()
  const routingId = res.headers[routingIdHeader]
  const routingIdRotated =
    typeof routingId === "string" && routingId !== cfg._outgoingRoutingId
  if (routingIdRotated) return false

  // Instance identity check — closes the ALB fallback gap.
  // When the instance ID is unknown (startup race or backend returned null hash),
  // don't emit reachable — we cannot verify which instance served the response.
  // This prevents premiumAssigned=true + premiumInstanceId=null from silently
  // reverting to the routing-id-only check that caused false-positives.
  const outgoingInstanceId = cfg._outgoingInstanceId
  if (!outgoingInstanceId) return false
  const servedByHeader = RoutingHeaders.SERVED_BY_INSTANCE.toLowerCase()
  const servedByInstance = res.headers[servedByHeader]
  if (servedByInstance !== outgoingInstanceId) return false

  return true
}

/**
 * Detects when a successful response (200 OK) came from the wrong instance
 * — indicating ALB fallback after EventBridge cleanup deleted the per-user
 * rule and target group.
 *
 * Returns true only when ALL of:
 *  1. The request carried premium routing headers
 *  2. The expected instance ID was known at request time (not startup race)
 *  3. The response carries an x-served-by-instance header
 *  4. The serving instance differs from the expected dedicated instance
 *
 * When true, the caller should emit premiumUnreachable to trigger the
 * recovery flow (polling /status + /assign). The warm-up grace period
 * (DEDICATED_HANDOFF_GRACE_MS) is respected downstream by the
 * useInstanceUnreachableMachine listener.
 */
function isInstanceMismatch(
  res: AxiosResponse,
  cfg: CustomAxiosRequestConfig | undefined,
): boolean {
  if (!cfg?._hadPremiumHeaders) return false
  const outgoingInstanceId = cfg._outgoingInstanceId
  if (!outgoingInstanceId) return false
  const servedByHeader = RoutingHeaders.SERVED_BY_INSTANCE.toLowerCase()
  const servedByInstance = res.headers[servedByHeader]
  if (typeof servedByInstance !== "string") return false
  return servedByInstance !== outgoingInstanceId
}

axios.interceptors.response.use(
  async (res) => {
    const cfg = res.config as CustomAxiosRequestConfig | undefined

    // Extract routing headers from backend response
    // Note: axios normalizes response header names to lowercase
    const routingIdHeader = RoutingHeaders.ROUTING_ID.toLowerCase()
    const routingId = res.headers[routingIdHeader]
    if (routingId) {
      // Only update the routing token when:
      //  (a) the user is NOT in premium-assigned mode (initial token seeding), or
      //  (b) the response came from the verified premium instance, or
      //  (c) the current token is null (recovery from cleared state).
      // This prevents non-premium responses (free/public tier 200s after
      // 502/503 fallback) from overwriting the token while premiumAssigned
      // is true — breaking the feedback loop described in issue #605.
      // Condition (c) ensures that (premiumAssigned=true, token=null) is
      // never a permanent trap — any response carrying X-Routing-ID can
      // re-seed a missing token. This is safe because the token value is
      // deterministic (HMAC of UID) and identical across all backends.
      const isPremiumMode = routingService.isPremiumAssigned()
      const tokenIsNull = routingService.getRoutingToken() == null
      const instanceVerified =
        cfg?._hadPremiumHeaders &&
        cfg._outgoingInstanceId &&
        res.headers[RoutingHeaders.SERVED_BY_INSTANCE.toLowerCase()] ===
          cfg._outgoingInstanceId
      if (!isPremiumMode || tokenIsNull || instanceVerified) {
        routingService.updateRoutingToken(routingId)
      }
    }

    if (shouldEmitPremiumReachable(res, cfg)) {
      routingService.emitPremiumReachable({
        url: cfg!.url,
        status: res.status,
        sentAt: cfg!._premiumSentAt,
      })
    } else if (isInstanceMismatch(res, cfg)) {
      // Active detection: 200 OK from wrong instance (ALB fallback after
      // EventBridge cleanup). Tear down to trigger the recovery flow — gated so
      // a warm-up mismatch (fresh instance still registering) is left alone.
      tearDownPremiumRoutingUnlessWarmup({
        url: cfg!.url,
        status: res.status,
        sentAt: cfg!._premiumSentAt,
      })
    }
    return res
  },
  async (error) => {
    if (error?.response?.status === 401) {
      return handleUnauthorizedError(error)
    }

    // ALB 502/503 when premium instance is unavailable (target group empty
    // or unhealthy after environment restart).
    // Also handle network errors (ERR_FAILED / no response)
    const is502or503 =
      error?.response?.status === 502 || error?.response?.status === 503
    const isNetworkError = !error?.response && !!error?.config
    if (
      (is502or503 || isNetworkError) &&
      routingService.requiresPremiumRouting()
    ) {
      return handlePremiumRoutingError(error)
    }

    return Promise.reject(error)
  },
)

export default axios
