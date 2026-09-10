import { describe, it, expect } from "@jest/globals"

import { isPublicRoute, requiresAuth } from "utils/auth/AuthUtils"

describe("public routes", () => {
  it.each(["/terms", "/privacy"])("%s is reachable without login", (path) => {
    expect(isPublicRoute(path)).toBe(true)
    expect(requiresAuth(path)).toBe(false)
  })

  it.each(["/terms/x", "/privacy/x", "/termsx", "/account"])(
    "%s still requires auth",
    (path) => {
      expect(isPublicRoute(path)).toBe(false)
      expect(requiresAuth(path)).toBe(true)
    },
  )
})
