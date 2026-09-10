import {
  beforeEach,
  afterEach,
  describe,
  expect,
  it,
  jest,
} from "@jest/globals"

import { analyticsMiddleware } from "store/analyticsMiddleware"
import {
  run,
  runApplyFilter,
  runByCurrentUid,
} from "store/slice/Pipeline/PipelineActions"
import { registerUser } from "store/slice/Registration/RegistrationActions"
import { getMe, login, proxyLogin } from "store/slice/User/UserActions"
import { CONSENT_STORAGE_KEY } from "utils/analytics"
import {
  disableGtm,
  setUpAnalyticsTest,
  tearDownAnalyticsTest,
} from "utils/analyticsTestUtils"

const dispatchThrough = (
  action: unknown,
  { isStandalone = false } = {},
): unknown[] => {
  const api = {
    getState: () => ({ mode: { mode: isStandalone, loading: false } }),
    dispatch: jest.fn(),
  }
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  ;(analyticsMiddleware as any)(api)((a: unknown) => a)(action)
  return window.dataLayer as unknown[]
}

describe("analyticsMiddleware", () => {
  beforeEach(() => {
    setUpAnalyticsTest()
    localStorage.setItem(CONSENT_STORAGE_KEY, "granted")
  })

  afterEach(tearDownAnalyticsTest)

  it("passes the action on to the next middleware", () => {
    const next = jest.fn((a: unknown) => a)
    const action = { type: "some/other/action" }
    const api = {
      getState: () => ({ mode: { mode: false, loading: false } }),
      dispatch: jest.fn(),
    }
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const result = (analyticsMiddleware as any)(api)(next)(action)

    expect(next).toHaveBeenCalledWith(action)
    expect(result).toBe(action)
  })

  it("emits sign_up on a fulfilled registration", () => {
    expect(dispatchThrough({ type: registerUser.fulfilled.type })).toEqual([
      { event: "sign_up" },
    ])
  })

  it("emits login on a fulfilled user login", () => {
    expect(
      dispatchThrough({
        type: login.fulfilled.type,
        meta: { arg: { email: "user@example.com", password: "secret" } },
      }),
    ).toEqual([{ event: "login" }])
  })

  it("emits no PII with the login event", () => {
    const pushed = dispatchThrough({
      type: login.fulfilled.type,
      meta: { arg: { email: "user@example.com", password: "secret" } },
    })
    expect(JSON.stringify(pushed)).not.toContain("user@example.com")
    expect(JSON.stringify(pushed)).not.toContain("secret")
  })

  it("ignores proxyLogin, which shares login's action type", () => {
    expect(
      dispatchThrough({
        type: proxyLogin.fulfilled.type,
        meta: { arg: "some-firebase-uid" },
      }),
    ).toEqual([])
  })

  it("ignores a login-typed action with no arg", () => {
    expect(dispatchThrough({ type: login.fulfilled.type })).toEqual([])
  })

  it("emits run_pipeline for both run thunks", () => {
    expect(dispatchThrough({ type: run.fulfilled.type })).toEqual([
      { event: "run_pipeline" },
    ])

    window.dataLayer = []
    expect(dispatchThrough({ type: runByCurrentUid.fulfilled.type })).toEqual([
      { event: "run_pipeline" },
    ])
  })

  it("ignores a filter re-run, which shares run's type stem", () => {
    expect(dispatchThrough({ type: runApplyFilter.fulfilled.type })).toEqual([])
  })

  it("ignores pending and rejected variants", () => {
    dispatchThrough({ type: run.pending.type })
    dispatchThrough({ type: run.rejected.type })
    expect(window.dataLayer).toEqual([])
  })

  it("ignores unmapped actions", () => {
    expect(dispatchThrough({ type: getMe.fulfilled.type })).toEqual([])
  })

  it("ignores action types that collide with Object prototype members", () => {
    expect(dispatchThrough({ type: "toString" })).toEqual([])
    expect(dispatchThrough({ type: "constructor" })).toEqual([])
  })

  it("emits nothing in standalone mode", () => {
    expect(
      dispatchThrough({ type: run.fulfilled.type }, { isStandalone: true }),
    ).toEqual([])
  })

  it("emits nothing when GTM is not configured", () => {
    disableGtm()
    expect(dispatchThrough({ type: run.fulfilled.type })).toEqual([])
  })

  it("emits nothing before the visitor has consented", () => {
    localStorage.clear()
    expect(dispatchThrough({ type: run.fulfilled.type })).toEqual([])
  })
})
