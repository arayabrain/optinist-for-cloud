import { describe, it, expect, beforeEach, jest } from "@jest/globals"

import type { User } from "store/slice/User/UserType"
import type { RootState } from "store/store"

/**
 * Tests for UserSlice
 *
 * These tests verify:
 * - logoutGeneration is initialized to 0
 * - logoutGeneration increments on logout
 * - logoutGeneration is preserved through logout (other state reset)
 * - getMe.rejected does not clear tokens (prevents forced logout on transient errors)
 */

// Mock dependencies - use mock prefix for hoisting compatibility
const mockSetLoggingOut = jest.fn()
const mockRemoveToken = jest.fn()
const mockRemoveRefreshToken = jest.fn()
const mockRemoveExToken = jest.fn()
const mockSaveToken = jest.fn()
const mockSaveRefreshToken = jest.fn()
const mockSaveExToken = jest.fn()
const mockClearRoutingInfo = jest.fn()
const mockUpdateRoutingInfo = jest.fn()

jest.mock("utils/axios", () => ({
  setLoggingOut: mockSetLoggingOut,
}))

jest.mock("utils/auth/AuthUtils", () => ({
  removeToken: mockRemoveToken,
  removeRefreshToken: mockRemoveRefreshToken,
  removeExToken: mockRemoveExToken,
  saveToken: mockSaveToken,
  saveRefreshToken: mockSaveRefreshToken,
  saveExToken: mockSaveExToken,
}))

jest.mock("utils/routing/RoutingService", () => ({
  routingService: {
    clearRoutingInfo: mockClearRoutingInfo,
    updateRoutingInfo: mockUpdateRoutingInfo,
  },
}))

// Mock localStorage
const localStorageMock = {
  removeItem: jest.fn(),
  getItem: jest.fn(),
  setItem: jest.fn(),
  clear: jest.fn(),
  length: 0,
  key: jest.fn(),
}
Object.defineProperty(global, "localStorage", { value: localStorageMock })

// Mock sessionStorage
const sessionStorageMock = {
  removeItem: jest.fn(),
  getItem: jest.fn(),
  setItem: jest.fn(),
  clear: jest.fn(),
  length: 0,
  key: jest.fn(),
}
Object.defineProperty(global, "sessionStorage", { value: sessionStorageMock })

describe("UserSlice", () => {
  let userSlice: typeof import("store/slice/User/UserSlice").userSlice
  let logout: typeof import("store/slice/User/UserSlice").logout

  beforeEach(async () => {
    jest.clearAllMocks()
    jest.resetModules()

    // Re-import to get fresh state
    const module = await import("store/slice/User/UserSlice")
    userSlice = module.userSlice
    logout = module.logout
  })

  describe("initialState", () => {
    it("should have logoutGeneration initialized to 0", () => {
      const state = userSlice.reducer(undefined, { type: "@@INIT" })
      expect(state.logoutGeneration).toBe(0)
    })

    it("should have currentUser as undefined", () => {
      const state = userSlice.reducer(undefined, { type: "@@INIT" })
      expect(state.currentUser).toBeUndefined()
    })

    it("should have loading as false", () => {
      const state = userSlice.reducer(undefined, { type: "@@INIT" })
      expect(state.loading).toBe(false)
    })
  })

  describe("logout action", () => {
    it("should increment logoutGeneration", () => {
      const initialState = userSlice.reducer(undefined, { type: "@@INIT" })
      expect(initialState.logoutGeneration).toBe(0)

      const stateAfterLogout = userSlice.reducer(initialState, logout())
      expect(stateAfterLogout.logoutGeneration).toBe(1)
    })

    it("should increment logoutGeneration on each logout", () => {
      let state = userSlice.reducer(undefined, { type: "@@INIT" })
      expect(state.logoutGeneration).toBe(0)

      state = userSlice.reducer(state, logout())
      expect(state.logoutGeneration).toBe(1)

      state = userSlice.reducer(state, logout())
      expect(state.logoutGeneration).toBe(2)

      state = userSlice.reducer(state, logout())
      expect(state.logoutGeneration).toBe(3)
    })

    it("should reset currentUser to undefined", () => {
      const stateWithUser: User = {
        currentUser: {
          id: 1,
          uid: "test",
          email: "test@example.com",
        } as User["currentUser"],
        listUserSearch: undefined,
        listUser: undefined,
        loading: false,
        logoutGeneration: 5,
      }

      const stateAfterLogout = userSlice.reducer(stateWithUser, logout())
      expect(stateAfterLogout.currentUser).toBeUndefined()
    })

    it("should reset loading to false", () => {
      const stateWithLoading: User = {
        currentUser: undefined,
        listUserSearch: undefined,
        listUser: undefined,
        loading: true,
        logoutGeneration: 0,
      }

      const stateAfterLogout = userSlice.reducer(stateWithLoading, logout())
      expect(stateAfterLogout.loading).toBe(false)
    })

    it("should call setLoggingOut(true)", () => {
      const state = userSlice.reducer(undefined, { type: "@@INIT" })

      userSlice.reducer(state, logout())

      expect(mockSetLoggingOut).toHaveBeenCalledWith(true)
    })

    it("should clear every stored auth token", () => {
      const state = userSlice.reducer(undefined, { type: "@@INIT" })

      userSlice.reducer(state, logout())

      expect(mockRemoveToken).toHaveBeenCalledTimes(1)
      expect(mockRemoveRefreshToken).toHaveBeenCalledTimes(1)
      expect(mockRemoveExToken).toHaveBeenCalledTimes(1)
    })

    it("should clear premium routing info", () => {
      const state = userSlice.reducer(undefined, { type: "@@INIT" })

      userSlice.reducer(state, logout())

      expect(mockClearRoutingInfo).toHaveBeenCalledTimes(1)
    })

    it("should clear localStorage dismissedAlerts", () => {
      const state = userSlice.reducer(undefined, { type: "@@INIT" })

      userSlice.reducer(state, logout())

      expect(localStorageMock.removeItem).toHaveBeenCalledWith(
        "dismissedAlerts",
      )
    })

    it("should clear localStorage storageAlertDismissed", () => {
      const state = userSlice.reducer(undefined, { type: "@@INIT" })

      userSlice.reducer(state, logout())

      expect(localStorageMock.removeItem).toHaveBeenCalledWith(
        "storageAlertDismissed",
      )
    })

    it("should clear sessionStorage storage-refreshed-on-login", () => {
      const state = userSlice.reducer(undefined, { type: "@@INIT" })

      userSlice.reducer(state, logout())

      expect(sessionStorageMock.removeItem).toHaveBeenCalledWith(
        "storage-refreshed-on-login",
      )
    })
  })

  describe("getMe.rejected", () => {
    it("should clear currentUser but not clear tokens", () => {
      const stateWithUser: User = {
        currentUser: {
          id: 1,
          uid: "test",
          email: "test@example.com",
        } as User["currentUser"],
        listUserSearch: undefined,
        listUser: undefined,
        loading: true,
        logoutGeneration: 0,
      }

      const getMeRejectedAction = {
        type: "user/getMe/rejected",
        error: { message: "Network Error" },
      }

      const state = userSlice.reducer(stateWithUser, getMeRejectedAction)

      expect(state.currentUser).toBeUndefined()
      expect(state.loading).toBe(false)
      expect(mockRemoveToken).not.toHaveBeenCalled()
      expect(mockRemoveRefreshToken).not.toHaveBeenCalled()
      expect(mockRemoveExToken).not.toHaveBeenCalled()
      expect(mockClearRoutingInfo).not.toHaveBeenCalled()
    })
  })

  describe("login.fulfilled", () => {
    const loginFulfilledAction = {
      type: "user/login/fulfilled",
      payload: {
        access_token: "new_access_token",
        refresh_token: "new_refresh_token",
        ex_token: "new_ex_token",
      },
    }

    it("should clear routing info before saving new tokens", () => {
      const state = userSlice.reducer(undefined, { type: "@@INIT" })

      userSlice.reducer(state, loginFulfilledAction)

      // clearRoutingInfo must be called
      expect(mockClearRoutingInfo).toHaveBeenCalledTimes(1)
      // tokens must be saved
      expect(mockSaveToken).toHaveBeenCalledWith("new_access_token")
      expect(mockSaveRefreshToken).toHaveBeenCalledWith("new_refresh_token")
      expect(mockSaveExToken).toHaveBeenCalledWith("new_ex_token")

      // clearRoutingInfo must be called BEFORE saveToken
      const clearOrder = mockClearRoutingInfo.mock.invocationCallOrder[0]
      const saveOrder = mockSaveToken.mock.invocationCallOrder[0]
      expect(clearOrder).toBeLessThan(saveOrder)
    })
  })

  describe("proxyLogin.fulfilled", () => {
    // proxyLogin currently shares the same action type as login
    // ("user/login"), so this test exercises the same isAnyOf matcher.
    // An explicit test ensures coverage remains visible if the action
    // types diverge in the future.
    const proxyLoginFulfilledAction = {
      type: "user/login/fulfilled",
      payload: {
        access_token: "proxy_access_token",
        refresh_token: "proxy_refresh_token",
        ex_token: "proxy_ex_token",
      },
    }

    it("should clear routing info before saving new tokens", () => {
      const state = userSlice.reducer(undefined, { type: "@@INIT" })

      userSlice.reducer(state, proxyLoginFulfilledAction)

      expect(mockClearRoutingInfo).toHaveBeenCalledTimes(1)
      expect(mockSaveToken).toHaveBeenCalledWith("proxy_access_token")
      expect(mockSaveRefreshToken).toHaveBeenCalledWith("proxy_refresh_token")
      expect(mockSaveExToken).toHaveBeenCalledWith("proxy_ex_token")

      const clearOrder = mockClearRoutingInfo.mock.invocationCallOrder[0]
      const saveOrder = mockSaveToken.mock.invocationCallOrder[0]
      expect(clearOrder).toBeLessThan(saveOrder)
    })
  })
})

describe("selectLogoutGeneration", () => {
  it("should select logoutGeneration from state", async () => {
    const { selectLogoutGeneration } = await import(
      "store/slice/User/UserSelector"
    )

    const mockState: Pick<RootState, "user"> = {
      user: {
        currentUser: undefined,
        listUserSearch: undefined,
        listUser: undefined,
        loading: false,
        logoutGeneration: 42,
      },
    }

    const result = selectLogoutGeneration(mockState as RootState)
    expect(result).toBe(42)
  })
})
