import { useCallback, useEffect, useReducer, useRef } from "react"

import {
  logPremiumUiEvent,
  PremiumAssignmentResult,
} from "api/premium/PremiumAssignmentApi"
import {
  DEDICATED_HANDOFF_GRACE_MS,
  LS_UNREACHABLE_SNAPSHOT,
  MAX_FAILED_PROBES,
  UNREACHABLE_SNAPSHOT_TTL_MS,
} from "contexts/premium/unreachableConstants"
import {
  INITIAL_UNREACHABLE_STATE,
  UnreachableMachineState,
  UnreachableSnapshot,
  computeNextProbeDelayMs,
  computeProbeFailure,
  shouldClearUnreachableForAssignment,
  shouldHydrateFromSnapshot,
  unreachableMachineReducer,
} from "contexts/premium/unreachableMachine"
import { tabSync } from "utils/crossTabSync"
import { routingService } from "utils/routing/RoutingService"

function lsReadUnreachableSnapshot(): UnreachableSnapshot | null {
  try {
    const raw = localStorage.getItem(LS_UNREACHABLE_SNAPSHOT)
    if (!raw) return null
    const snap = JSON.parse(raw) as UnreachableSnapshot
    if (
      typeof snap?.updated_at !== "number" ||
      Date.now() - snap.updated_at > UNREACHABLE_SNAPSHOT_TTL_MS
    ) {
      return null
    }
    return snap
  } catch {
    return null
  }
}

function lsWriteUnreachableSnapshot(
  snap: Omit<UnreachableSnapshot, "updated_at"> | null,
): void {
  try {
    if (snap == null) {
      localStorage.removeItem(LS_UNREACHABLE_SNAPSHOT)
    } else {
      localStorage.setItem(
        LS_UNREACHABLE_SNAPSHOT,
        JSON.stringify({ ...snap, updated_at: Date.now() }),
      )
    }
  } catch {
    // localStorage unavailable (private browsing / quota exceeded)
  }
}

export interface UseInstanceUnreachableMachineArgs {
  assignment: PremiumAssignmentResult | null
  isTabLeader: boolean
}

export interface InstanceUnreachableHandle {
  state: UnreachableMachineState
  retryProbe: () => void
  // Clears the refs the reducer doesn't own (hydrated, snapshot, prev-instance). Use on explicit release/logout.
  reset: () => void
}

export function useInstanceUnreachableMachine({
  assignment,
  isTabLeader,
}: UseInstanceUnreachableMachineArgs): InstanceUnreachableHandle {
  const [state, dispatch] = useReducer(
    unreachableMachineReducer,
    INITIAL_UNREACHABLE_STATE,
  )

  // Refs written synchronously from listeners to avoid pre-commit-state races; mirrored from reducer state in the effect below.
  const assignmentRef = useRef<PremiumAssignmentResult | null>(null)
  const unreachableRef = useRef(false)
  const unreachableSinceRef = useRef<number | null>(null)
  const failedProbesRef = useRef(0)
  const probingRef = useRef(false)
  const hydratedFromSnapshotRef = useRef(false)
  // Gate for snapshot writes — a fresh tab must not wipe a peer's snapshot before hydrating.
  const hasEverBeenUnreachableRef = useRef(false)
  // Last dedicated instance_id — lets the effect below detect a reassignment to a different instance.
  const prevDedicatedInstanceIdRef = useRef<string | undefined>(undefined)
  // Timestamp of the most recent transition onto a dedicated instance (initial
  // assignment, shared → dedicated migration, or reassignment). Opens the
  // warm-up grace window during which transient 5xx are suppressed.
  const dedicatedSinceRef = useRef<number | null>(null)

  // Consolidated state → refs mirror (one effect for three refs).
  useEffect(() => {
    unreachableRef.current = state.instanceUnreachable
    unreachableSinceRef.current = state.unreachableSince
    failedProbesRef.current = state.failedProbes
  }, [state])

  // Clear unreachable on non-dedicated transitions or dedicated reassignment onto a different instance.
  useEffect(() => {
    assignmentRef.current = assignment
    const isDedicated = !!assignment?.assigned && !assignment.is_shared

    if (shouldClearUnreachableForAssignment(assignment)) {
      prevDedicatedInstanceIdRef.current = undefined
      dedicatedSinceRef.current = null
      if (unreachableRef.current || probingRef.current) {
        unreachableRef.current = false
        probingRef.current = false
        failedProbesRef.current = 0
        dispatch({ type: "CLEAR" })
      }
      return
    }

    if (
      isDedicated &&
      prevDedicatedInstanceIdRef.current !== undefined &&
      prevDedicatedInstanceIdRef.current !== assignment?.instance_id
    ) {
      unreachableRef.current = false
      probingRef.current = false
      failedProbesRef.current = 0
      dispatch({ type: "CLEAR" })
      routingService.setPremiumAssigned(true)
      // Reassignment onto a different dedicated instance — fresh grace.
      dedicatedSinceRef.current = Date.now()
      routingService.startPremiumWarmup()
    } else if (
      isDedicated &&
      prevDedicatedInstanceIdRef.current === undefined
    ) {
      // First dedicated transition: initial sign-in, shared→dedicated migration,
      // or reload/new-tab onto an existing instance.
      // Co-arm the axios warm-up window with this grace (lock-step): on
      // reload/new-tab the hash is unchanged (hydrated from localStorage), so
      // setPremiumInstanceId does not arm axios by itself. Without co-arming, a
      // transient 5xx tears routing down while this grace suppresses the
      // unreachable event — stranding routing with no probe to recover it.
      dedicatedSinceRef.current = Date.now()
      routingService.startPremiumWarmup()
    }
    prevDedicatedInstanceIdRef.current = assignment?.instance_id
  }, [assignment])

  // routingService listeners — emit peer broadcasts, log telemetry.
  useEffect(() => {
    const unsubUnreachable = routingService.onPremiumUnreachable((detail) => {
      const a = assignmentRef.current
      if (!a?.assigned || a.is_shared) {
        probingRef.current = false
        return
      }

      // Stale/out-of-order failure — suppressed at the same watermark the axios
      // teardown choke-point uses (single source of truth in RoutingService).
      if (routingService.isStalePremiumFailure(detail.sentAt)) {
        return
      }

      // Warm-up grace window — absorbs every transient 5xx within
      // DEDICATED_HANDOFF_GRACE_MS of a handoff before flipping unreachable.
      // Kept armed for the full window (not single-shot) so multiple warm-up
      // flaps from a freshly-assigned instance are all suppressed; the window
      // expires naturally once the timestamp ages out.
      if (
        !unreachableRef.current &&
        dedicatedSinceRef.current !== null &&
        Date.now() - dedicatedSinceRef.current < DEDICATED_HANDOFF_GRACE_MS
      ) {
        logPremiumUiEvent("instance_unreachable_warmup_suppressed", {
          instance_id: a.instance_id ?? null,
          url: detail.url ?? null,
          status: detail.status ?? null,
        })
        return
      }

      if (probingRef.current) {
        // PROBING → DEGRADED: probe failed. Consume probe, count failure.
        probingRef.current = false
        const { nextFailed, nextTerminal } = computeProbeFailure(
          failedProbesRef.current,
        )
        failedProbesRef.current = nextFailed
        dispatch({ type: "PROBE_FAILURE" })
        logPremiumUiEvent("instance_probe_failure", {
          instance_id: a.instance_id ?? null,
          failed_probes: nextFailed,
          is_terminal: nextTerminal,
          url: detail.url ?? null,
          status: detail.status ?? null,
        })
        tabSync.broadcast({
          type: "PREMIUM_INSTANCE_PROBE_UPDATE",
          payload: {
            instance_id: a.instance_id ?? null,
            failed_probes: nextFailed,
            is_terminal: nextTerminal,
          },
        })
        return
      }
      if (unreachableRef.current) {
        // DEGRADED + late echo of an already-known failure: noop.
        return
      }

      // Flip ref before dispatch so back-to-back events don't each log a fresh incident.
      unreachableRef.current = true
      const now = Date.now()
      dispatch({ type: "FLIP_TO_UNREACHABLE", since: now })
      logPremiumUiEvent("instance_unreachable", {
        instance_id: a.instance_id ?? null,
        url: detail.url ?? null,
        status: detail.status ?? null,
      })
      tabSync.broadcast({
        type: "PREMIUM_INSTANCE_UNREACHABLE",
        payload: {
          instance_id: a.instance_id ?? null,
          unreachable_since: now,
          failed_probes: failedProbesRef.current,
          is_terminal: failedProbesRef.current >= MAX_FAILED_PROBES,
        },
      })
    })

    const unsubReachable = routingService.onPremiumReachable(() => {
      // The reachable watermark is advanced in RoutingService.emitPremiumReachable
      // (before listeners run), so no local bookkeeping is needed here.
      if (!unreachableRef.current) return
      probingRef.current = false
      unreachableRef.current = false
      failedProbesRef.current = 0
      const a = assignmentRef.current
      const since = unreachableSinceRef.current
      dispatch({ type: "CLEAR" })
      logPremiumUiEvent("instance_reachable", {
        instance_id: a?.instance_id ?? null,
        duration_ms: since != null ? Date.now() - since : null,
      })
      tabSync.broadcast({
        type: "PREMIUM_INSTANCE_REACHABLE",
        payload: { instance_id: a?.instance_id ?? null },
      })
    })

    return () => {
      unsubUnreachable()
      unsubReachable()
    }
  }, [])

  // Peer handlers — apply state locally only, no re-log/re-broadcast (echo prevention).
  useEffect(() => {
    const unsubUnreachable = tabSync.on(
      "PREMIUM_INSTANCE_UNREACHABLE",
      (msg) => {
        const a = assignmentRef.current
        if (!a?.assigned || a.is_shared) return
        if (unreachableRef.current) return
        unreachableRef.current = true
        const payload = (msg.payload ?? {}) as {
          unreachable_since?: number
          failed_probes?: number
          is_terminal?: boolean
        }
        failedProbesRef.current = payload.failed_probes ?? 0
        dispatch({ type: "APPLY_PEER_UNREACHABLE", payload })
        // Peer says instance is bad — stop sending premium headers from this tab too.
        routingService.setPremiumAssigned(false)
      },
    )
    const unsubProbeUpdate = tabSync.on(
      "PREMIUM_INSTANCE_PROBE_UPDATE",
      (msg) => {
        if (!unreachableRef.current) return
        const payload = (msg.payload ?? {}) as {
          failed_probes?: number
          is_terminal?: boolean
        }
        failedProbesRef.current = payload.failed_probes ?? 0
        dispatch({ type: "APPLY_PEER_PROBE_UPDATE", payload })
      },
    )
    const unsubReachable = tabSync.on("PREMIUM_INSTANCE_REACHABLE", () => {
      if (!unreachableRef.current) return
      probingRef.current = false
      unreachableRef.current = false
      failedProbesRef.current = 0
      dispatch({ type: "CLEAR" })
      // A peer tab confirmed reachability — resume premium routing locally.
      routingService.setPremiumAssigned(true)
    })
    return () => {
      unsubUnreachable()
      unsubProbeUpdate()
      unsubReachable()
    }
  }, [])

  // Leader-only snapshot write.
  useEffect(() => {
    if (!isTabLeader) return
    if (state.instanceUnreachable && state.unreachableSince != null) {
      hasEverBeenUnreachableRef.current = true
      lsWriteUnreachableSnapshot({
        instance_id: assignment?.instance_id ?? null,
        unreachable_since: state.unreachableSince,
        failed_probes: state.failedProbes,
        is_terminal: state.isUnreachableTerminal,
      })
    } else if (hasEverBeenUnreachableRef.current) {
      lsWriteUnreachableSnapshot(null)
    }
  }, [
    isTabLeader,
    state.instanceUnreachable,
    state.unreachableSince,
    state.failedProbes,
    state.isUnreachableTerminal,
    assignment?.instance_id,
  ])

  // One-shot snapshot hydration on first dedicated assignment.
  useEffect(() => {
    if (hydratedFromSnapshotRef.current) return
    if (!assignment?.assigned || assignment.is_shared) return
    hydratedFromSnapshotRef.current = true
    const snap = lsReadUnreachableSnapshot()
    if (!shouldHydrateFromSnapshot(assignment, snap)) return
    // Narrow: shouldHydrateFromSnapshot rejects a null snap.
    const applied = snap!
    unreachableRef.current = true
    failedProbesRef.current = applied.failed_probes
    hasEverBeenUnreachableRef.current = true
    dispatch({
      type: "HYDRATE_FROM_SNAPSHOT",
      payload: {
        unreachable_since: applied.unreachable_since,
        failed_probes: applied.failed_probes,
        is_terminal: applied.is_terminal,
      },
    })
    routingService.setPremiumAssigned(false)
  }, [assignment])

  // Half-open circuit re-arm. Does NOT signal recovery — the next request's response does.
  useEffect(() => {
    if (!isTabLeader) return
    if (!state.instanceUnreachable) return
    if (state.isUnreachableTerminal) return
    if (probingRef.current) return

    const delay = computeNextProbeDelayMs(state.failedProbes)
    const timer = setTimeout(() => {
      probingRef.current = true
      routingService.setPremiumAssigned(true)
      logPremiumUiEvent("instance_probe_armed", {
        instance_id: assignmentRef.current?.instance_id ?? null,
        failed_probes: failedProbesRef.current,
        delay_ms: delay,
      })
    }, delay)
    return () => clearTimeout(timer)
  }, [
    isTabLeader,
    state.instanceUnreachable,
    state.isUnreachableTerminal,
    state.failedProbes,
  ])

  // Resets probe budget; reachability still requires a real request.
  const retryProbe = useCallback(() => {
    if (!unreachableRef.current) return
    probingRef.current = true
    failedProbesRef.current = 0
    dispatch({ type: "MANUAL_RETRY" })
    routingService.setPremiumAssigned(true)
    logPremiumUiEvent("instance_unreachable_manual_retry", {
      instance_id: assignmentRef.current?.instance_id ?? null,
    })
  }, [])

  const reset = useCallback(() => {
    unreachableRef.current = false
    unreachableSinceRef.current = null
    failedProbesRef.current = 0
    probingRef.current = false
    hydratedFromSnapshotRef.current = false
    hasEverBeenUnreachableRef.current = false
    prevDedicatedInstanceIdRef.current = undefined
    dedicatedSinceRef.current = null
    dispatch({ type: "CLEAR" })
    lsWriteUnreachableSnapshot(null)
  }, [])

  return { state, retryProbe, reset }
}
