import {
  INITIAL_PROBE_DELAY_MS,
  MAX_FAILED_PROBES,
  MAX_PROBE_DELAY_MS,
  PROBE_BACKOFF_MULTIPLIER,
} from "contexts/premium/unreachableConstants"

export type AssignmentSnapshot = {
  assigned?: boolean
  is_shared?: boolean
  instance_id?: string
} | null

export type UnreachableSnapshot = {
  instance_id: string | null
  unreachable_since: number
  failed_probes: number
  is_terminal: boolean
  updated_at: number
}

// Also polls while unreachable so we catch a backend-initiated reassignment to shared.
export const shouldPoll = (
  isPremiumUser: boolean,
  isTabLeader: boolean,
  assignment: AssignmentSnapshot,
  instanceUnreachable: boolean,
): boolean => {
  if (!isPremiumUser || !isTabLeader || assignment == null) return false
  const hasDedicatedAndHealthy =
    !!assignment.assigned && !assignment.is_shared && !instanceUnreachable
  return !hasDedicatedAndHealthy
}

export const shouldFlipToUnreachable = (
  assignment: AssignmentSnapshot,
  currentUnreachable: boolean,
): boolean => {
  if (!assignment?.assigned || assignment.is_shared) return false
  return !currentUnreachable
}

export const computeNextProbeDelayMs = (failedProbes: number): number =>
  Math.min(
    INITIAL_PROBE_DELAY_MS *
      Math.pow(PROBE_BACKOFF_MULTIPLIER, Math.max(0, failedProbes)),
    MAX_PROBE_DELAY_MS,
  )

export const hasReachedProbeCap = (failedProbes: number): boolean =>
  failedProbes >= MAX_FAILED_PROBES

export const computeProbeFailure = (
  prevFailed: number,
): { nextFailed: number; nextTerminal: boolean } => {
  const nextFailed = prevFailed + 1
  return { nextFailed, nextTerminal: nextFailed >= MAX_FAILED_PROBES }
}

// Only apply the snapshot when instance_id matches — a snapshot from a prior assignment is stale.
export const shouldHydrateFromSnapshot = (
  assignment: AssignmentSnapshot,
  snap: { instance_id: string | null } | null,
): boolean => {
  if (!snap) return false
  if (!assignment?.assigned || assignment.is_shared) return false
  return snap.instance_id === (assignment.instance_id ?? null)
}

// Unreachable tracking is meaningless once the assignment is no longer dedicated.
export const shouldClearUnreachableForAssignment = (
  assignment: AssignmentSnapshot,
): boolean => {
  const isDedicated = !!assignment?.assigned && !assignment.is_shared
  return !isDedicated
}

// --- Unreachable state machine (reducer) ---

export interface UnreachableMachineState {
  instanceUnreachable: boolean
  unreachableSince: number | null
  failedProbes: number
  // Probe budget exhausted — only manual retry or backend reassignment recovers.
  isUnreachableTerminal: boolean
}

export const INITIAL_UNREACHABLE_STATE: UnreachableMachineState = {
  instanceUnreachable: false,
  unreachableSince: null,
  failedProbes: 0,
  isUnreachableTerminal: false,
}

export type UnreachableMachineAction =
  | { type: "CLEAR" }
  | { type: "FLIP_TO_UNREACHABLE"; since: number }
  | { type: "PROBE_FAILURE" }
  | { type: "MANUAL_RETRY" }
  | {
      type: "APPLY_PEER_UNREACHABLE"
      payload: {
        unreachable_since?: number
        failed_probes?: number
        is_terminal?: boolean
      }
    }
  | {
      type: "APPLY_PEER_PROBE_UPDATE"
      payload: { failed_probes?: number; is_terminal?: boolean }
    }
  | {
      type: "HYDRATE_FROM_SNAPSHOT"
      payload: {
        unreachable_since: number
        failed_probes: number
        is_terminal: boolean
      }
    }

export const unreachableMachineReducer = (
  state: UnreachableMachineState,
  action: UnreachableMachineAction,
): UnreachableMachineState => {
  switch (action.type) {
    case "CLEAR":
      return state.instanceUnreachable ||
        state.unreachableSince != null ||
        state.failedProbes > 0 ||
        state.isUnreachableTerminal
        ? INITIAL_UNREACHABLE_STATE
        : state
    case "FLIP_TO_UNREACHABLE":
      if (state.instanceUnreachable) return state
      return {
        ...state,
        instanceUnreachable: true,
        unreachableSince: action.since,
      }
    case "PROBE_FAILURE": {
      const { nextFailed, nextTerminal } = computeProbeFailure(
        state.failedProbes,
      )
      return {
        ...state,
        failedProbes: nextFailed,
        isUnreachableTerminal: nextTerminal,
      }
    }
    case "APPLY_PEER_UNREACHABLE": {
      const failed = action.payload.failed_probes ?? 0
      return {
        ...state,
        instanceUnreachable: true,
        unreachableSince: action.payload.unreachable_since ?? Date.now(),
        failedProbes: failed,
        isUnreachableTerminal: action.payload.is_terminal ?? false,
      }
    }
    case "APPLY_PEER_PROBE_UPDATE":
      return {
        ...state,
        failedProbes: action.payload.failed_probes ?? 0,
        isUnreachableTerminal: action.payload.is_terminal ?? false,
      }
    case "HYDRATE_FROM_SNAPSHOT":
      return {
        instanceUnreachable: true,
        unreachableSince: action.payload.unreachable_since,
        failedProbes: action.payload.failed_probes,
        isUnreachableTerminal: action.payload.is_terminal,
      }
    case "MANUAL_RETRY":
      // Reset probe budget; reachability still requires a real request.
      if (!state.instanceUnreachable) return state
      return {
        ...state,
        failedProbes: 0,
        isUnreachableTerminal: false,
      }
    default:
      return state
  }
}
