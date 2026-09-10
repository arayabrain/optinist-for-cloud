/**
 * Fault-tolerant logout.
 *
 * The logout API call is fire-and-forget: local cleanup (dispatch(logout) +
 * redirect to /login) must happen regardless of whether the backend call
 * succeeds, so a blocked/offline API never strands the user logged in.
 */

import { beforeEach, describe, expect, jest, test } from "@jest/globals"
import { renderHook, act } from "@testing-library/react"

// jest.mock factories may only reference `mock`-prefixed outer vars (and must
// not call jest.fn() inside, since `jest` here is the @jest/globals import, not
// the allowed global). So build every mock fn out here and reference it inside.
const mockDispatch = jest.fn()
const mockNavigate = jest.fn()
const mockUsePremiumAssignment = jest.fn()
const mockLogoutFreeUserApi = jest.fn()
const mockLogout = jest.fn(() => ({ type: "user/logout" }))
const mockSetLoggingOut = jest.fn()
const mockBroadcastLogout = jest.fn()
const mockFlushErrors = jest.fn()
const mockAutoReleaseOnLogout = jest.fn()

jest.mock("react-redux", () => ({ useDispatch: () => mockDispatch }))
jest.mock("react-router-dom", () => ({ useNavigate: () => mockNavigate }))
jest.mock("contexts/PremiumAssignmentContext", () => ({
  usePremiumAssignment: () => mockUsePremiumAssignment(),
}))
jest.mock("api/users/UsersMe", () => ({
  logoutFreeUserApi: () => mockLogoutFreeUserApi(),
}))
jest.mock("store/slice/User/UserSlice", () => ({ logout: () => mockLogout() }))
jest.mock("utils/axios", () => ({ setLoggingOut: () => mockSetLoggingOut() }))
jest.mock("utils/crossTabSync", () => ({
  tabSync: { broadcastLogout: () => mockBroadcastLogout() },
}))
jest.mock("utils/errorReporter", () => ({
  flushErrors: () => mockFlushErrors(),
}))

// eslint-disable-next-line import/first
import { useLogout } from "hooks/useLogout"

describe("useLogout - fault-tolerant logout", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    mockLogout.mockReturnValue({ type: "user/logout" })
    mockUsePremiumAssignment.mockReturnValue({
      autoReleaseOnLogout: mockAutoReleaseOnLogout,
      isPremiumUser: false,
    })
  })

  test("free user: logout completes (dispatch + redirect) even when the logout API rejects", async () => {
    mockLogoutFreeUserApi.mockRejectedValue(
      new Error("net::ERR_INTERNET_DISCONNECTED"),
    )
    const { result } = renderHook(() => useLogout())

    await act(async () => {
      await result.current.performLogout()
    })

    expect(mockLogoutFreeUserApi).toHaveBeenCalled()
    // Local cleanup happened despite the API failure.
    expect(mockDispatch).toHaveBeenCalledWith({ type: "user/logout" })
    expect(mockNavigate).toHaveBeenCalledWith("/login")
  })

  test("premium user: releases via beacon and completes without the free-logout API", async () => {
    mockUsePremiumAssignment.mockReturnValue({
      autoReleaseOnLogout: mockAutoReleaseOnLogout,
      isPremiumUser: true,
    })
    const { result } = renderHook(() => useLogout())

    await act(async () => {
      await result.current.performLogout()
    })

    expect(mockAutoReleaseOnLogout).toHaveBeenCalled()
    expect(mockLogoutFreeUserApi).not.toHaveBeenCalled()
    expect(mockDispatch).toHaveBeenCalledWith({ type: "user/logout" })
    expect(mockNavigate).toHaveBeenCalledWith("/login")
  })
})
