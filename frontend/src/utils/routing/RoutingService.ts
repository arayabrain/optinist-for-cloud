/**
 * Routing Service for Premium User ALB Header Management
 *
 * Handles the logic for managing non-reversible routing IDs issued by the backend.
 * The backend generates cryptographically secure routing IDs from user UIDs using
 * HMAC-SHA256, which cannot be forged or reverse-engineered.
 *
 * Security Flow:
 * 1. Backend validates Firebase JWT authentication
 * 2. Backend generates non-reversible routing_id from UID (HMAC-SHA256)
 * 3. Backend sends routing_id in X-Routing-ID response header
 * 4. Frontend stores routing_id and includes it in subsequent requests
 * 5. ALB routes based on routing_id, backend validates against JWT UID
 *
 * Privacy: User UID is never exposed to the client, only the opaque routing_id
 */

import { UserDTO } from "api/users/UsersApiDTO"
import {
  PlanName,
  RoutingHeaders,
  SubscriptionStatus,
  UserTier,
} from "const/Subscription"

export interface RoutingInfo {
  user_tier: UserTier
  requires_premium_routing: boolean
  routing_headers: Record<string, string>
}

export interface PremiumAssignmentResult {
  message: string
  instance_id?: string
  assigned: boolean
  retry_after?: number
  scaling_in_progress?: boolean
}

export interface PremiumUnreachableDetail {
  url?: string
  status?: number
  // Timestamp of when the request was sent — listeners use it to drop failures that predate a newer success.
  sentAt?: number
}

export type PremiumUnreachableListener = (
  detail: PremiumUnreachableDetail,
) => void

export interface PremiumReachableDetail {
  url?: string
  status?: number
  sentAt?: number
}

export type PremiumReachableListener = (detail: PremiumReachableDetail) => void

export class RoutingService {
  private routingInfo: RoutingInfo | null = null
  private routingToken: string | null = null
  private storedTier: UserTier | null = null
  private premiumAssigned: boolean = false
  private premiumInstanceId: string | null = null
  // Whether the current assignment is a shared (pool) instance. Shared has no
  // dedicated-only recovery (state machine / probe), so the teardown choke-point
  // must not permanently downgrade it — see tearDownPremiumRoutingUnlessWarmup.
  private premiumShared: boolean = false
  // Warm-up grace window (epoch ms) after a fresh dedicated assignment.
  private premiumWarmupUntil: number | null = null
  // Monotonic sentAt of the last response confirmed to come from the assigned
  // instance. A failure whose request was sent before this is a stale/out-of-order
  // echo; the teardown choke-point and the state machine use it to ignore such
  // failures instead of tearing routing down.
  private lastReachableSentAt = 0
  private lastFetch: number = 0
  private readonly CACHE_DURATION = 5 * 60 * 1000 // 5 minutes
  // Warm-up grace duration. Intentionally longer than DEDICATED_HANDOFF_GRACE_MS
  // (15000 ms, contexts/premium/unreachableConstants) so this window CONTAINS the
  // machine's grace. Arming sources:
  //   - fresh/changed instance: setPremiumInstanceId arms synchronously (T0)
  //   - every first dedicated transition (incl. reload/new-tab same instance):
  //     the machine co-arms via startPremiumWarmup in its useEffect (T0+Δ)
  // Equal durations would leave a tail [T0+15000, T0+Δ+15000] where teardown is no
  // longer suppressed here but the machine still suppresses the unreachable event —
  // stranding premium routing. Do NOT shrink this back to equality.
  private readonly PREMIUM_WARMUP_GRACE_MS = 16000
  private readonly STORAGE_KEY = "routing_id"
  private readonly TIER_STORAGE_KEY = "routing_tier"
  private readonly PREMIUM_ASSIGNED_KEY = "premium_assigned"
  private readonly PREMIUM_INSTANCE_ID_KEY = "premium_instance_id"
  private readonly PREMIUM_SHARED_KEY = "premium_shared"
  private unreachableListeners: Set<PremiumUnreachableListener> = new Set()
  private reachableListeners: Set<PremiumReachableListener> = new Set()

  constructor() {
    // Load token and tier from localStorage on initialization
    this.loadTokenFromStorage()
    this.loadTierFromStorage()
    this.loadPremiumAssignedFromStorage()
    this.loadPremiumInstanceIdFromStorage()
    this.loadPremiumSharedFromStorage()
  }

  /**
   * Whether the user has active premium routing credentials in memory.
   * Both premiumAssigned (confirmed by /assign) and routingToken (seeded
   * from the X-Routing-ID response header) must be present.
   *
   * Single source of truth consumed by getRoutingHeaders() and
   * requiresPremiumRouting() — keeps the two in sync by construction.
   */
  private hasActiveRoutingCredentials(): boolean {
    return !!this.routingToken && this.premiumAssigned
  }

  /**
   * Get routing headers for the current user request
   * Returns the backend-issued non-reversible routing ID and user tier
   */
  getRoutingHeaders(): Record<string, string> {
    if (!this.hasActiveRoutingCredentials()) {
      return {}
    }

    const headers: Record<string, string> = {
      // Safe: hasActiveRoutingCredentials() guarantees routingToken is non-null
      [RoutingHeaders.ROUTING_ID]: this.routingToken!,
    }

    // Use routingInfo.user_tier if available, fall back to stored tier
    const tier = this.routingInfo?.user_tier ?? this.storedTier
    if (tier) {
      headers[RoutingHeaders.USER_TIER] = tier
    }

    return headers
  }

  /**
   * Update routing ID from backend response header
   * Called by axios response interceptor when X-Routing-ID header is present
   */
  updateRoutingToken(token: string): void {
    this.routingToken = token
    this.saveTokenToStorage(token)
  }

  /**
   * Update routing information for a user.
   * Note: This maintains user tier info but no longer sets client-controlled headers.
   * For non-premium users, also clears stale premiumAssigned and premiumInstanceId
   * to prevent downgrade state leaks.
   */
  updateRoutingInfo(user: UserDTO): void {
    const isPremium = this.isPremiumUser(user)
    const userTier = isPremium ? UserTier.PREMIUM : UserTier.FREE

    this.routingInfo = {
      user_tier: userTier,
      requires_premium_routing: isPremium,
      routing_headers: {}, // No longer client-controlled
    }

    // Persist tier to localStorage
    this.storedTier = userTier
    this.saveTierToStorage(userTier)

    // When the authoritative source (/users/me) says the user is not
    // premium, clear any stale assignment state left in localStorage.
    // This prevents a downgraded user from retaining premiumAssigned=true,
    // which would cause requiresPremiumRouting() and getRoutingHeaders()
    // to behave as if the user is still premium (affects the logout
    // free-user cleanup path in AuthUtils and the 503 fallback gate).
    if (!isPremium) {
      this.setPremiumAssigned(false)
      this.setPremiumInstanceId(null)
      this.setPremiumShared(false)
      // Reset the reachable watermark alongside clearRoutingInfo / resetForRelease
      // so all three "premium goes away" paths leave watermark state consistent.
      this.lastReachableSentAt = 0
    }

    this.lastFetch = Date.now()
  }

  /**
   * Clear routing information (on logout)
   */
  clearRoutingInfo(): void {
    this.routingInfo = null
    this.routingToken = null
    this.storedTier = null
    this.premiumAssigned = false
    this.premiumInstanceId = null
    this.premiumShared = false
    this.premiumWarmupUntil = null
    this.lastReachableSentAt = 0
    this.lastFetch = 0
    this.clearTokenFromStorage()
    this.clearTierFromStorage()
    this.clearPremiumAssignedFromStorage()
    this.clearPremiumInstanceIdFromStorage()
    this.clearPremiumSharedFromStorage()
  }

  /**
   * Clear only the routing token (not tier, routing info, or premium flags).
   * Used on premium release to prevent stale token reuse on reassignment.
   */
  clearRoutingToken(): void {
    this.routingToken = null
    this.clearTokenFromStorage()
  }

  /**
   * Reset routing state for a premium release.
   * Clears premiumAssigned, premiumInstanceId, and routingToken together.
   * Use this in both same-tab and cross-tab release paths to keep them
   * in sync and prevent (premiumAssigned=true, token=null) deadlocks.
   */
  resetForRelease(): void {
    this.setPremiumAssigned(false)
    this.setPremiumInstanceId(null)
    this.setPremiumShared(false)
    this.clearRoutingToken()
    this.lastReachableSentAt = 0
  }

  /**
   * Set whether premium assignment has been confirmed
   * Controls whether routing headers are actually sent
   */
  setPremiumAssigned(assigned: boolean): void {
    this.premiumAssigned = assigned
    this.savePremiumAssignedToStorage(assigned)
  }

  /**
   * Check if premium assignment is confirmed
   */
  isPremiumAssigned(): boolean {
    return this.premiumAssigned
  }

  /**
   * Set the HMAC hash of the assigned premium instance ID.
   * Used by the axios interceptor to detect ALB fallback responses.
   */
  setPremiumInstanceId(id: string | null): void {
    // Arm the warm-up grace only when moving onto a new/changed dedicated
    // instance — re-confirming the same instance must not keep extending the
    // window, or a genuine late fallback would never surface.
    const changedToNewInstance = !!id && id !== this.premiumInstanceId
    this.premiumInstanceId = id
    if (id) {
      if (changedToNewInstance) {
        this.startPremiumWarmup()
      }
      this.savePremiumInstanceIdToStorage(id)
    } else {
      this.clearPremiumWarmup()
      this.clearPremiumInstanceIdFromStorage()
    }
  }

  /**
   * Get the stored premium instance ID hash
   */
  getPremiumInstanceId(): string | null {
    return this.premiumInstanceId
  }

  /**
   * Set whether the current assignment is a shared (pool) instance.
   * Set alongside the instance ID whenever an assignment is established.
   */
  setPremiumShared(shared: boolean): void {
    this.premiumShared = shared
    this.savePremiumSharedToStorage(shared)
  }

  /**
   * Whether the current assignment is a shared (pool) instance.
   */
  isPremiumShared(): boolean {
    return this.premiumShared
  }

  /**
   * Arm the warm-up grace window (transition onto a new dedicated instance).
   */
  startPremiumWarmup(): void {
    this.premiumWarmupUntil = Date.now() + this.PREMIUM_WARMUP_GRACE_MS
  }

  /**
   * Whether we are still within the dedicated-instance warm-up grace window.
   * axios uses this to suppress the isInstanceMismatch teardown while a
   * freshly-assigned instance is still registering in the ALB target group.
   */
  isWithinPremiumWarmup(): boolean {
    return (
      this.premiumWarmupUntil != null && Date.now() < this.premiumWarmupUntil
    )
  }

  /**
   * Clear the warm-up grace window (release/logout/downgrade).
   */
  clearPremiumWarmup(): void {
    this.premiumWarmupUntil = null
  }

  /**
   * Whether a premium failure is stale — its request was sent before the last
   * response confirmed reachable, so it is an out-of-order echo rather than a
   * live outage. The teardown choke-point and the state machine both consult
   * this so a stale failure never tears premium routing down.
   */
  isStalePremiumFailure(sentAt: number | undefined): boolean {
    return (sentAt ?? Date.now()) < this.lastReachableSentAt
  }

  // Pure notifier — telemetry lives in listeners so tests can emit without side effects.
  onPremiumUnreachable(listener: PremiumUnreachableListener): () => void {
    this.unreachableListeners.add(listener)
    return () => {
      this.unreachableListeners.delete(listener)
    }
  }

  emitPremiumUnreachable(detail: PremiumUnreachableDetail): void {
    this.unreachableListeners.forEach((listener) => {
      try {
        listener(detail)
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn("Premium-unreachable listener threw:", e)
      }
    })
  }

  onPremiumReachable(listener: PremiumReachableListener): () => void {
    this.reachableListeners.add(listener)
    return () => {
      this.reachableListeners.delete(listener)
    }
  }

  emitPremiumReachable(detail: PremiumReachableDetail): void {
    // Advance the reachable watermark before notifying — the teardown
    // choke-point and the state machine read it to suppress stale failures.
    const sentAt = detail.sentAt ?? Date.now()
    if (sentAt > this.lastReachableSentAt) {
      this.lastReachableSentAt = sentAt
    }
    // Re-arm routing at the source of truth for every listener path. Gate on a
    // live premiumInstanceId so a late post-release 200 can't resurrect routing.
    if (this.premiumInstanceId != null && !this.premiumAssigned) {
      this.setPremiumAssigned(true)
    }
    this.reachableListeners.forEach((listener) => {
      try {
        listener(detail)
      } catch (e) {
        // eslint-disable-next-line no-console
        console.warn("Premium-reachable listener threw:", e)
      }
    })
  }

  /**
   * Get current routing ID (for debugging)
   */
  getRoutingToken(): string | null {
    return this.routingToken
  }

  /**
   * Check if user requires premium routing.
   *
   * Returns true when either:
   *  - routingInfo (set by /users/me) indicates premium, OR
   *  - localStorage-backed credentials are active (survives page reload
   *    even when routingInfo is null).
   *
   * The second clause delegates to hasActiveRoutingCredentials(), the
   * same predicate used by getRoutingHeaders(), so the 503 fallback
   * gate is aligned with header emission by construction.
   */
  requiresPremiumRouting(): boolean {
    return (
      (this.routingInfo?.requires_premium_routing ?? false) ||
      this.hasActiveRoutingCredentials()
    )
  }

  /**
   * Get current user tier
   */
  getUserTier(): UserTier | null {
    return this.routingInfo?.user_tier ?? this.storedTier ?? null
  }

  /**
   * Check if routing info is stale and needs refresh
   */
  isRoutingInfoStale(): boolean {
    if (!this.routingInfo) return true
    return Date.now() - this.lastFetch > this.CACHE_DURATION
  }

  /**
   * Determine if a user is premium based on subscription info
   */
  private isPremiumUser(user: UserDTO): boolean {
    return (
      user.subscription_plan_name === PlanName.PREMIUM &&
      user.subscription_status === SubscriptionStatus.PREMIUM
    )
  }

  /**
   * Get current routing information
   */
  getCurrentRoutingInfo(): RoutingInfo | null {
    return this.routingInfo
  }

  /**
   * Load routing ID from localStorage
   */
  private loadTokenFromStorage(): void {
    try {
      const token = localStorage.getItem(this.STORAGE_KEY)
      if (token) {
        this.routingToken = token
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to load routing ID from localStorage:", e)
    }
  }

  /**
   * Save routing ID to localStorage
   */
  private saveTokenToStorage(token: string): void {
    try {
      localStorage.setItem(this.STORAGE_KEY, token)
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to save routing ID to localStorage:", e)
    }
  }

  /**
   * Clear routing ID from localStorage
   */
  private clearTokenFromStorage(): void {
    try {
      localStorage.removeItem(this.STORAGE_KEY)
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to clear routing ID from localStorage:", e)
    }
  }

  /**
   * Load user tier from localStorage
   */
  private loadTierFromStorage(): void {
    try {
      const tier = localStorage.getItem(this.TIER_STORAGE_KEY)
      if (tier && this.isValidUserTier(tier)) {
        this.storedTier = tier as UserTier
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to load user tier from localStorage:", e)
    }
  }

  /**
   * Save user tier to localStorage
   */
  private saveTierToStorage(tier: UserTier): void {
    try {
      localStorage.setItem(this.TIER_STORAGE_KEY, tier)
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to save user tier to localStorage:", e)
    }
  }

  /**
   * Clear user tier from localStorage
   */
  private clearTierFromStorage(): void {
    try {
      localStorage.removeItem(this.TIER_STORAGE_KEY)
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to clear user tier from localStorage:", e)
    }
  }

  /**
   * Validate that a string is a valid UserTier value
   */
  private isValidUserTier(value: string): value is UserTier {
    return value === UserTier.PREMIUM || value === UserTier.FREE
  }

  /**
   * Load premium assigned flag from localStorage
   */
  private loadPremiumAssignedFromStorage(): void {
    try {
      const value = localStorage.getItem(this.PREMIUM_ASSIGNED_KEY)
      this.premiumAssigned = value === "true"
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to load premium assigned from localStorage:", e)
    }
  }

  /**
   * Save premium assigned flag to localStorage
   */
  private savePremiumAssignedToStorage(assigned: boolean): void {
    try {
      localStorage.setItem(this.PREMIUM_ASSIGNED_KEY, String(assigned))
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to save premium assigned to localStorage:", e)
    }
  }

  /**
   * Clear premium assigned flag from localStorage
   */
  private clearPremiumAssignedFromStorage(): void {
    try {
      localStorage.removeItem(this.PREMIUM_ASSIGNED_KEY)
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to clear premium assigned from localStorage:", e)
    }
  }

  private loadPremiumInstanceIdFromStorage(): void {
    try {
      const id = localStorage.getItem(this.PREMIUM_INSTANCE_ID_KEY)
      if (id) {
        this.premiumInstanceId = id
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to load premium instance ID from localStorage:", e)
    }
  }

  private savePremiumInstanceIdToStorage(id: string): void {
    try {
      localStorage.setItem(this.PREMIUM_INSTANCE_ID_KEY, id)
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to save premium instance ID to localStorage:", e)
    }
  }

  private clearPremiumInstanceIdFromStorage(): void {
    try {
      localStorage.removeItem(this.PREMIUM_INSTANCE_ID_KEY)
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to clear premium instance ID from localStorage:", e)
    }
  }

  private loadPremiumSharedFromStorage(): void {
    try {
      this.premiumShared =
        localStorage.getItem(this.PREMIUM_SHARED_KEY) === "true"
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to load premium shared from localStorage:", e)
    }
  }

  private savePremiumSharedToStorage(shared: boolean): void {
    try {
      localStorage.setItem(this.PREMIUM_SHARED_KEY, String(shared))
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to save premium shared to localStorage:", e)
    }
  }

  private clearPremiumSharedFromStorage(): void {
    try {
      localStorage.removeItem(this.PREMIUM_SHARED_KEY)
    } catch (e) {
      // eslint-disable-next-line no-console
      console.warn("Failed to clear premium shared from localStorage:", e)
    }
  }
}

// Export singleton instance
export const routingService = new RoutingService()
export default routingService
