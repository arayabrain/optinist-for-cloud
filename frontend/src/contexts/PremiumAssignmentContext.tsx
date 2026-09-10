/**
 * Premium Assignment Context Provider
 *
 * Single source of truth for premium assignment logic across the entire app.
 * Eliminates the issue of multiple hook instances causing duplicate API calls.
 */

import React, {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
  useRef,
} from "react"
import { flushSync } from "react-dom"
import { useDispatch, useSelector } from "react-redux"

import {
  assignPremiumInstance,
  getBeaconTokenApi,
  getPremiumStatus,
  getRoutingInfo,
  releasePremiumInstance,
  sendPremiumHeartbeat,
  PremiumAssignment,
  PremiumAssignmentResult,
  PremiumStatusResult,
  RoutingInfo,
} from "api/premium/PremiumAssignmentApi"
import { BASE_URL } from "const/API"
import { PlanName, PremiumTiming, SubscriptionStatus } from "const/Subscription"
import { shouldPoll } from "contexts/premium/unreachableMachine"
import {
  InstanceUnreachableHandle,
  useInstanceUnreachableMachine,
} from "contexts/premium/useInstanceUnreachableMachine"
import { useSleepDetection } from "hooks/useSleepDetection"
import { selectPipelineStatus } from "store/slice/Pipeline/PipelineSelectors"
import { RUN_STATUS } from "store/slice/Pipeline/PipelineType"
import { getMe } from "store/slice/User/UserActions"
import { selectLogoutGeneration } from "store/slice/User/UserSelector"
import { AppDispatch, RootState } from "store/store"
import { logout as authLogout } from "utils/auth/AuthUtils"
import {
  CrossTabLeaderElection,
  syncActivityAcrossTabs,
  getLastActivityFromAnyTab,
  onActivityFromOtherTab,
  tabSync,
} from "utils/crossTabSync"
import { routingService } from "utils/routing/RoutingService"

// Absolute URL for sendBeacon (must target the API server directly).
const BEACON_RELEASE_URL = `${BASE_URL}/users/me/premium/release-beacon`
const BEACON_CONTENT_TYPE = "text/plain"

// Polling configuration constants
// Backend rate limit is 30s, so initial interval must be >= 30s
const INITIAL_POLL_INTERVAL_MS = 30000
const MAX_POLL_INTERVAL_MS = 60000
const MAX_POLL_ATTEMPTS = 40
const BACKOFF_MULTIPLIER = 1.5
const ERROR_BACKOFF_MULTIPLIER = 2
// When polling returns assignment=null after a retryable assign response,
// re-trigger assignPremiumInstance() every N polls instead of only polling
// status. This handles the case where the assign API timed out without
// actually creating an assignment (e.g., lock contention).
const ASSIGN_RETRY_POLL_THRESHOLD = 3
// Maximum number of re-trigger assign attempts before stopping.
// Independent of pollAttempts so that finalizeDedicatedAssignment (which
// resets pollAttempts to 0) cannot remove the overall ceiling.
// Only reset when confirmed reachable (instanceUnreachable → false).
const MAX_RETRIGGER_ATTEMPTS = 5
// 5 min poll to detect subscription expiry; lower if tighter detection needed
const SUBSCRIPTION_CHECK_INTERVAL_MS = 5 * 60 * 1000

// sessionStorage keys — per-tab persistence across page refreshes.
// Clears automatically when the tab closes.
const SS_HAS_ATTEMPTED = "premium_hasAttempted"
export const SS_POLL_ATTEMPTS = "premium_pollAttempts"

// Canonical PremiumAssignment (from /status) → PremiumAssignmentResult shape
// consumed by the rest of the provider. Fields absent from /status
// (retry_after, scaling_in_progress) stay undefined.
const statusToAssignmentResult = (
  assignment: PremiumAssignment,
  message: string,
): PremiumAssignmentResult => ({
  message,
  instance_id: assignment.instance_id,
  instance_id_hash: assignment.instance_id_hash,
  assigned: true,
  is_shared: assignment.is_shared,
  assignment_source: assignment.assignment_source,
})

function ssRead(key: string): string | null {
  try {
    return sessionStorage.getItem(key)
  } catch {
    return null
  }
}

function ssWrite(key: string, value: string): void {
  try {
    sessionStorage.setItem(key, value)
  } catch {
    // sessionStorage unavailable (e.g., some private browsing modes)
  }
}

function ssRemove(key: string): void {
  try {
    sessionStorage.removeItem(key)
  } catch {
    // sessionStorage unavailable
  }
}

// Heartbeat retry configuration (Case 49)
const HEARTBEAT_MAX_RETRIES = 3
const HEARTBEAT_RETRY_DELAY_MS = 1000

// Throttle for the passive activity listener: genuine user interaction
// advances the inactivity clock at most once per this interval.
const ACTIVITY_MARK_THROTTLE_MS = 60 * 1000

interface PremiumAssignmentState {
  isAssigning: boolean
  isReleasing: boolean
  assignmentResult: PremiumAssignmentResult | null
  statusResult: PremiumStatusResult | null
  routingInfo: RoutingInfo | null
  error: string | null
  isRetryableError: boolean
  isPremiumUser: boolean
  showInactivityWarning: boolean
  lastActivityTime: number
  // Orthogonal to instanceUnreachable — same 503 can set both; they are not redundant.
  heartbeatFailing: boolean
}

interface PremiumAssignmentContextType extends PremiumAssignmentState {
  assign: () => Promise<PremiumAssignmentResult | null>
  release: () => Promise<unknown>
  getStatus: () => Promise<PremiumStatusResult | null>
  updateRoutingInfo: () => Promise<RoutingInfo | null>
  autoReleaseOnLogout: () => void
  dismissInactivityWarning: () => void
  recordActivity: () => Promise<void>
  unreachable: InstanceUnreachableHandle
}

const PremiumAssignmentContext =
  createContext<PremiumAssignmentContextType | null>(null)

export const usePremiumAssignment = () => {
  const context = useContext(PremiumAssignmentContext)
  if (!context) {
    throw new Error(
      "usePremiumAssignment must be used within PremiumAssignmentProvider",
    )
  }
  return context
}

export const PremiumAssignmentProvider: React.FC<{
  children: React.ReactNode
}> = ({ children }) => {
  const currentUser = useSelector((state: RootState) => state.user.currentUser)
  const dispatch = useDispatch<AppDispatch>()
  // Track logout generation to detect stale closures
  const logoutGeneration = useSelector(selectLogoutGeneration)

  // A running workflow counts as activity even without direct user input,
  // so a long unattended analysis is never falsely auto-released.
  const pipelineStatus = useSelector(selectPipelineStatus)
  const isWorkflowRunning =
    pipelineStatus === RUN_STATUS.START_PENDING ||
    pipelineStatus === RUN_STATUS.START_SUCCESS
  const isWorkflowRunningRef = useRef(isWorkflowRunning)
  useEffect(() => {
    isWorkflowRunningRef.current = isWorkflowRunning
  }, [isWorkflowRunning])

  const [state, setState] = useState<PremiumAssignmentState>({
    isAssigning: false,
    isReleasing: false,
    assignmentResult: null,
    statusResult: null,
    routingInfo: null,
    error: null,
    isRetryableError: false,
    isPremiumUser: false,
    showInactivityWarning: false,
    lastActivityTime: Date.now(),
    heartbeatFailing: false,
  })

  // Ref guard to prevent multiple auto-assignment attempts per mount.
  // useRef (not useState) so the flag is set synchronously and survives
  // StrictMode double-invocations without triggering extra renders.
  const hasAttemptedRef = useRef(ssRead(SS_HAS_ATTEMPTED) === "true")

  // Bumped to re-fire the auto-assign effect after inactivity release.
  // Unlike hasAttemptedRef (which is also false on initial mount), this ref
  // is only true after an explicit inactivity release — preventing the
  // duplicate-/assign regression.
  const [autoAssignGeneration, setAutoAssignGeneration] = useState(0)
  const needsReassignAfterReleaseRef = useRef(false)

  // Polling state with backoff.
  // NOTE: pollAttempts is dual-purpose — it's the attempt counter (MAX_POLL_ATTEMPTS
  // stop, re-trigger threshold) AND a keep-alive tick: it's in the poll effect's
  // dependency array, so incrementing it every cycle re-runs the effect and
  // reschedules the next poll even after pollInterval saturates (needed for shared).
  const [pollInterval, setPollInterval] = useState(INITIAL_POLL_INTERVAL_MS)
  const [pollAttempts, setPollAttempts] = useState(() => {
    const stored = ssRead(SS_POLL_ATTEMPTS)
    const n = Number(stored)
    return Number.isNaN(n) ? 0 : n
  })

  // Sync pollAttempts to sessionStorage so the cap survives page refreshes
  useEffect(() => {
    if (pollAttempts > 0) {
      ssWrite(SS_POLL_ATTEMPTS, String(pollAttempts))
    } else if (ssRead(SS_POLL_ATTEMPTS) !== null) {
      ssRemove(SS_POLL_ATTEMPTS)
    }
  }, [pollAttempts])

  // Cross-tab leader election for coordinating polling
  const [isTabLeader, setIsTabLeader] = useState(false)
  const leaderElectionRef = useRef<CrossTabLeaderElection | null>(null)
  const beaconTokenRef = useRef<string | null>(null)

  // Monotonically increasing generation counter, incremented synchronously
  // on every release path (autoReleaseOnLogout, cross-tab PREMIUM_RELEASED,
  // explicit release, logout, MAX_POLL_ATTEMPTS).  The polling callback
  // captures the value before its first await and re-checks after each
  // subsequent await.  A mismatch means a release occurred during the
  // in-flight call — the callback bails instead of resurrecting a released
  // instance.
  const releaseGenerationRef = useRef(0)
  // Bounded counter for re-trigger assign attempts.  Independent of
  // pollAttempts so that finalizeDedicatedAssignment (which resets
  // pollAttempts) cannot remove the overall ceiling.  Only reset when
  // confirmed reachable (instanceUnreachable → false).
  const retriggerCountRef = useRef(0)
  // In-flight guard to prevent overlapping re-trigger calls across
  // concurrent polling callbacks.
  const isRetriggeringRef = useRef(false)

  // Refs for values that inactivity check needs but shouldn't trigger re-renders
  const lastActivityTimeRef = useRef(state.lastActivityTime)
  const showInactivityWarningRef = useRef(state.showInactivityWarning)
  // Last time we broadcast activity to other tabs, shared by the passive
  // activity listener and the running-workflow guard to throttle cross-tab
  // writes to once per ACTIVITY_MARK_THROTTLE_MS.
  const lastActivityMarkRef = useRef(0)
  // Track previous premium status to detect subscription expiry transition
  const prevIsPremiumRef = useRef(false)

  const unreachable = useInstanceUnreachableMachine({
    assignment: state.assignmentResult,
    isTabLeader,
  })

  // Calculate premium user status
  const isPremiumUser =
    currentUser?.subscription_plan_name === PlanName.PREMIUM &&
    currentUser?.subscription_status === SubscriptionStatus.PREMIUM

  // Update state when premium status changes
  useEffect(() => {
    setState((prev) => ({ ...prev, isPremiumUser }))
  }, [isPremiumUser])

  // Periodic subscription status refresh for premium users.
  // Keeps Redux currentUser.subscription_status fresh so we can detect expiry.
  useEffect(() => {
    if (!isPremiumUser || !isTabLeader) return

    const interval = setInterval(() => {
      dispatch(getMe())
    }, SUBSCRIPTION_CHECK_INTERVAL_MS)

    return () => clearInterval(interval)
  }, [isPremiumUser, isTabLeader, dispatch])

  // Reset flag when user changes
  useEffect(() => {
    hasAttemptedRef.current = false
    prevIsPremiumRef.current = isPremiumUser
    ssRemove(SS_HAS_ATTEMPTED)
    ssRemove(SS_POLL_ATTEMPTS)
    // isPremiumUser intentionally excluded — this effect syncs refs on user
    // identity change only; the auto-logout effect handles isPremiumUser transitions.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentUser?.id])

  // Reset state when logout generation changes to prevent stale closures
  useEffect(() => {
    if (logoutGeneration > 0) {
      // Clear any cached assignment state on logout
      releaseGenerationRef.current += 1
      setState({
        isAssigning: false,
        isReleasing: false,
        assignmentResult: null,
        statusResult: null,
        routingInfo: null,
        error: null,
        isRetryableError: false,
        isPremiumUser: false,
        showInactivityWarning: false,
        lastActivityTime: Date.now(),
        heartbeatFailing: false,
      })
      unreachable.reset()
      hasAttemptedRef.current = false
      prevIsPremiumRef.current = false
      ssRemove(SS_HAS_ATTEMPTED)
      ssRemove(SS_POLL_ATTEMPTS)
    }
    // Only run on logoutGeneration change, not initial mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logoutGeneration])

  // Sync refs with state to avoid including in inactivity effect dependencies
  useEffect(() => {
    lastActivityTimeRef.current = state.lastActivityTime
  }, [state.lastActivityTime])

  useEffect(() => {
    showInactivityWarningRef.current = state.showInactivityWarning
  }, [state.showInactivityWarning])

  // Reset the re-trigger counter when the instance becomes reachable again.
  // Only a confirmed-reachable response (emitPremiumReachable → state machine
  // transition) flips instanceUnreachable to false — finalizeDedicatedAssignment
  // alone does not reset it.
  useEffect(() => {
    if (!unreachable.state.instanceUnreachable) {
      retriggerCountRef.current = 0
      isRetriggeringRef.current = false
    }
  }, [unreachable.state.instanceUnreachable])

  // Initialize cross-tab leader election for premium users
  useEffect(() => {
    if (!isPremiumUser) {
      // Clean up leader election if user is not premium
      if (leaderElectionRef.current) {
        leaderElectionRef.current.destroy()
        leaderElectionRef.current = null
        setIsTabLeader(false)
      }
      return
    }

    // Create leader election instance
    leaderElectionRef.current = new CrossTabLeaderElection(
      () => setIsTabLeader(true),
      () => setIsTabLeader(false),
    )

    // Check initial leadership state
    setIsTabLeader(leaderElectionRef.current.getIsLeader())

    return () => {
      if (leaderElectionRef.current) {
        leaderElectionRef.current.destroy()
        leaderElectionRef.current = null
      }
    }
  }, [isPremiumUser])

  /**
   * Dismiss inactivity warning
   */
  const dismissInactivityWarning = useCallback(() => {
    setState((prev) => ({ ...prev, showInactivityWarning: false }))
  }, [])

  /**
   * Sleep utility for retry delays
   */
  const sleep = useCallback(
    (ms: number) => new Promise((resolve) => setTimeout(resolve, ms)),
    [],
  )

  /**
   * Record user activity by sending heartbeat with retry logic (Case 49)
   */
  const recordActivity = useCallback(async (): Promise<void> => {
    if (!isPremiumUser) return

    for (let attempt = 0; attempt < HEARTBEAT_MAX_RETRIES; attempt++) {
      try {
        await sendPremiumHeartbeat()
        const now = Date.now()
        setState((prev) => ({
          ...prev,
          lastActivityTime: now,
          showInactivityWarning: false,
          heartbeatFailing: false,
        }))
        syncActivityAcrossTabs(now)
        return
      } catch (error) {
        const isLastAttempt = attempt === HEARTBEAT_MAX_RETRIES - 1
        if (isLastAttempt) {
          // eslint-disable-next-line no-console
          console.error("Heartbeat failed after retries:", error)
          const now = Date.now()
          setState((prev) => ({
            ...prev,
            lastActivityTime: now,
            heartbeatFailing: true,
          }))
          syncActivityAcrossTabs(now)
          throw error
        }
        // eslint-disable-next-line no-console
        console.warn(`Heartbeat attempt ${attempt + 1} failed, retrying...`)
        await sleep(HEARTBEAT_RETRY_DELAY_MS * (attempt + 1))
      }
    }
  }, [isPremiumUser, sleep])

  /**
   * Mark genuine user interaction as activity.
   * Advances the frontend inactivity clock (and syncs it across tabs) so the
   * 1h warning / 2h auto-release only fire on a truly idle session. Throttled
   * and frontend-local — it does not send a backend heartbeat (normal API
   * traffic already keeps the backend's last_activity fresh).
   */
  const markLocalActivity = useCallback(() => {
    if (!isPremiumUser) return
    const now = Date.now()
    // Throttled early-return does not dismiss the warning, but this is safe:
    // a warning only appears after >=1h of no activity, so the last mark is
    // also >=1h old and any interaction while it shows always passes the
    // throttle and reaches the dismissal below.
    if (now - lastActivityMarkRef.current < ACTIVITY_MARK_THROTTLE_MS) return
    lastActivityMarkRef.current = now
    lastActivityTimeRef.current = now
    setState((prev) => ({
      ...prev,
      lastActivityTime: now,
      showInactivityWarning: false,
    }))
    syncActivityAcrossTabs(now)
  }, [isPremiumUser])

  /**
   * Assign premium instance
   */
  const assign =
    useCallback(async (): Promise<PremiumAssignmentResult | null> => {
      if (!isPremiumUser) {
        const error = "Premium subscription required"
        setState((prev) => ({ ...prev, error }))
        return null
      }

      setState((prev) => ({
        ...prev,
        isAssigning: true,
        error: null,
        isRetryableError: false,
      }))

      try {
        const result = await assignPremiumInstance()
        const isRetryable =
          !result.assigned &&
          (result.scaling_in_progress || result.retry_after != null)

        setState((prev) => ({
          ...prev,
          isAssigning: false,
          assignmentResult: result,
          error: result.assigned ? null : result.message,
          isRetryableError: isRetryable,
        }))

        if (result.assigned) {
          routingService.setPremiumAssigned(true)
          routingService.setPremiumInstanceId(result.instance_id_hash ?? null)
          routingService.setPremiumShared(result.is_shared ?? false)
          try {
            const tokenRes = await getBeaconTokenApi()
            beaconTokenRef.current = tokenRes.data.token
          } catch {
            // Non-critical; beacon will fail gracefully
          }
        }

        return result
      } catch (error: unknown) {
        const errorMessage =
          error &&
          typeof error === "object" &&
          "response" in error &&
          error.response &&
          typeof error.response === "object" &&
          "data" in error.response &&
          error.response.data &&
          typeof error.response.data === "object" &&
          "detail" in error.response.data
            ? (error.response.data as { detail: string }).detail
            : error instanceof Error
              ? error.message
              : "Assignment failed"

        setState((prev) => ({
          ...prev,
          isAssigning: false,
          error: errorMessage,
          isRetryableError: false,
        }))
        return null
      }
    }, [isPremiumUser])

  /**
   * Release premium instance
   */
  const release = useCallback(async () => {
    setState((prev) => ({ ...prev, isReleasing: true, error: null }))

    try {
      const result = await releasePremiumInstance()
      // Clear beacon token so beforeunload doesn't fire a duplicate release
      beaconTokenRef.current = null
      releaseGenerationRef.current += 1
      setState((prev) => ({
        ...prev,
        isReleasing: false,
        assignmentResult: null,
        statusResult: null,
      }))
      // Defensive — covers refs outside the reducer that the hook's mirror effect doesn't touch.
      unreachable.reset()

      routingService.resetForRelease()
      // Notify other tabs about premium release
      tabSync.broadcastPremiumReleased()

      return result
    } catch (error: unknown) {
      // eslint-disable-next-line no-console
      console.warn("Premium instance release warning:", error)
      setState((prev) => ({ ...prev, isReleasing: false }))
      return { released: true, message: "Release completed with warnings" }
    }
  }, [unreachable])

  /**
   * Get current status
   */
  const getStatus = useCallback(async () => {
    try {
      const status = await getPremiumStatus()
      setState((prev) => ({ ...prev, statusResult: status }))
      return status
    } catch (error: unknown) {
      // eslint-disable-next-line no-console
      console.warn("Failed to get premium status:", error)
      return null
    }
  }, [])

  /**
   * Update routing info
   */
  const updateRoutingInfo = useCallback(async () => {
    if (!currentUser) return null

    try {
      const routing = await getRoutingInfo()
      setState((prev) => ({ ...prev, routingInfo: routing }))

      // Update the routing service
      routingService.updateRoutingInfo(currentUser)

      return routing
    } catch (error: unknown) {
      // eslint-disable-next-line no-console
      console.warn("Failed to get routing info:", error)
      return null
    }
  }, [currentUser])

  /**
   * Auto-assign on premium user login (fully isolated to prevent loops)
   */
  const autoAssignOnLogin = useCallback(async () => {
    if (!isPremiumUser || hasAttemptedRef.current) return

    // Set flag immediately to prevent duplicate calls
    hasAttemptedRef.current = true

    try {
      // Check current status first (inline to avoid dependency issues)
      const statusResponse = await getPremiumStatus()
      if (statusResponse?.error) {
        // Lambda failed transiently — don't trigger assignment flow.
        // Not persisted to sessionStorage, so page refresh retries.
        return
      }
      if (statusResponse?.assignment) {
        // User already has an assignment - update state immediately so notifications trigger
        const assignmentResult: PremiumAssignmentResult = {
          ...statusToAssignmentResult(
            statusResponse.assignment,
            "Premium instance already assigned",
          ),
          assignment_source:
            statusResponse.assignment.assignment_source ?? "existing",
        }
        setState((prev) => ({
          ...prev,
          assignmentResult,
          error: null,
          isRetryableError: false,
        }))
        routingService.setPremiumAssigned(true)
        routingService.setPremiumInstanceId(
          assignmentResult.instance_id_hash ?? null,
        )
        routingService.setPremiumShared(assignmentResult.is_shared ?? false)
        try {
          const tokenRes = await getBeaconTokenApi()
          beaconTokenRef.current = tokenRes.data.token
        } catch {
          // Non-critical; beacon will fail gracefully
        }
        ssWrite(SS_HAS_ATTEMPTED, "true") // duplicated in assign path below — both exits must persist
        return
      }

      flushSync(() => {
        setState((prev) => ({ ...prev, isAssigning: true }))
      })

      // Attempt assignment directly (inline to avoid dependency issues)
      const assignmentResponse = await assignPremiumInstance()
      if (assignmentResponse?.assigned) {
        // Update state to reflect the assignment
        setState((prev) => ({
          ...prev,
          isAssigning: false,
          assignmentResult: assignmentResponse,
          error: null,
        }))
        routingService.setPremiumAssigned(true)
        routingService.setPremiumInstanceId(
          assignmentResponse.instance_id_hash ?? null,
        )
        routingService.setPremiumShared(assignmentResponse.is_shared ?? false)
        try {
          const tokenRes = await getBeaconTokenApi()
          beaconTokenRef.current = tokenRes.data.token
        } catch {
          // Non-critical; beacon will fail gracefully
        }
      } else {
        // Only store assignmentResult for transient errors (scaling/retry)
        // so the waiting popup stays visible and polling retries.
        // For non-retryable errors, leave assignmentResult null to prevent
        // contradictory polling + "Falling back to shared" notification.
        const isRetryable =
          assignmentResponse.scaling_in_progress ||
          assignmentResponse.retry_after != null
        setState((prev) => ({
          ...prev,
          isAssigning: false,
          assignmentResult: isRetryable ? assignmentResponse : null,
          error: assignmentResponse.message || null,
          isRetryableError: isRetryable,
        }))
      }
      // Persist only after a successful status/assign round-trip.
      // On network error (catch below), leave unpersisted so refresh retries.
      ssWrite(SS_HAS_ATTEMPTED, "true") // duplicated in already-assigned path above — both exits must persist
    } catch (error) {
      // hasAttemptedRef stays true to prevent rapid-fire retries on this mount.
      // sessionStorage is NOT written, so a page refresh will retry.
      // Set error state so the error notification fires
      const errorMessage =
        error instanceof Error ? error.message : "Assignment failed"
      // eslint-disable-next-line no-console
      console.warn("Auto-assignment failed:", error)
      setState((prev) => ({
        ...prev,
        isAssigning: false,
        error: errorMessage,
        isRetryableError: false,
      }))
      routingService.clearRoutingInfo()
    }
  }, [isPremiumUser]) // refs and setState are stable — no other deps needed

  /**
   * Auto-release on logout via sendBeacon.
   * Uses the HMAC-signed beacon token (no auth header needed),
   * so it's safe to call right before dispatch(logout) clears
   * the auth token from localStorage.
   */
  const autoReleaseOnLogout = useCallback((): void => {
    if (beaconTokenRef.current) {
      const blob = new Blob(
        [JSON.stringify({ token: beaconTokenRef.current })],
        { type: BEACON_CONTENT_TYPE },
      )
      navigator.sendBeacon(BEACON_RELEASE_URL, blob)
      beaconTokenRef.current = null
    }
    releaseGenerationRef.current += 1
    setState((prev) => ({
      ...prev,
      assignmentResult: null,
      statusResult: null,
    }))
    routingService.resetForRelease()
  }, [])

  // Auto-logout when subscription expires during an active session.
  // Detects premium → non-premium transition and releases the instance.
  useEffect(() => {
    if (prevIsPremiumRef.current && !isPremiumUser && state.assignmentResult) {
      // Subscription expired mid-session — release instance + force logout
      autoReleaseOnLogout()
      tabSync.broadcastLogout()
      // Fire-and-forget: authLogout is async and redirect is intentionally not awaited.
      // skipBackendLogout: autoReleaseOnLogout already released the instance via
      // sendBeacon, so suppress the free-logout endpoint (user is now free tier).
      // The ref update on the last line prevents re-trigger on subsequent renders.
      authLogout({ skipBackendLogout: true })
    }
    prevIsPremiumRef.current = isPremiumUser
    // tabSync.broadcastLogout and authLogout are stable module imports.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPremiumUser, state.assignmentResult, autoReleaseOnLogout])

  // Inactivity monitoring for premium users
  // Uses refs for lastActivityTime/showInactivityWarning to avoid interval churn
  useEffect(() => {
    if (!isPremiumUser || !currentUser || !state.assignmentResult) {
      return
    }

    let inactivityCheckInterval: ReturnType<typeof setInterval> | null = null

    const checkInactivity = () => {
      const now = Date.now()

      // A running workflow keeps the instance active even with no direct
      // input. Advance the clock so the 1h/2h countdown only starts once the
      // workflow finishes, and clear any warning already shown.
      if (isWorkflowRunningRef.current) {
        lastActivityTimeRef.current = now
        // Throttle the cross-tab broadcast to once per minute (same rationale
        // as markLocalActivity) — a long-running workflow otherwise writes
        // localStorage every 30s for its whole duration. Other tabs still see
        // fresh activity well within the 1h idle threshold.
        if (now - lastActivityMarkRef.current >= ACTIVITY_MARK_THROTTLE_MS) {
          lastActivityMarkRef.current = now
          syncActivityAcrossTabs(now)
        }
        if (showInactivityWarningRef.current) {
          setState((prev) => ({ ...prev, showInactivityWarning: false }))
        }
        return
      }

      // Check activity from any tab, not just this one
      const lastActivityAnyTab = getLastActivityFromAnyTab()
      const effectiveLastActivity = Math.max(
        lastActivityTimeRef.current,
        lastActivityAnyTab,
      )
      const timeSinceLastActivity = now - effectiveLastActivity

      const warningMs = PremiumTiming.INACTIVITY_WARNING_MINUTES * 60 * 1000
      const releaseMs = PremiumTiming.INACTIVITY_RELEASE_MINUTES * 60 * 1000
      // eslint-disable-next-line no-console
      console.log(
        `Inactivity check: ${Math.round(timeSinceLastActivity / 1000 / 60)}min ` +
          "since last activity (any tab)",
      )

      if (timeSinceLastActivity >= releaseMs) {
        // eslint-disable-next-line no-console
        console.warn(
          "2 hours of inactivity detected - auto-releasing premium instance",
        )
        setState((prev) => ({ ...prev, showInactivityWarning: false }))
        autoReleaseOnLogout()
        // Notify other tabs so they can also re-prime for reassignment.
        tabSync.broadcastPremiumReleased()
        // Reset the assignment guard so autoAssignOnLogin can run again.
        hasAttemptedRef.current = false
        ssRemove(SS_HAS_ATTEMPTED)
        // Flag that the next user gesture should trigger reassignment.
        needsReassignAfterReleaseRef.current = true
      } else if (
        timeSinceLastActivity >= warningMs &&
        !showInactivityWarningRef.current
      ) {
        // eslint-disable-next-line no-console
        console.warn("1 hour of inactivity detected - showing warning")
        setState((prev) => ({ ...prev, showInactivityWarning: true }))
      }
    }

    // Check inactivity every 30 seconds
    inactivityCheckInterval = setInterval(checkInactivity, 30 * 1000)

    // Listen for activity from other tabs to dismiss warning
    const unsubscribe = onActivityFromOtherTab((timestamp) => {
      setState((prev) => ({
        ...prev,
        lastActivityTime: Math.max(prev.lastActivityTime, timestamp),
        showInactivityWarning: false,
      }))
    })

    return () => {
      if (inactivityCheckInterval) {
        clearInterval(inactivityCheckInterval)
      }
      unsubscribe()
    }
  }, [isPremiumUser, currentUser, state.assignmentResult, autoReleaseOnLogout])

  // Genuine user interaction (pointer/keyboard/scroll) resets the inactivity
  // clock; a pointer/keyboard gesture also re-fires auto-assign once after an
  // inactivity auto-release. Both concerns share a single set of window
  // listeners. Throttled via markLocalActivity so a busy session does not spam
  // state updates or cross-tab writes.
  useEffect(() => {
    if (!isPremiumUser) return

    const onGesture = () => {
      // Re-fire auto-assign after an inactivity auto-release when the user
      // resumes activity. Guarded by needsReassignAfterReleaseRef (not
      // hasAttemptedRef) so normal initial-mount clicks never bump the
      // counter — avoiding a duplicate /assign.
      if (needsReassignAfterReleaseRef.current) {
        needsReassignAfterReleaseRef.current = false
        setAutoAssignGeneration((g) => g + 1)
      }
      markLocalActivity()
    }
    // Scroll counts as activity but must not trigger reassignment: it can be
    // code-driven (e.g. the auto-scrolling log panel in ScrollLogs), so it only
    // marks activity. A code-driven scroll therefore still resets the idle
    // clock, but in practice that autoscroll coincides with a running workflow,
    // which the inactivity guard already keeps alive. capture:true so scrolls
    // inside inner containers (which don't bubble to window) also count.
    const onScroll = () => markLocalActivity()
    const scrollOpts = { passive: true, capture: true } as const
    window.addEventListener("pointerdown", onGesture)
    window.addEventListener("keydown", onGesture)
    window.addEventListener("scroll", onScroll, scrollOpts)

    return () => {
      window.removeEventListener("pointerdown", onGesture)
      window.removeEventListener("keydown", onGesture)
      window.removeEventListener("scroll", onScroll, scrollOpts)
    }
  }, [isPremiumUser, markLocalActivity])

  // Sleep/wake detection callback (Cases 50-51)
  // Send a backend heartbeat to keep the instance alive, but do NOT reset
  // the frontend inactivity timer.  Device wake (e.g. macOS Power Nap) is
  // not a user interaction — only explicit gestures ("Stay Active" button)
  // should reset the 2h inactivity countdown.
  const handleDeviceWake = useCallback(() => {
    if (!isPremiumUser || !state.assignmentResult) return
    sendPremiumHeartbeat().catch((error) => {
      // eslint-disable-next-line no-console
      console.warn("Failed to send heartbeat after wake:", error)
    })
  }, [isPremiumUser, state.assignmentResult])

  // Detect sleep/wake cycles and send backend heartbeat (Cases 50-51)
  useSleepDetection(handleDeviceWake, {
    enabled: isPremiumUser && !!state.assignmentResult,
  })

  // Listen for premium release events from other tabs (Cases 54-56)
  useEffect(() => {
    const unsubscribe = tabSync.on("PREMIUM_RELEASED", () => {
      // Backend has already released this assignment; drop the local token
      // so a later logout/beforeunload doesn't beacon a now-invalid token.
      beaconTokenRef.current = null
      releaseGenerationRef.current += 1
      setState((prev) => ({
        ...prev,
        assignmentResult: null,
        statusResult: null,
      }))
      // Mirror the same-tab release path: clear assigned flag, instance ID,
      // and token together. Without setPremiumAssigned(false), this tab's
      // in-memory RoutingService stays premiumAssigned=true with token=null
      // — an unrecoverable state where the interceptor guard blocks
      // re-seeding and getRoutingHeaders() returns {}.
      routingService.resetForRelease()
      // Allow this tab to reassign on next user gesture.
      hasAttemptedRef.current = false
      ssRemove(SS_HAS_ATTEMPTED)
      needsReassignAfterReleaseRef.current = true
    })
    return unsubscribe
  }, [])

  // Auto-assign when premium user is detected
  useEffect(() => {
    if (isPremiumUser && currentUser) {
      autoAssignOnLogin()
    } else {
      // eslint-disable-next-line no-console
      console.warn("Conditions not met for auto-assignment:", {
        isPremiumUser,
        hasCurrentUser: !!currentUser,
      })
    }
  }, [isPremiumUser, currentUser, autoAssignOnLogin, autoAssignGeneration])

  // Reset polling state when user changes or gets a dedicated instance
  useEffect(() => {
    if (!state.assignmentResult?.is_shared) {
      setPollInterval(INITIAL_POLL_INTERVAL_MS)
      setPollAttempts(0)
    }
  }, [state.assignmentResult?.is_shared])

  // Shared helper for the polling effect: finalize a dedicated assignment
  // by updating state, restoring routing, acquiring the beacon token, and
  // resetting the polling cadence.  Used by both the "status found dedicated"
  // path and the "re-trigger assign succeeded" path to avoid duplication.
  const finalizeDedicatedAssignment = async (
    result: PremiumAssignmentResult,
    statusResult?: PremiumStatusResult | null,
  ) => {
    setState((prev) => ({
      ...prev,
      assignmentResult: result,
      ...(statusResult !== undefined ? { statusResult } : {}),
      error: null,
      isRetryableError: false,
    }))
    routingService.setPremiumAssigned(true)
    routingService.setPremiumInstanceId(result.instance_id_hash ?? null)
    // Dedicated by definition (only reached for !is_shared), but set explicitly
    // so the upgrade from a prior shared assignment clears the shared flag.
    routingService.setPremiumShared(result.is_shared ?? false)
    try {
      const tokenRes = await getBeaconTokenApi()
      beaconTokenRef.current = tokenRes.data.token
    } catch {
      // Non-critical; beacon will fail gracefully.
      // If this was a 502/503, the axios interceptor already
      // handled recovery (setPremiumAssigned(false) + retry).
    }
    setPollInterval(INITIAL_POLL_INTERVAL_MS)
    setPollAttempts(0)
  }

  // Poll runs while unreachable so a backend reassignment is caught; a poll result alone never clears unreachable (only a real response does).
  useEffect(() => {
    if (
      !shouldPoll(
        isPremiumUser,
        isTabLeader,
        state.assignmentResult,
        unreachable.state.instanceUnreachable,
      )
    ) {
      return
    }

    // Shared assignments still burn premium budget — keep polling past the cap
    // so a long migration doesn't strand the UI on a state only a reload can fix.
    const isOnShared = state.assignmentResult?.is_shared === true
    if (pollAttempts >= MAX_POLL_ATTEMPTS && !isOnShared) {
      // eslint-disable-next-line no-console
      console.warn(
        `Max poll attempts (${MAX_POLL_ATTEMPTS}) reached. Stopping polling.`,
      )
      // Reset the attempt guard so the user can retry via page refresh
      // or user gesture without closing the tab entirely.  Covers both
      // the retryable-assign case (assigned:false) and the instance-lost
      // case (assigned:true but status returned null).
      releaseGenerationRef.current += 1
      if (state.assignmentResult) {
        hasAttemptedRef.current = false
        ssRemove(SS_HAS_ATTEMPTED)
        // Allow re-assignment on the next user gesture (click/keydown).
        needsReassignAfterReleaseRef.current = true
      }
      setState((prev) => ({
        ...prev,
        assignmentResult: null,
        error:
          "No premium instance available after extended wait. " +
          "Please try again later or contact support.",
        isRetryableError: false,
      }))
      setPollAttempts(0)
      setPollInterval(INITIAL_POLL_INTERVAL_MS)
      return
    }

    const timeoutId = setTimeout(async () => {
      // Capture release generation before async work. If any release path
      // fires during an await, the generation will have advanced and we
      // bail out instead of resurrecting a released instance.
      const gen = releaseGenerationRef.current

      try {
        // /status reads the canonical assignment row; /assign could return shared even after migration completed.
        const status = await getPremiumStatus()

        // Post-await liveness check: if a release occurred during the
        // status call (autoReleaseOnLogout, cross-tab PREMIUM_RELEASED,
        // explicit release), bail to avoid resurrecting the instance.
        if (releaseGenerationRef.current !== gen) return

        if (status?.error) {
          // eslint-disable-next-line no-console
          console.warn("Premium status poll returned error:", status.error)
          setPollAttempts((prev) => prev + 1)
          setPollInterval((prev) =>
            Math.min(prev * ERROR_BACKOFF_MULTIPLIER, MAX_POLL_INTERVAL_MS),
          )
          return
        }

        const assignment = status?.assignment ?? null

        if (assignment && !assignment.is_shared) {
          // eslint-disable-next-line no-console
          console.log("Premium instance now available:", assignment.instance_id)
          const result = statusToAssignmentResult(
            assignment,
            "Premium instance now available",
          )
          // The hook clears unreachable on an instance_id change; same-id is a no-op — reachability must come from a real response.
          // Also serves as a routing probe — if the dedicated instance is
          // unreachable, the 502/503 handler will flip premiumAssigned off
          // and emit unreachable, causing polling to resume automatically.
          await finalizeDedicatedAssignment(result, status)
        } else {
          if (assignment) {
            setState((prev) => {
              const cur = prev.assignmentResult
              if (
                cur &&
                cur.instance_id === assignment.instance_id &&
                cur.is_shared === assignment.is_shared
              ) {
                return { ...prev, statusResult: status }
              }
              return {
                ...prev,
                assignmentResult: statusToAssignmentResult(
                  assignment,
                  "Premium instance assignment (shared)",
                ),
                statusResult: status,
              }
            })
            // Reached only for a shared assignment (dedicated returns above).
            // Keep the flag consistent with the polled assignment so the
            // teardown gate holds even for a shared state seen only via polling.
            // Refresh the instance hash too: a dedicated→shared transition seen
            // only via polling would otherwise leave the stale dedicated hash,
            // skewing routing telemetry.
            routingService.setPremiumInstanceId(
              assignment.instance_id_hash ?? null,
            )
            routingService.setPremiumShared(true)
          } else {
            setState((prev) => ({ ...prev, statusResult: status }))

            // Re-trigger assignPremiumInstance() when status returns null
            // and we have a stale assignmentResult. Two scenarios:
            //  1. Original assign returned retryable error (assigned:false,
            //     scaling_in_progress) — polling alone can't create one.
            //  2. Instance was successfully assigned but later stopped/
            //     terminated externally (assigned:true, but status now null)
            //     — the tab silently fell back to free tier.
            // In both cases, periodically re-call assign so the backend
            // gets another chance to place the user on a live instance.
            // NOTE: state.assignmentResult is read from the closure and
            // must remain in this effect's dependency array (see
            // deps below) to stay fresh across re-renders.
            const shouldRetriggerAssign = state.assignmentResult != null

            if (
              shouldRetriggerAssign &&
              pollAttempts > 0 &&
              (pollAttempts + 1) % ASSIGN_RETRY_POLL_THRESHOLD === 0
            ) {
              if (retriggerCountRef.current >= MAX_RETRIGGER_ATTEMPTS) {
                // eslint-disable-next-line no-console
                console.warn(
                  `[premium-poll] Re-trigger limit (${MAX_RETRIGGER_ATTEMPTS}) reached, ` +
                    "continuing status-only polling",
                )
              } else if (isRetriggeringRef.current) {
                // eslint-disable-next-line no-console
                console.warn(
                  "[premium-poll] Re-trigger already in-flight, skipping",
                )
              } else {
                isRetriggeringRef.current = true
                retriggerCountRef.current += 1
                // eslint-disable-next-line no-console
                console.log(
                  `[premium-poll] Re-triggering assign after ${pollAttempts + 1} null-status polls ` +
                    `(attempt ${retriggerCountRef.current}/${MAX_RETRIGGER_ATTEMPTS})`,
                )
                try {
                  // Pre-assign liveness re-check
                  if (releaseGenerationRef.current !== gen) return
                  const reassignResult = await assignPremiumInstance()
                  // Post-assign liveness re-check: a release during the
                  // await means the instance was intentionally freed — do
                  // not finalize.
                  if (releaseGenerationRef.current !== gen) return
                  if (reassignResult?.assigned) {
                    // eslint-disable-next-line no-console
                    console.log(
                      "[premium-poll] Re-assign succeeded:",
                      reassignResult.instance_id,
                    )
                    await finalizeDedicatedAssignment(reassignResult)
                    return
                  }
                  // Still retryable or non-retryable — fall through to
                  // continue polling with backoff.
                } catch (retryError) {
                  // eslint-disable-next-line no-console
                  console.warn(
                    "[premium-poll] Re-assign attempt failed:",
                    retryError,
                  )
                  // Fall through to continue polling
                } finally {
                  isRetriggeringRef.current = false
                }
              }
            }
          }
          // eslint-disable-next-line no-console
          console.warn(
            "Still on temporary instance, will retry with backoff...",
          )
          // Increment for shared too — a changing dep re-runs the effect and
          // reschedules the next poll once pollInterval saturates at the cap.
          // The MAX_POLL_ATTEMPTS stop still excludes shared (!isOnShared), and
          // the re-trigger check runs only in the null-assignment branch.
          setPollAttempts((prev) => prev + 1)
          // Exponential backoff capped at MAX_POLL_INTERVAL_MS
          setPollInterval((prev) =>
            Math.min(prev * BACKOFF_MULTIPLIER, MAX_POLL_INTERVAL_MS),
          )
        }
      } catch (error) {
        // eslint-disable-next-line no-console
        console.warn("Error polling for premium instance:", error)
        setPollAttempts((prev) => prev + 1)
        // More aggressive backoff on errors
        setPollInterval((prev) =>
          Math.min(prev * ERROR_BACKOFF_MULTIPLIER, MAX_POLL_INTERVAL_MS),
        )
      }
    }, pollInterval)

    return () => clearTimeout(timeoutId)
  }, [
    isPremiumUser,
    isTabLeader,
    state.assignmentResult,
    unreachable.state.instanceUnreachable,
    pollInterval,
    pollAttempts,
  ])

  // Handle browser close/refresh for premium users
  useEffect(() => {
    if (!isPremiumUser || !currentUser) return

    const handleBeforeUnload = () => {
      if (state.assignmentResult?.instance_id && beaconTokenRef.current) {
        const blob = new Blob(
          [JSON.stringify({ token: beaconTokenRef.current })],
          { type: BEACON_CONTENT_TYPE },
        )
        navigator.sendBeacon(BEACON_RELEASE_URL, blob)
      }
    }

    window.addEventListener("beforeunload", handleBeforeUnload)
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload)
    }
  }, [isPremiumUser, currentUser, state.assignmentResult])

  const contextValue: PremiumAssignmentContextType = {
    ...state,
    // Override state.isPremiumUser (mirror-effect-driven) with the
    // synchronously-computed value derived from currentUser. Closes the
    // logout race where the mirror effect hasn't propagated yet on a
    // same-tab sign-out → sign-in flow, causing useLogout's gate to bail
    // with stale state.isPremiumUser=false
    isPremiumUser,
    assign,
    release,
    getStatus,
    updateRoutingInfo,
    autoReleaseOnLogout,
    dismissInactivityWarning,
    recordActivity,
    unreachable,
  }

  return (
    <PremiumAssignmentContext.Provider value={contextValue}>
      {children}
    </PremiumAssignmentContext.Provider>
  )
}
