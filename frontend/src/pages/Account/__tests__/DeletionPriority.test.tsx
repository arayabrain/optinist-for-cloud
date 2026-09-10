import {
  describe,
  it,
  expect,
  beforeEach,
  jest,
  afterEach,
} from "@jest/globals"
import { screen, waitFor, fireEvent } from "@testing-library/react"

import {
  createAccountStore,
  editDeletionPriorityButton,
  PREMIUM_SUBSCRIPTION,
  renderAccount,
} from "pages/Account/accountTestHarness"

jest.mock("utils/auth/AuthUtils", () => ({
  getToken: () => "mock-token",
}))

// Plain actions, so an unrelated thunk cannot dispatch during these renders
jest.mock("store/slice/User/UserActions", () => ({
  getMe: () => ({ type: "user/getMe/mock" }),
  updateMe: () => ({ type: "user/updateMe/mock" }),
  updateMePassword: () => ({ type: "user/updateMePassword/mock" }),
  deleteMe: () => ({ type: "user/deleteMe/mock" }),
}))

jest.mock("store/slice/Subscriptions/SubscriptionActions", () => ({
  getUserSubscription: () => ({
    type: "subscription/getUserSubscription/mock",
  }),
  getDeletionPriority: () => ({
    type: "subscription/getDeletionPriority/mock",
  }),
  updateDeletionPriority: (priority: string) => ({
    type: "subscription/updateDeletionPriority/pending",
    meta: { arg: priority },
  }),
}))

const createStore = (subscription: Record<string, unknown> = {}) =>
  createAccountStore({
    subscription: { userSubscription: PREMIUM_SUBSCRIPTION, ...subscription },
  })

describe("DeletionPriority dropdown", () => {
  beforeEach(() => {
    jest.clearAllMocks()
  })

  afterEach(() => {
    jest.restoreAllMocks()
  })

  it("renders with default value", async () => {
    const store = createStore()
    renderAccount(store)

    await waitFor(() => {
      expect(screen.getByText("Data Deletion Priority")).toBeTruthy()
      expect(screen.getByText("Preserve Outputs")).toBeTruthy()
    })
  })

  it("renders with stored value", async () => {
    const store = createStore({ deletionPriority: "preserve_inputs" })
    renderAccount(store)

    await waitFor(() => {
      expect(screen.getByText("Preserve Inputs")).toBeTruthy()
    })
  })

  it("dispatches update action on change", async () => {
    const store = createStore()
    renderAccount(store)

    await waitFor(() => {
      expect(screen.getByText("Preserve Outputs")).toBeTruthy()
    })

    fireEvent.click(editDeletionPriorityButton())

    // Open the select dropdown
    await waitFor(() => {
      expect(screen.getByRole("combobox")).toBeTruthy()
    })
    const selectEl = screen.getByRole("combobox")
    fireEvent.mouseDown(selectEl)

    // Click the "Preserve Inputs" option in the dropdown menu
    await waitFor(() => {
      const option = screen.getByRole("option", { name: "Preserve Inputs" })
      fireEvent.click(option)
    })

    // Verify dispatch was called with the right action
    const actions = store.getActions()
    expect(
      actions.some(
        (a: { type: string }) =>
          a.type === "subscription/updateDeletionPriority/pending",
      ),
    ).toBeTruthy()
  })

  it("renders label when error state is set", async () => {
    const store = createStore({ error: "Failed to update deletion priority" })
    renderAccount(store)

    await waitFor(() => {
      expect(screen.getByText("Data Deletion Priority")).toBeTruthy()
    })
  })
})
