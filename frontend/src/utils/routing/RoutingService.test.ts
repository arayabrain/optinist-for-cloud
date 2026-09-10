import {
  describe,
  test,
  expect,
  beforeEach,
  afterEach,
  jest,
} from "@jest/globals"

import { UserDTO } from "api/users/UsersApiDTO"
import {
  PlanName,
  RoutingHeaders,
  SubscriptionStatus,
  UserTier,
} from "const/Subscription"
import { RoutingService } from "utils/routing/RoutingService"

// Mock localStorage
const localStorageMock = (() => {
  let store: Record<string, string> = {}
  return {
    getItem: (key: string) => store[key] || null,
    setItem: (key: string, value: string) => {
      store[key] = value
    },
    removeItem: (key: string) => {
      delete store[key]
    },
    clear: () => {
      store = {}
    },
  }
})()

Object.defineProperty(global, "localStorage", { value: localStorageMock })

describe("RoutingService", () => {
  let routingService: RoutingService

  const createPremiumUser = (overrides?: Partial<UserDTO>): UserDTO => ({
    uid: "test-uid-123",
    email: "premium@test.com",
    data_usage: 0,
    subscription_plan_name: PlanName.PREMIUM,
    subscription_status: SubscriptionStatus.PREMIUM,
    ...overrides,
  })

  const createFreeUser = (overrides?: Partial<UserDTO>): UserDTO => ({
    uid: "test-uid-456",
    email: "free@test.com",
    data_usage: 0,
    subscription_plan_name: PlanName.FREE,
    subscription_status: SubscriptionStatus.FREE,
    ...overrides,
  })

  beforeEach(() => {
    localStorageMock.clear()
    routingService = new RoutingService()
  })

  describe("getRoutingHeaders", () => {
    test("should return empty object when no routing token is set", () => {
      const headers = routingService.getRoutingHeaders()
      expect(headers).toEqual({})
    })

    test("should return empty object when token is set but premiumAssigned is false", () => {
      routingService.updateRoutingToken("abc123def456")

      const headers = routingService.getRoutingHeaders()

      expect(headers).toEqual({})
    })

    test("should return X-Routing-ID header when token is set and premiumAssigned is true", () => {
      routingService.updateRoutingToken("abc123def456")
      routingService.setPremiumAssigned(true)

      const headers = routingService.getRoutingHeaders()

      expect(headers[RoutingHeaders.ROUTING_ID]).toBe("abc123def456")
    })

    test("should include X-User-Tier header for premium users", () => {
      const premiumUser = createPremiumUser()
      routingService.updateRoutingToken("abc123def456")
      routingService.updateRoutingInfo(premiumUser)
      routingService.setPremiumAssigned(true)

      const headers = routingService.getRoutingHeaders()

      expect(headers[RoutingHeaders.ROUTING_ID]).toBe("abc123def456")
      expect(headers[RoutingHeaders.USER_TIER]).toBe(UserTier.PREMIUM)
    })

    test("should include X-User-Tier header for free users", () => {
      const freeUser = createFreeUser()
      routingService.updateRoutingToken("abc123def456")
      routingService.updateRoutingInfo(freeUser)
      // Note: updateRoutingInfo(freeUser) clears premiumAssigned;
      // re-set it here to test header format independently of tier logic.
      routingService.setPremiumAssigned(true)

      const headers = routingService.getRoutingHeaders()

      expect(headers[RoutingHeaders.ROUTING_ID]).toBe("abc123def456")
      expect(headers[RoutingHeaders.USER_TIER]).toBe(UserTier.FREE)
    })

    test("should return both headers required for ALB routing", () => {
      const premiumUser = createPremiumUser()
      routingService.updateRoutingToken("73279292c0867872")
      routingService.updateRoutingInfo(premiumUser)
      routingService.setPremiumAssigned(true)

      const headers = routingService.getRoutingHeaders()

      // Both headers must be present for ALB routing rules to match
      expect(Object.keys(headers)).toContain("X-Routing-ID")
      expect(Object.keys(headers)).toContain("X-User-Tier")
      expect(headers["X-User-Tier"]).toBe("premium")
      expect(headers["X-Routing-ID"]).toBe("73279292c0867872")
    })
  })

  describe("updateRoutingToken", () => {
    test("should store routing token", () => {
      routingService.updateRoutingToken("test-token")

      expect(routingService.getRoutingToken()).toBe("test-token")
    })

    test("should persist token to localStorage", () => {
      routingService.updateRoutingToken("persisted-token")

      expect(localStorageMock.getItem("routing_id")).toBe("persisted-token")
    })
  })

  describe("updateRoutingInfo", () => {
    test("should identify premium user correctly", () => {
      const premiumUser = createPremiumUser()
      routingService.updateRoutingInfo(premiumUser)

      expect(routingService.getUserTier()).toBe(UserTier.PREMIUM)
      expect(routingService.requiresPremiumRouting()).toBe(true)
    })

    test("should identify free user correctly", () => {
      const freeUser = createFreeUser()
      routingService.updateRoutingInfo(freeUser)

      expect(routingService.getUserTier()).toBe(UserTier.FREE)
      expect(routingService.requiresPremiumRouting()).toBe(false)
    })

    test("should identify Limit Grace user as free", () => {
      const graceUser = createPremiumUser({
        subscription_status: SubscriptionStatus.LIMIT_GRACE,
      })
      routingService.updateRoutingInfo(graceUser)

      expect(routingService.getUserTier()).toBe(UserTier.FREE)
      expect(routingService.requiresPremiumRouting()).toBe(false)
    })

    test("should identify expired premium user as free", () => {
      const expiredUser = createPremiumUser({
        subscription_status: SubscriptionStatus.EXPIRED,
      })
      routingService.updateRoutingInfo(expiredUser)

      expect(routingService.getUserTier()).toBe(UserTier.FREE)
      expect(routingService.requiresPremiumRouting()).toBe(false)
    })

    test("should clear stale premiumAssigned on downgrade (premium → free)", () => {
      // Setup: user was premium and assigned
      routingService.updateRoutingToken("test-token")
      routingService.updateRoutingInfo(createPremiumUser())
      routingService.setPremiumAssigned(true)
      routingService.setPremiumInstanceId("inst-hash")

      // User's subscription expires — /users/me now returns free tier
      routingService.updateRoutingInfo(createFreeUser())

      // Stale assignment state should be cleared
      expect(routingService.isPremiumAssigned()).toBe(false)
      expect(routingService.getPremiumInstanceId()).toBeNull()
      expect(routingService.requiresPremiumRouting()).toBe(false)
      expect(routingService.getRoutingHeaders()).toEqual({})
      // localStorage cleaned up
      expect(localStorageMock.getItem("premium_assigned")).toBe("false")
      expect(localStorageMock.getItem("premium_instance_id")).toBeNull()
    })

    test("should NOT clear premiumAssigned when user is premium", () => {
      routingService.updateRoutingToken("test-token")
      routingService.setPremiumAssigned(true)
      routingService.setPremiumInstanceId("inst-hash")

      routingService.updateRoutingInfo(createPremiumUser())

      // Assignment state preserved for premium users
      expect(routingService.isPremiumAssigned()).toBe(true)
      expect(routingService.getPremiumInstanceId()).toBe("inst-hash")
    })

    test("should clear stale localStorage state after page reload when user has been downgraded", () => {
      // Simulate stale localStorage from a previous premium session
      localStorageMock.setItem("routing_id", "stored-token")
      localStorageMock.setItem("premium_assigned", "true")
      localStorageMock.setItem("premium_instance_id", "old-inst-hash")
      localStorageMock.setItem("routing_tier", "premium")

      // Page reload: constructor loads stale state
      const reloadedService = new RoutingService()
      // Before /users/me returns, stale state makes it look premium
      expect(reloadedService.requiresPremiumRouting()).toBe(true)

      // /users/me returns — user is now free tier (downgraded)
      reloadedService.updateRoutingInfo(createFreeUser())

      // Stale state cleared — correct for a free user
      expect(reloadedService.requiresPremiumRouting()).toBe(false)
      expect(reloadedService.isPremiumAssigned()).toBe(false)
      expect(reloadedService.getPremiumInstanceId()).toBeNull()
      expect(reloadedService.getRoutingHeaders()).toEqual({})
    })
  })

  describe("premiumAssigned gate", () => {
    test("should default to false", () => {
      expect(routingService.isPremiumAssigned()).toBe(false)
    })

    test("should be set to true via setPremiumAssigned", () => {
      routingService.setPremiumAssigned(true)
      expect(routingService.isPremiumAssigned()).toBe(true)
    })

    test("should persist to localStorage", () => {
      routingService.setPremiumAssigned(true)
      expect(localStorageMock.getItem("premium_assigned")).toBe("true")

      routingService.setPremiumAssigned(false)
      expect(localStorageMock.getItem("premium_assigned")).toBe("false")
    })

    test("should load from localStorage on construction", () => {
      localStorageMock.setItem("premium_assigned", "true")
      const newService = new RoutingService()
      expect(newService.isPremiumAssigned()).toBe(true)
    })

    test("should gate routing headers - no headers when false", () => {
      routingService.updateRoutingToken("test-token")
      routingService.updateRoutingInfo(createPremiumUser())

      expect(routingService.getRoutingHeaders()).toEqual({})
    })

    test("should gate routing headers - headers returned when true", () => {
      routingService.updateRoutingToken("test-token")
      routingService.updateRoutingInfo(createPremiumUser())
      routingService.setPremiumAssigned(true)

      const headers = routingService.getRoutingHeaders()
      expect(headers[RoutingHeaders.ROUTING_ID]).toBe("test-token")
      expect(headers[RoutingHeaders.USER_TIER]).toBe(UserTier.PREMIUM)
    })
  })

  describe("requiresPremiumRouting", () => {
    test("should return true when routingInfo indicates premium", () => {
      routingService.updateRoutingInfo(createPremiumUser())
      expect(routingService.requiresPremiumRouting()).toBe(true)
    })

    test("should return false when routingInfo indicates free", () => {
      routingService.updateRoutingInfo(createFreeUser())
      expect(routingService.requiresPremiumRouting()).toBe(false)
    })

    test("should return true when premiumAssigned and routingToken are set (no routingInfo)", () => {
      // Simulates page reload: localStorage state survives but routingInfo is null
      localStorageMock.setItem("routing_id", "stored-token")
      localStorageMock.setItem("premium_assigned", "true")

      const newService = new RoutingService()

      expect(newService.requiresPremiumRouting()).toBe(true)
    })

    test("should return false when premiumAssigned but no routingToken", () => {
      localStorageMock.setItem("premium_assigned", "true")

      const newService = new RoutingService()

      expect(newService.requiresPremiumRouting()).toBe(false)
    })

    test("should return false when routingToken exists but premiumAssigned is false", () => {
      localStorageMock.setItem("routing_id", "stored-token")

      const newService = new RoutingService()

      expect(newService.requiresPremiumRouting()).toBe(false)
    })

    test("should return false after clearRoutingInfo", () => {
      routingService.updateRoutingInfo(createPremiumUser())
      routingService.updateRoutingToken("test-token")
      routingService.setPremiumAssigned(true)

      routingService.clearRoutingInfo()

      expect(routingService.requiresPremiumRouting()).toBe(false)
    })

    test("should return false after resetForRelease (premiumAssigned cleared)", () => {
      routingService.updateRoutingToken("test-token")
      routingService.setPremiumAssigned(true)

      routingService.resetForRelease()

      expect(routingService.requiresPremiumRouting()).toBe(false)
    })

    test("should stay aligned with getRoutingHeaders in both directions", () => {
      // After page reload with localStorage state
      localStorageMock.setItem("routing_id", "stored-token")
      localStorageMock.setItem("premium_assigned", "true")
      localStorageMock.setItem("routing_tier", "premium")

      const newService = new RoutingService()

      // Both active: headers are emitted and the 503 fallback gate is open.
      expect(newService.getRoutingHeaders()).toEqual({
        [RoutingHeaders.ROUTING_ID]: "stored-token",
        [RoutingHeaders.USER_TIER]: UserTier.PREMIUM,
      })
      expect(newService.requiresPremiumRouting()).toBe(true)

      // Both inactive: dropping the assignment closes both at once.
      newService.setPremiumAssigned(false)

      expect(newService.getRoutingHeaders()).toEqual({})
      expect(newService.requiresPremiumRouting()).toBe(false)
    })
  })

  describe("clearRoutingToken", () => {
    test("should clear token and localStorage but preserve tier and premiumAssigned", () => {
      const premiumUser = createPremiumUser()
      routingService.updateRoutingToken("test-token")
      routingService.updateRoutingInfo(premiumUser)
      routingService.setPremiumAssigned(true)
      routingService.setPremiumInstanceId("inst-hash")

      routingService.clearRoutingToken()

      expect(routingService.getRoutingToken()).toBeNull()
      expect(localStorageMock.getItem("routing_id")).toBeNull()
      // Tier, premiumAssigned, and instanceId must be preserved
      expect(routingService.getUserTier()).toBe(UserTier.PREMIUM)
      expect(routingService.isPremiumAssigned()).toBe(true)
      expect(routingService.getPremiumInstanceId()).toBe("inst-hash")
      expect(localStorageMock.getItem("routing_tier")).toBe("premium")
      expect(localStorageMock.getItem("premium_assigned")).toBe("true")
    })

    test("should cause getRoutingHeaders to return empty (token is null)", () => {
      routingService.updateRoutingToken("test-token")
      routingService.updateRoutingInfo(createPremiumUser())
      routingService.setPremiumAssigned(true)

      routingService.clearRoutingToken()

      // Even though premiumAssigned is true, token is null → empty headers
      expect(routingService.getRoutingHeaders()).toEqual({})
    })
  })

  describe("resetForRelease", () => {
    test("should clear premiumAssigned, instanceId, and token together", () => {
      const premiumUser = createPremiumUser()
      routingService.updateRoutingToken("test-token")
      routingService.updateRoutingInfo(premiumUser)
      routingService.setPremiumAssigned(true)
      routingService.setPremiumInstanceId("inst-hash")

      routingService.resetForRelease()

      expect(routingService.isPremiumAssigned()).toBe(false)
      expect(routingService.getPremiumInstanceId()).toBeNull()
      expect(routingService.getRoutingToken()).toBeNull()
      expect(localStorageMock.getItem("routing_id")).toBeNull()
      expect(localStorageMock.getItem("premium_assigned")).toBe("false")
      expect(localStorageMock.getItem("premium_instance_id")).toBeNull()
      // Tier and routingInfo are preserved (user is still premium-subscribed)
      expect(routingService.getUserTier()).toBe(UserTier.PREMIUM)
    })

    test("should make getRoutingHeaders return empty after release", () => {
      routingService.updateRoutingToken("test-token")
      routingService.updateRoutingInfo(createPremiumUser())
      routingService.setPremiumAssigned(true)

      routingService.resetForRelease()

      expect(routingService.getRoutingHeaders()).toEqual({})
    })

    test("should allow re-seeding after release (premiumAssigned=false path)", () => {
      routingService.updateRoutingToken("test-token")
      routingService.setPremiumAssigned(true)

      routingService.resetForRelease()

      // After release, premiumAssigned=false so interceptor can re-seed
      expect(routingService.isPremiumAssigned()).toBe(false)
      // Simulate re-seeding
      routingService.updateRoutingToken("new-token")
      routingService.setPremiumAssigned(true)
      routingService.setPremiumInstanceId("new-hash")

      expect(routingService.getRoutingToken()).toBe("new-token")
      expect(routingService.isPremiumAssigned()).toBe(true)
    })
  })

  describe("clearRoutingInfo", () => {
    test("should clear all routing state including premiumAssigned", () => {
      const premiumUser = createPremiumUser()
      routingService.updateRoutingToken("test-token")
      routingService.updateRoutingInfo(premiumUser)
      routingService.setPremiumAssigned(true)

      routingService.clearRoutingInfo()

      expect(routingService.getRoutingToken()).toBeNull()
      expect(routingService.getUserTier()).toBeNull()
      expect(routingService.requiresPremiumRouting()).toBe(false)
      expect(routingService.isPremiumAssigned()).toBe(false)
      expect(routingService.getRoutingHeaders()).toEqual({})
    })

    test("should remove token from localStorage", () => {
      routingService.updateRoutingToken("test-token")
      expect(localStorageMock.getItem("routing_id")).toBe("test-token")

      routingService.clearRoutingInfo()

      expect(localStorageMock.getItem("routing_id")).toBeNull()
    })

    test("should remove premiumAssigned from localStorage", () => {
      routingService.setPremiumAssigned(true)
      expect(localStorageMock.getItem("premium_assigned")).toBe("true")

      routingService.clearRoutingInfo()

      expect(localStorageMock.getItem("premium_assigned")).toBeNull()
    })
  })

  describe("localStorage persistence", () => {
    test("should load token from localStorage on initialization", () => {
      localStorageMock.setItem("routing_id", "stored-token")

      const newService = new RoutingService()

      expect(newService.getRoutingToken()).toBe("stored-token")
    })

    test("should persist user tier to localStorage", () => {
      const premiumUser = createPremiumUser()
      routingService.updateRoutingInfo(premiumUser)

      expect(localStorageMock.getItem("routing_tier")).toBe("premium")
    })

    test("should load tier from localStorage on initialization", () => {
      localStorageMock.setItem("routing_tier", "premium")

      const newService = new RoutingService()

      expect(newService.getUserTier()).toBe("premium")
    })

    test("should include stored tier in headers after page refresh when premiumAssigned", () => {
      localStorageMock.setItem("routing_id", "stored-token")
      localStorageMock.setItem("routing_tier", "premium")
      localStorageMock.setItem("premium_assigned", "true")

      const newService = new RoutingService()
      const headers = newService.getRoutingHeaders()

      expect(headers["X-Routing-ID"]).toBe("stored-token")
      expect(headers["X-User-Tier"]).toBe("premium")
    })

    test("should not include headers after page refresh when premiumAssigned is false", () => {
      localStorageMock.setItem("routing_id", "stored-token")
      localStorageMock.setItem("routing_tier", "premium")

      const newService = new RoutingService()
      const headers = newService.getRoutingHeaders()

      expect(headers).toEqual({})
    })

    test("should ignore invalid tier values from localStorage", () => {
      localStorageMock.setItem("routing_tier", "invalid-tier")

      const newService = new RoutingService()

      expect(newService.getUserTier()).toBeNull()
    })

    test("should remove tier from localStorage on clear", () => {
      routingService.updateRoutingInfo(createPremiumUser())
      expect(localStorageMock.getItem("routing_tier")).toBe("premium")

      routingService.clearRoutingInfo()

      expect(localStorageMock.getItem("routing_tier")).toBeNull()
    })
  })

  describe("isRoutingInfoStale", () => {
    test("should return true when no routing info exists", () => {
      expect(routingService.isRoutingInfoStale()).toBe(true)
    })

    test("should return false immediately after updating routing info", () => {
      const premiumUser = createPremiumUser()
      routingService.updateRoutingInfo(premiumUser)

      expect(routingService.isRoutingInfoStale()).toBe(false)
    })
  })

  describe("premium-unreachable listeners", () => {
    test("emit invokes every registered listener with the detail", () => {
      const a = jest.fn()
      const b = jest.fn()
      routingService.onPremiumUnreachable(a)
      routingService.onPremiumUnreachable(b)

      routingService.emitPremiumUnreachable({ url: "/foo", status: 503 })

      expect(a).toHaveBeenCalledWith({ url: "/foo", status: 503 })
      expect(b).toHaveBeenCalledWith({ url: "/foo", status: 503 })
    })

    test("unsubscribe stops further invocations", () => {
      const listener = jest.fn()
      const unsubscribe = routingService.onPremiumUnreachable(listener)

      routingService.emitPremiumUnreachable({ status: 502 })
      unsubscribe()
      routingService.emitPremiumUnreachable({ status: 503 })

      expect(listener).toHaveBeenCalledTimes(1)
    })

    test("a throwing listener does not block the others", () => {
      const good = jest.fn()
      routingService.onPremiumUnreachable(() => {
        throw new Error("boom")
      })
      routingService.onPremiumUnreachable(good)

      routingService.emitPremiumUnreachable({ status: 503 })

      expect(good).toHaveBeenCalled()
    })
  })

  describe("premium-reachable listeners", () => {
    test("emit invokes every registered listener with the detail", () => {
      const a = jest.fn()
      const b = jest.fn()
      routingService.onPremiumReachable(a)
      routingService.onPremiumReachable(b)

      routingService.emitPremiumReachable({ url: "/foo", status: 200 })

      expect(a).toHaveBeenCalledWith({ url: "/foo", status: 200 })
      expect(b).toHaveBeenCalledWith({ url: "/foo", status: 200 })
    })

    test("unsubscribe stops further invocations", () => {
      const listener = jest.fn()
      const unsubscribe = routingService.onPremiumReachable(listener)

      routingService.emitPremiumReachable({ status: 200 })
      unsubscribe()
      routingService.emitPremiumReachable({ status: 200 })

      expect(listener).toHaveBeenCalledTimes(1)
    })

    test("a throwing listener does not block the others", () => {
      const good = jest.fn()
      routingService.onPremiumReachable(() => {
        throw new Error("boom")
      })
      routingService.onPremiumReachable(good)

      routingService.emitPremiumReachable({ status: 200 })

      expect(good).toHaveBeenCalled()
    })

    test("reachable and unreachable listener pools are independent", () => {
      const unreachable = jest.fn()
      const reachable = jest.fn()
      routingService.onPremiumUnreachable(unreachable)
      routingService.onPremiumReachable(reachable)

      routingService.emitPremiumUnreachable({ status: 503 })
      expect(unreachable).toHaveBeenCalledTimes(1)
      expect(reachable).not.toHaveBeenCalled()

      routingService.emitPremiumReachable({ status: 200 })
      expect(reachable).toHaveBeenCalledTimes(1)
      expect(unreachable).toHaveBeenCalledTimes(1)
    })

    test("forwards sentAt on both event types", () => {
      const reachable = jest.fn()
      const unreachable = jest.fn()
      routingService.onPremiumReachable(reachable)
      routingService.onPremiumUnreachable(unreachable)

      routingService.emitPremiumReachable({ status: 200, sentAt: 1111 })
      routingService.emitPremiumUnreachable({ status: 503, sentAt: 2222 })

      expect(reachable).toHaveBeenCalledWith({ status: 200, sentAt: 1111 })
      expect(unreachable).toHaveBeenCalledWith({ status: 503, sentAt: 2222 })
    })
  })

  describe("emitPremiumReachable re-arms premiumAssigned (exit side)", () => {
    test("recovery not routed through the probe still re-arms routing", () => {
      // Teardown turned routing off (concurrent failure) but keeps the instance
      // identity, then a concurrent success emits reachable without going
      // through the half-open probe.
      routingService.setPremiumInstanceId("inst-A")
      routingService.setPremiumAssigned(false)

      routingService.emitPremiumReachable({ status: 200 })

      expect(routingService.isPremiumAssigned()).toBe(true)
    })

    test("re-arm holds even when a listener throws", () => {
      routingService.setPremiumInstanceId("inst-A")
      routingService.setPremiumAssigned(false)
      routingService.onPremiumReachable(() => {
        throw new Error("boom")
      })

      routingService.emitPremiumReachable({ status: 200 })

      expect(routingService.isPremiumAssigned()).toBe(true)
    })

    test("re-arm is idempotent when already assigned (probe path)", () => {
      routingService.setPremiumInstanceId("inst-A")
      routingService.setPremiumAssigned(true)

      routingService.emitPremiumReachable({ status: 200 })

      expect(routingService.isPremiumAssigned()).toBe(true)
    })

    test("late post-release reachable does not resurrect routing", () => {
      // A premium request was in flight, then release/logout cleared the
      // instance identity. The late verified 200 must not re-arm routing, or it
      // recreates the (assigned, instanceId=null) desync resetForRelease prevents.
      routingService.setPremiumInstanceId("inst-A")
      routingService.setPremiumAssigned(true)
      routingService.resetForRelease()
      expect(routingService.getPremiumInstanceId()).toBeNull()

      routingService.emitPremiumReachable({ status: 200 })

      expect(routingService.isPremiumAssigned()).toBe(false)
    })
  })

  describe("premiumInstanceId", () => {
    test("setPremiumInstanceId / getPremiumInstanceId round-trip", () => {
      expect(routingService.getPremiumInstanceId()).toBeNull()

      routingService.setPremiumInstanceId("abc123hash")
      expect(routingService.getPremiumInstanceId()).toBe("abc123hash")

      routingService.setPremiumInstanceId(null)
      expect(routingService.getPremiumInstanceId()).toBeNull()
    })

    test("persists instance ID to localStorage", () => {
      routingService.setPremiumInstanceId("hash-from-api")
      expect(localStorageMock.getItem("premium_instance_id")).toBe(
        "hash-from-api",
      )
    })

    test("clears instance ID from localStorage when set to null", () => {
      routingService.setPremiumInstanceId("hash-from-api")
      routingService.setPremiumInstanceId(null)
      expect(localStorageMock.getItem("premium_instance_id")).toBeNull()
    })

    test("loads instance ID from localStorage on initialization", () => {
      localStorageMock.setItem("premium_instance_id", "stored-hash")

      const newService = new RoutingService()
      expect(newService.getPremiumInstanceId()).toBe("stored-hash")
    })

    test("clearRoutingInfo clears premium instance ID", () => {
      routingService.setPremiumInstanceId("hash-from-api")
      expect(routingService.getPremiumInstanceId()).toBe("hash-from-api")

      routingService.clearRoutingInfo()

      expect(routingService.getPremiumInstanceId()).toBeNull()
      expect(localStorageMock.getItem("premium_instance_id")).toBeNull()
    })
  })

  describe("premiumShared flag", () => {
    test("defaults to false", () => {
      expect(routingService.isPremiumShared()).toBe(false)
    })

    test("setPremiumShared / isPremiumShared round-trip and persist", () => {
      routingService.setPremiumShared(true)
      expect(routingService.isPremiumShared()).toBe(true)
      expect(localStorageMock.getItem("premium_shared")).toBe("true")

      routingService.setPremiumShared(false)
      expect(routingService.isPremiumShared()).toBe(false)
      expect(localStorageMock.getItem("premium_shared")).toBe("false")
    })

    test("loads shared flag from localStorage on initialization", () => {
      localStorageMock.setItem("premium_shared", "true")

      const newService = new RoutingService()
      expect(newService.isPremiumShared()).toBe(true)
    })

    test("clearRoutingInfo clears the shared flag", () => {
      routingService.setPremiumShared(true)
      routingService.clearRoutingInfo()

      expect(routingService.isPremiumShared()).toBe(false)
      expect(localStorageMock.getItem("premium_shared")).toBeNull()
    })

    test("resetForRelease clears the shared flag", () => {
      routingService.setPremiumShared(true)
      routingService.resetForRelease()

      expect(routingService.isPremiumShared()).toBe(false)
    })

    test("updateRoutingInfo for a non-premium user clears the shared flag", () => {
      routingService.setPremiumShared(true)
      routingService.updateRoutingInfo(createFreeUser())

      expect(routingService.isPremiumShared()).toBe(false)
    })
  })

  describe("premium warm-up window", () => {
    // Intentionally longer than DEDICATED_HANDOFF_GRACE_MS (15000) so the axios
    // window contains the machine's grace window — see PREMIUM_WARMUP_GRACE_MS.
    const GRACE_MS = 16000
    const T0 = 1_000_000

    beforeEach(() => {
      jest.useFakeTimers()
      jest.setSystemTime(T0)
      // Rebuild after fake timers are active so no stale state leaks in.
      routingService = new RoutingService()
    })

    afterEach(() => {
      jest.useRealTimers()
    })

    test("no window is open initially", () => {
      expect(routingService.isWithinPremiumWarmup()).toBe(false)
    })

    test("startPremiumWarmup opens a window that expires after the grace", () => {
      routingService.startPremiumWarmup()
      expect(routingService.isWithinPremiumWarmup()).toBe(true)

      jest.setSystemTime(T0 + GRACE_MS - 1)
      expect(routingService.isWithinPremiumWarmup()).toBe(true)

      jest.setSystemTime(T0 + GRACE_MS)
      expect(routingService.isWithinPremiumWarmup()).toBe(false)
    })

    test("setPremiumInstanceId arms the window on a new/changed instance", () => {
      routingService.setPremiumInstanceId("hash-A")
      expect(routingService.isWithinPremiumWarmup()).toBe(true)
    })

    test("re-confirming the same instance does not extend the window", () => {
      routingService.setPremiumInstanceId("hash-A")

      // Advance near the end of the original window, then re-confirm the SAME
      // instance — must not re-arm (a genuine late fallback still needs to
      // surface once the original window expires).
      jest.setSystemTime(T0 + GRACE_MS - 1)
      routingService.setPremiumInstanceId("hash-A")

      jest.setSystemTime(T0 + GRACE_MS)
      expect(routingService.isWithinPremiumWarmup()).toBe(false)
    })

    test("changing to a different instance re-arms the window", () => {
      routingService.setPremiumInstanceId("hash-A")

      jest.setSystemTime(T0 + GRACE_MS)
      expect(routingService.isWithinPremiumWarmup()).toBe(false)

      routingService.setPremiumInstanceId("hash-B")
      expect(routingService.isWithinPremiumWarmup()).toBe(true)
    })

    test("setPremiumInstanceId(null) clears the window", () => {
      routingService.setPremiumInstanceId("hash-A")
      routingService.setPremiumInstanceId(null)
      expect(routingService.isWithinPremiumWarmup()).toBe(false)
    })

    test("clearRoutingInfo clears the window", () => {
      routingService.setPremiumInstanceId("hash-A")
      routingService.clearRoutingInfo()
      expect(routingService.isWithinPremiumWarmup()).toBe(false)
    })
  })

  describe("reachable watermark / stale premium failure", () => {
    const T0 = 1_000_000

    beforeEach(() => {
      jest.useFakeTimers()
      jest.setSystemTime(T0)
      routingService = new RoutingService()
    })

    afterEach(() => {
      jest.useRealTimers()
    })

    test("no failure is stale before any reachable is observed", () => {
      expect(routingService.isStalePremiumFailure(100)).toBe(false)
    })

    test("emitPremiumReachable advances the watermark; an older failure is stale", () => {
      routingService.emitPremiumReachable({ status: 200, sentAt: 2000 })
      expect(routingService.isStalePremiumFailure(1500)).toBe(true)
    })

    test("a failure newer than the watermark is not stale", () => {
      routingService.emitPremiumReachable({ status: 200, sentAt: 2000 })
      expect(routingService.isStalePremiumFailure(2500)).toBe(false)
    })

    test("a failure sent at exactly the watermark is not stale", () => {
      routingService.emitPremiumReachable({ status: 200, sentAt: 2000 })
      expect(routingService.isStalePremiumFailure(2000)).toBe(false)
    })

    test("undefined sentAt is treated as now — not stale when the watermark is in the past", () => {
      routingService.emitPremiumReachable({ status: 200, sentAt: 2000 })
      expect(routingService.isStalePremiumFailure(undefined)).toBe(false)
    })

    test("undefined sentAt is stale only if now predates the watermark", () => {
      routingService.emitPremiumReachable({ status: 200, sentAt: T0 + 5000 })
      expect(routingService.isStalePremiumFailure(undefined)).toBe(true)
    })

    test("the watermark is monotonic — an older reachable does not lower it", () => {
      routingService.emitPremiumReachable({ status: 200, sentAt: 2000 })
      routingService.emitPremiumReachable({ status: 200, sentAt: 1000 })
      expect(routingService.isStalePremiumFailure(1500)).toBe(true)
    })

    test("clearRoutingInfo resets the watermark", () => {
      routingService.emitPremiumReachable({ status: 200, sentAt: 2000 })
      routingService.clearRoutingInfo()
      expect(routingService.isStalePremiumFailure(1500)).toBe(false)
    })

    test("resetForRelease resets the watermark", () => {
      routingService.emitPremiumReachable({ status: 200, sentAt: 2000 })
      routingService.resetForRelease()
      expect(routingService.isStalePremiumFailure(1500)).toBe(false)
    })

    test("updateRoutingInfo downgrade to free resets the watermark", () => {
      routingService.emitPremiumReachable({ status: 200, sentAt: 2000 })
      routingService.updateRoutingInfo(createFreeUser())
      expect(routingService.isStalePremiumFailure(1500)).toBe(false)
    })
  })
})
