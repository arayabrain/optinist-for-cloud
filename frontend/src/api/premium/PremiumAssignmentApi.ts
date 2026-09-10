/**
 * Premium Assignment API
 *
 * Handles API calls for premium user instance assignment and management.
 */

import { UserTier } from "const/Subscription"
import axios from "utils/axios"

export interface RoutingInfo {
  user_tier: UserTier
  requires_premium_routing: boolean
  routing_headers: Record<string, string>
}

export interface PremiumAssignmentResult {
  message: string
  instance_id?: string
  instance_id_hash?: string
  assigned: boolean
  retry_after?: number
  scaling_in_progress?: boolean
  is_shared?: boolean
  assignment_source?: string
}

export interface PremiumReleaseResult {
  message: string
  released_instance?: string
  released: boolean
}

export interface PremiumAssignment {
  instance_id: string
  instance_id_hash?: string
  assigned_at: string
  status: string
  is_shared: boolean
  assignment_source?: string
}

export interface PremiumStatusResult {
  subscription_type: UserTier
  is_premium: boolean
  assignment: PremiumAssignment | null
  migration_ready?: boolean
  health_status?: string
  error?: string
}

export interface PremiumHeartbeatResult {
  message: string
  updated: boolean
  user_tier: UserTier
  assignment_active: boolean
  activity_update?: boolean
  error?: string
}

/**
 * Get routing information for the current user
 */
export const getRoutingInfo = async (): Promise<RoutingInfo> => {
  const response = await axios.get("/users/me/routing-info")
  return response.data
}

/**
 * Assign current user to a premium instance
 */
export const assignPremiumInstance =
  async (): Promise<PremiumAssignmentResult> => {
    const response = await axios.post("/users/me/premium/assign")
    return response.data
  }

/**
 * Release current user from their premium instance
 */
export const releasePremiumInstance =
  async (): Promise<PremiumReleaseResult> => {
    const response = await axios.delete("/users/me/premium/assign")
    return response.data
  }

/**
 * Get current premium assignment status
 */
export const getPremiumStatus = async (): Promise<PremiumStatusResult> => {
  const response = await axios.get("/users/me/premium/status")
  return response.data
}

/**
 * Send heartbeat to update activity timestamp for premium users
 * Prevents stale assignment cleanup for active users
 */
export const sendPremiumHeartbeat =
  async (): Promise<PremiumHeartbeatResult> => {
    const response = await axios.post("/users/me/premium/heartbeat")
    return response.data
  }

export const getBeaconTokenApi = () =>
  axios.get<{ token: string }>("/users/me/premium/beacon-token")

/**
 * Log a premium UI event to the backend (CloudWatch) for timing correlation.
 */
export const logPremiumUiEvent = async (
  eventType: string,
  details?: Record<string, unknown>,
): Promise<void> => {
  try {
    await axios.post("/users/me/premium/ui-event", {
      event_type: eventType,
      timestamp_ms: Date.now(),
      details: details ?? {},
    })
  } catch {
    // Non-critical logging; swallow errors silently
  }
}
