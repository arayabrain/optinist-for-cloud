/**
 * Manual storage refresh: the Reload button cannot be double-fired. Not
 * clicking twice is the invariant; the spinner is how the user is told.
 */

import { Provider } from "react-redux"
import { MemoryRouter } from "react-router-dom"

import { SnackbarProvider } from "notistack"
import { default as configureStore } from "redux-mock-store"

import { describe, it, expect, jest, beforeEach } from "@jest/globals"
import "@testing-library/jest-dom"
import { render, screen, act, fireEvent, waitFor } from "@testing-library/react"

import { refreshStorageUsageApi } from "api/storage/StorageAlerts"
import { refreshAllWorkspacesStorageApi } from "api/workspace"
import Workspaces from "pages/Workspace"
import { AppDispatch } from "store/store"

jest.mock("api/workspace")
jest.mock("api/storage/StorageAlerts")
// Stubbed so its own progressbar does not collide with the button's spinner.
// Counting mounts rather than renders is the point: a successful refresh proves
// it re-read usage only by remounting the panel, which the page does by bumping
// its key. Ordinary re-renders must not register.
const mockUsageMounts = { count: 0 }
jest.mock("components/common/StorageUsage", () => ({
  __esModule: true,
  default: function StorageUsageStub() {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const { useEffect } = require("react") as typeof import("react")
    useEffect(() => {
      mockUsageMounts.count += 1
    }, [])
    return null
  },
}))

const mockRefreshAll = refreshAllWorkspacesStorageApi as jest.MockedFunction<
  typeof refreshAllWorkspacesStorageApi
>
const mockRefreshUsage = refreshStorageUsageApi as jest.MockedFunction<
  typeof refreshStorageUsageApi
>

const mockStore = configureStore<Record<string, unknown>, AppDispatch>([])

const renderWorkspaces = () => {
  const store = mockStore({
    workspace: {
      workspace: { items: [], total: 0, limit: 50, offset: 0 },
      listUserShare: null,
      loading: false,
    },
    user: { currentUser: { id: 1, name: "E2E", email: "e2e@test.com" } },
  })
  store.dispatch = jest.fn(() => Promise.resolve({ payload: {} })) as never
  render(
    <Provider store={store}>
      <MemoryRouter>
        <SnackbarProvider>
          <Workspaces />
        </SnackbarProvider>
      </MemoryRouter>
    </Provider>,
  )
  return screen.getByRole("button", { name: /Reload/ })
}

describe("Workspaces storage Reload button", () => {
  beforeEach(() => {
    jest.clearAllMocks()
    mockUsageMounts.count = 0
    mockRefreshUsage.mockResolvedValue({} as never)
  })

  it("disables itself and shows a spinner until the refresh completes", async () => {
    let finishRefresh: (value: unknown) => void = () => {}
    mockRefreshAll.mockReturnValue(
      new Promise((resolve) => {
        finishRefresh = resolve
      }) as never,
    )

    const reload = renderWorkspaces()
    const mountsBeforeRefresh = mockUsageMounts.count
    expect(reload).toBeEnabled()
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument()

    await act(async () => {
      fireEvent.click(reload)
    })

    expect(reload).toBeDisabled()
    expect(screen.getByRole("progressbar")).toBeInTheDocument()

    // A second click while in flight must not start a second refresh
    fireEvent.click(reload)
    expect(mockRefreshAll).toHaveBeenCalledTimes(1)

    await act(async () => {
      finishRefresh({ refreshed_workspaces: 3 })
    })

    await waitFor(() => expect(reload).toBeEnabled())
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument()
    // The panel is remounted, or the figures on screen stay stale after a reload
    expect(mockUsageMounts.count).toBeGreaterThan(mountsBeforeRefresh)
  })

  it("re-enables itself when the refresh fails", async () => {
    mockRefreshAll.mockRejectedValue(new Error("boom"))

    const reload = renderWorkspaces()
    await act(async () => {
      fireEvent.click(reload)
    })

    // A failed refresh that left the button disabled would strand the page
    await waitFor(() => expect(reload).toBeEnabled())
  })
})
