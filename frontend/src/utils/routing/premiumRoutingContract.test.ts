/**
 * Consumer-side contract test for the premium/routing response shapes.
 *
 * The FE and BE share ONE fixture file
 * (frontend/src/utils/routing/__fixtures__/premium_routing/premium_contract.json).
 * The BACKEND producer test (studio/tests/app/common/routers/
 * test_premium_contract_fixtures.py) is the sole PER-PR contract authority: it
 * ties each fixture to the FastAPI response models and runs in make test_backend
 * on every PR.
 *
 * This FE half is:
 *   - a build/IDE-time interface binding (the typed consts below): each fixture
 *     slice is a FRESH object literal typed as its FE interface, so a renamed or
 *     retyped field — required OR optional (excess-property check) — fails
 *     `yarn build` and the IDE. This does NOT run in CI: linters.yml ignores
 *     frontend/**, there is no tsc job, and jest runs via babel (types erased).
 *     It is a developer aid, not a per-PR gate (a project-wide tsc gate is
 *     blocked by pre-existing type errors elsewhere in the app).
 *   - per-PR runtime checks (jest): the fixture is well-formed for the fields
 *     the frontend reads, tier values are valid UserTier members, and
 *     RoutingService emits the pinned routing-id header name.
 *
 * Identifier-omission guards already live in
 * studio/tests/app/common/routers/test_premium_api_contract.py; not duplicated.
 */

import { describe, test, expect } from "@jest/globals"

import type {
  PremiumAssignment,
  PremiumAssignmentResult,
  PremiumHeartbeatResult,
  PremiumReleaseResult,
  PremiumStatusResult,
  RoutingInfo,
} from "api/premium/PremiumAssignmentApi"
import { RoutingHeaders, UserTier } from "const/Subscription"
import contract from "utils/routing/__fixtures__/premium_routing/premium_contract.json"
import { RoutingService } from "utils/routing/RoutingService"

// Compile-time interface binding (build/IDE gate). Each const is a fresh literal
// naming every fixture field explicitly: a field access breaks if the FIXTURE
// drops/renames a field, and the excess-property check breaks if the INTERFACE
// renames one (required or optional). Enum-typed fields are cast from the JSON's
// widened string; their VALUE is validated at runtime below.
const _routingInfo: RoutingInfo = {
  user_tier: contract.routing_info.user_tier as UserTier,
  requires_premium_routing: contract.routing_info.requires_premium_routing,
  routing_headers: contract.routing_info.routing_headers,
}
const _assign: PremiumAssignmentResult = {
  message: contract.premium_assign.message,
  assigned: contract.premium_assign.assigned,
  instance_id: contract.premium_assign.instance_id,
  instance_id_hash: contract.premium_assign.instance_id_hash,
  is_shared: contract.premium_assign.is_shared,
  assignment_source: contract.premium_assign.assignment_source,
}
const _release: PremiumReleaseResult = {
  message: contract.premium_release.message,
  released: contract.premium_release.released,
  released_instance: contract.premium_release.released_instance,
}
const _assignment: PremiumAssignment = {
  instance_id: contract.premium_status.assignment.instance_id,
  instance_id_hash: contract.premium_status.assignment.instance_id_hash,
  assigned_at: contract.premium_status.assignment.assigned_at,
  status: contract.premium_status.assignment.status,
  is_shared: contract.premium_status.assignment.is_shared,
}
const _status: PremiumStatusResult = {
  subscription_type: contract.premium_status.subscription_type as UserTier,
  is_premium: contract.premium_status.is_premium,
  assignment: _assignment,
}
const _heartbeat: PremiumHeartbeatResult = {
  message: contract.premium_heartbeat.message,
  updated: contract.premium_heartbeat.updated,
  user_tier: contract.premium_heartbeat.user_tier as UserTier,
  assignment_active: contract.premium_heartbeat.assignment_active,
}
void [_routingInfo, _assign, _release, _assignment, _status, _heartbeat]

// Mock localStorage for the RoutingService instance used below.
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

const TIERS = Object.values(UserTier) as string[]

describe("premium-routing contract fixtures (consumer side)", () => {
  test("no response body carries a user_id / uid identifier", () => {
    expect(JSON.stringify(contract)).not.toMatch(/"(user_id|uid)"\s*:/)
  })

  test("routing-info is well-formed and carries a valid tier", () => {
    const f = contract.routing_info
    expect(TIERS).toContain(f.user_tier)
    expect(typeof f.requires_premium_routing).toBe("boolean")
    expect(typeof f.routing_headers).toBe("object")
  })

  test("premium/assign is well-formed", () => {
    const f = contract.premium_assign
    expect(typeof f.message).toBe("string")
    expect(typeof f.assigned).toBe("boolean")
    expect(typeof f.instance_id).toBe("string")
    expect(typeof f.instance_id_hash).toBe("string")
    expect(typeof f.is_shared).toBe("boolean")
    expect(typeof f.assignment_source).toBe("string")
  })

  test("premium/assign (delete) is well-formed", () => {
    const f = contract.premium_release
    expect(typeof f.message).toBe("string")
    expect(typeof f.released).toBe("boolean")
    expect(typeof f.released_instance).toBe("string")
  })

  test("premium/status + nested assignment are well-formed with a valid tier", () => {
    const f = contract.premium_status
    expect(TIERS).toContain(f.subscription_type)
    expect(typeof f.is_premium).toBe("boolean")
    const a = f.assignment
    expect(typeof a.instance_id).toBe("string")
    expect(typeof a.instance_id_hash).toBe("string")
    expect(typeof a.assigned_at).toBe("string")
    expect(typeof a.status).toBe("string")
    expect(typeof a.is_shared).toBe("boolean")
  })

  test("premium/heartbeat is well-formed and carries a valid tier", () => {
    const f = contract.premium_heartbeat
    expect(typeof f.message).toBe("string")
    expect(typeof f.updated).toBe("boolean")
    expect(TIERS).toContain(f.user_tier)
    expect(typeof f.assignment_active).toBe("boolean")
  })

  test("free/logout is well-formed for logoutFreeUserApi's return shape", () => {
    const f = contract.free_logout
    expect(typeof f.message).toBe("string")
    expect(typeof f.logged_out).toBe("boolean")
    expect(typeof f.cleanup_after_minutes).toBe("number")
  })

  test("release-beacon is well-formed for the untyped BeaconResult shape", () => {
    const f = contract.release_beacon
    expect(typeof f.success).toBe("boolean")
    expect(typeof f.message).toBe("string")
  })

  test("routing header names match the RoutingHeaders const (case-insensitive)", () => {
    const h = contract.headers
    expect(RoutingHeaders.ROUTING_ID.toLowerCase()).toBe(
      h.routing_id.toLowerCase(),
    )
    expect(RoutingHeaders.USER_TIER.toLowerCase()).toBe(
      h.user_tier.toLowerCase(),
    )
    expect(RoutingHeaders.SERVED_BY_INSTANCE.toLowerCase()).toBe(
      h.served_by_instance.toLowerCase(),
    )
  })

  test("RoutingService emits the fixture's pinned routing-id header name", () => {
    // With a token and premiumAssigned set the way the context does on assign,
    // getRoutingHeaders must emit a header whose NAME is the pinned routing-id.
    const svc = new RoutingService()
    svc.updateRoutingToken("rid-x")
    svc.setPremiumAssigned(true)
    expect(Object.keys(svc.getRoutingHeaders())).toContain(
      contract.headers.routing_id,
    )
  })
})
