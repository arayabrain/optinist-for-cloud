/**
 * Auto storage refresh, once per session.
 *
 * The assertion is the guard's effect - exactly one POST
 * /workspaces/refresh-storage across repeated auth checks - not the presence of
 * the sessionStorage key. Logging out deliberately clears that key, so the next
 * login refreshes again; that direction is covered with the logout reducer.
 */

import { ReactNode } from "react"
import { Provider } from "react-redux"
import { MemoryRouter } from "react-router-dom"

import { default as configureStore } from "redux-mock-store"

import { describe, it, expect, jest, beforeEach } from "@jest/globals"
import "@testing-library/jest-dom"
import { render, act } from "@testing-library/react"

import { refreshAllWorkspacesStorageApi } from "api/workspace"
import Layout from "components/Layout"
import { AppDispatch } from "store/store"

jest.mock("api/workspace")

// AuthedLayout's furniture is not what this asserts
jest.mock("components/Layout/Header", () => ({
  __esModule: true,
  default: () => null,
}))
jest.mock("components/Layout/LeftMenu", () => ({
  __esModule: true,
  default: () => null,
}))
jest.mock("components/common/LimitAlert", () => ({
  __esModule: true,
  default: () => null,
}))
jest.mock("components/common/LogsFloatingButton", () => ({
  LogsFloatingButton: () => null,
}))
jest.mock("components/Premium/InactivityWarning", () => ({
  __esModule: true,
  default: () => null,
}))
jest.mock("components/Premium/PremiumAssignmentManager", () => ({
  __esModule: true,
  default: () => null,
}))
jest.mock("components/Premium/PremiumNotificationManager", () => ({
  __esModule: true,
  default: () => null,
}))
jest.mock("contexts/PremiumAssignmentContext", () => ({
  PremiumAssignmentProvider: ({ children }: { children: ReactNode }) =>
    children,
}))

const mockRefreshAll = refreshAllWorkspacesStorageApi as jest.MockedFunction<
  typeof refreshAllWorkspacesStorageApi
>

const mockStore = configureStore<Record<string, unknown>, AppDispatch>([])

const newStore = () => {
  const store = mockStore({
    // No user in the store, so checkAuth takes the getMe-then-refresh path a
    // real login takes
    user: { currentUser: null },
    mode: { mode: false, loading: false },
    logsModal: { isOpen: false },
  })
  store.dispatch = jest.fn(() => Promise.resolve({})) as never
  return store
}

const mountAt = async (path: string) => {
  let rendered: ReturnType<typeof render>
  await act(async () => {
    rendered = render(
      <Provider store={newStore()}>
        <MemoryRouter initialEntries={[path]}>
          <Layout>
            <div>content</div>
          </Layout>
        </MemoryRouter>
      </Provider>,
    )
  })
  return rendered!
}

describe("Auto storage refresh throttle", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    sessionStorage.clear()
    // What `getToken()` reads; a token plus no user in the store is the state a
    // real login lands in
    localStorage.setItem("access_token", "token")
    mockRefreshAll.mockResolvedValue({ refreshed_workspaces: 1 } as never)
  })

  it("refreshes storage on the first authenticated load of a session", async () => {
    await mountAt("/dashboard")

    expect(mockRefreshAll).toHaveBeenCalledTimes(1)
    expect(mockRefreshAll).toHaveBeenCalledWith(
      expect.objectContaining({ signal: expect.anything() }),
    )
  })

  it("aborts a refresh that never answers", async () => {
    jest.useFakeTimers()
    try {
      // A backend that accepts the request and never replies would otherwise
      // hold the authenticated load open indefinitely
      let signal: AbortSignal | undefined
      mockRefreshAll.mockImplementation((options) => {
        signal = options?.signal ?? undefined
        return new Promise(() => {}) as never
      })

      await mountAt("/dashboard")
      expect(signal?.aborted).toBe(false)

      await act(async () => {
        jest.advanceTimersByTime(10_000)
      })
      expect(signal?.aborted).toBe(true)
    } finally {
      jest.useRealTimers()
    }
  })

  it("does not refresh again on a later auth check in the same session", async () => {
    const first = await mountAt("/dashboard")
    expect(mockRefreshAll).toHaveBeenCalledTimes(1)

    first.unmount()
    await mountAt("/workspaces")

    expect(mockRefreshAll).toHaveBeenCalledTimes(1)
  })

  it("retries on the next auth check when the refresh failed", async () => {
    jest.useFakeTimers()
    try {
      mockRefreshAll.mockRejectedValue(new Error("network"))
      const first = await mountAt("/dashboard")
      // Layout sleeps 1s between its two attempts inside one call
      await act(async () => {
        jest.advanceTimersByTime(1_000)
      })
      expect(mockRefreshAll).toHaveBeenCalledTimes(2)
      expect(sessionStorage.getItem("storage-refreshed-on-login")).toBeNull()

      // A failure must not consume the session's one refresh, or a user whose
      // first load raced the backend keeps a stale usage figure all session
      mockRefreshAll.mockResolvedValue({ refreshed_workspaces: 1 } as never)
      first.unmount()
      await mountAt("/dashboard")

      expect(mockRefreshAll).toHaveBeenCalledTimes(3)
    } finally {
      jest.useRealTimers()
    }
  })
})
