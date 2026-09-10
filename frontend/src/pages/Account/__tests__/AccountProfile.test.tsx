/**
 * The Account Profile page: inline name editing, the two change-password
 * outcomes the modal cannot show on its own, and the per-tier deletion warning.
 *
 * The premium warning lines are covered here rather than end to end, because the
 * e2e account is free and cannot reach them without a paid subscription.
 */

/* eslint-disable no-undef -- `jest` is the injected global here rather than the
   `@jest/globals` import, because babel-plugin-jest-hoist rejects an imported
   `jest` being referenced inside a `jest.mock` factory. */

import { describe, it, expect, beforeEach } from "@jest/globals"
import "@testing-library/jest-dom"
import { screen, fireEvent, waitFor } from "@testing-library/react"

import { SubscriptionUserStatus } from "const/Subscription"
import {
  createAccountStore,
  dispatchedActions,
  editNameButton,
  nameInput,
  PREMIUM_SUBSCRIPTION,
  renderAccount,
} from "pages/Account/accountTestHarness"

// Only useNavigate is replaced; the harness needs the real MemoryRouter
const mockNavigate = jest.fn()
jest.mock("react-router-dom", () => ({
  ...jest.requireActual("react-router-dom"),
  useNavigate: () => mockNavigate,
}))

// `isRejectedWithValue` needs the full RTK rejection shape, so each mocked
// action reads a switch the test sets. Without this the failure branches - which
// is where the user-facing message lives - are unreachable.
const mockRejections: Record<string, boolean> = {}

jest.mock("store/slice/User/UserActions", () => {
  const settled = (name: string, arg: unknown) =>
    mockRejections[name]
      ? {
          type: `user/${name}/rejected`,
          payload: "boom",
          error: { message: "Rejected" },
          meta: {
            requestId: "test",
            requestStatus: "rejected",
            rejectedWithValue: true,
            arg,
          },
        }
      : {
          type: `user/${name}/fulfilled`,
          meta: { requestId: "test", requestStatus: "fulfilled", arg },
        }

  return {
    getMe: () => ({ type: "user/getMe/mock" }),
    updateMe: (arg: unknown) => settled("updateMe", arg),
    updateMePassword: (arg: unknown) => settled("updateMePassword", arg),
    deleteMe: () => settled("deleteMe", undefined),
  }
})

jest.mock("store/slice/Subscriptions/SubscriptionActions", () => ({
  getUserSubscription: () => ({
    type: "subscription/getUserSubscription/mock",
  }),
  getDeletionPriority: () => ({
    type: "subscription/getDeletionPriority/mock",
  }),
  updateDeletionPriority: () => ({
    type: "subscription/updateDeletionPriority/mock",
  }),
}))

const showAccount = (subscription: unknown = null) => {
  const store = createAccountStore({
    subscription: { userSubscription: subscription },
  })
  renderAccount(store)
  return store
}

// The modals render through a portal, so their inputs are not under `container`
const inputBy = (attribute: string, value: string) =>
  document.querySelector(
    `input[${attribute}="${value}"]`,
  ) as HTMLInputElement | null

const startEditingName = () => {
  fireEvent.click(editNameButton())
  return nameInput() as HTMLInputElement
}

// Every describe shares the module-level switch, so it is reset for all of them
// rather than per block
beforeEach(() => {
  jest.clearAllMocks()
  for (const key of Object.keys(mockRejections)) delete mockRejections[key]
})

describe("Account profile inline name editing", () => {
  it("saves the new name on Enter and confirms it", async () => {
    const store = showAccount()
    const input = startEditingName()

    fireEvent.change(input, { target: { value: "Edited Name" } })
    fireEvent.keyDown(input, { key: "Enter" })

    await waitFor(() =>
      expect(
        screen.getByText("Full name edited successfully!"),
      ).toBeInTheDocument(),
    )
    // The name that was typed, not the one that was already there
    const [update] = dispatchedActions(store, "user/updateMe")
    expect(update.meta.arg).toEqual({
      name: "Edited Name",
      email: "test@example.com",
    })
    // Editing is over: the input is gone
    expect(nameInput()).toBeNull()
  })

  it("restores the original name on Escape without saving", async () => {
    const store = showAccount()
    const input = startEditingName()

    fireEvent.change(input, { target: { value: "Discarded" } })
    fireEvent.keyDown(input, { key: "Escape" })

    await waitFor(() => expect(nameInput()).toBeNull())
    expect(screen.getByText("Original Name")).toBeInTheDocument()
    expect(dispatchedActions(store, "user/updateMe")).toHaveLength(0)
  })

  it("refuses an empty name and keeps the old one", async () => {
    const store = showAccount()
    const input = startEditingName()

    fireEvent.change(input, { target: { value: "" } })
    fireEvent.blur(input)

    await waitFor(() =>
      expect(screen.getByText("Full name can't be empty!")).toBeInTheDocument(),
    )
    expect(dispatchedActions(store, "user/updateMe")).toHaveLength(0)
    expect(screen.getByText("Original Name")).toBeInTheDocument()
  })

  it("reports a failed rename rather than showing it as saved", async () => {
    mockRejections.updateMe = true
    showAccount()
    const input = startEditingName()

    fireEvent.change(input, { target: { value: "Edited Name" } })
    fireEvent.keyDown(input, { key: "Enter" })

    await waitFor(() =>
      expect(screen.getByText("Full name edited failed!")).toBeInTheDocument(),
    )
    expect(screen.queryByText("Full name edited successfully!")).toBeNull()
  })
})

describe("Account profile change password outcomes", () => {
  const submitPasswordChange = () => {
    fireEvent.click(screen.getByRole("button", { name: "Change Password" }))
    const field = (name: string) => inputBy("name", name) as HTMLInputElement
    fireEvent.change(field("password"), { target: { value: "oldPass!1" } })
    fireEvent.change(field("new_password"), { target: { value: "newPass!1" } })
    fireEvent.change(field("confirm_password"), {
      target: { value: "newPass!1" },
    })
    fireEvent.click(screen.getByRole("button", { name: "UPDATE" }))
  }

  it("confirms a successful change and closes the modal", async () => {
    const store = showAccount()

    submitPasswordChange()

    await waitFor(() =>
      expect(
        screen.getByText("Your password has been successfully changed!"),
      ).toBeInTheDocument(),
    )
    const [update] = dispatchedActions(store, "user/updateMePassword")
    expect(update.meta.arg).toEqual({
      old_password: "oldPass!1",
      new_password: "newPass!1",
    })
    // The page's own "Change Password" button carries that text too, so the
    // modal closing is the fields going away
    await waitFor(() => expect(inputBy("name", "new_password")).toBeNull())
  })

  it("reports a wrong current password", async () => {
    // The server is the only thing that knows the old password is wrong, so the
    // modal cannot show this state on its own
    mockRejections.updateMePassword = true
    showAccount()

    submitPasswordChange()

    await waitFor(() =>
      expect(
        screen.getByText("Failed to Change Password!"),
      ).toBeInTheDocument(),
    )
    expect(
      screen.queryByText("Your password has been successfully changed!"),
    ).toBeNull()
  })
})

describe("Account deletion warning", () => {
  const PREMIUM_WARNINGS = [
    "Your subscription will be immediately canceled",
    "You will not receive a refund for the remaining period",
  ]
  const SHARED_WARNINGS = [
    "All your data (workspaces, experiments, files) will be permanently deleted",
    "This action cannot be undone",
  ]

  const openDeleteConfirmation = () =>
    fireEvent.click(screen.getByRole("button", { name: "Delete Account" }))

  it("warns a premium user about the subscription they are giving up", async () => {
    showAccount(PREMIUM_SUBSCRIPTION)

    openDeleteConfirmation()

    await waitFor(() =>
      expect(
        screen.getByText("You have an active Premium subscription."),
      ).toBeInTheDocument(),
    )
    for (const line of [...PREMIUM_WARNINGS, ...SHARED_WARNINGS]) {
      expect(screen.getByText(line)).toBeInTheDocument()
    }
  })

  it("does not show a free user subscription lines that do not apply", async () => {
    showAccount(null)

    openDeleteConfirmation()

    await waitFor(() =>
      expect(
        screen.getByText("Delete account will erase all of your data."),
      ).toBeInTheDocument(),
    )
    // Positive first: the shared lines prove the warning list rendered at all,
    // so the absences below are absences and not an unrendered dialog
    for (const line of SHARED_WARNINGS) {
      expect(screen.getByText(line)).toBeInTheDocument()
    }
    for (const line of PREMIUM_WARNINGS) {
      expect(screen.queryByText(line)).toBeNull()
    }
  })

  it("treats an expired premium subscription as free", async () => {
    showAccount({ ...PREMIUM_SUBSCRIPTION, is_expired: true })

    openDeleteConfirmation()

    await waitFor(() =>
      expect(
        screen.getByText("Delete account will erase all of your data."),
      ).toBeInTheDocument(),
    )
    for (const line of PREMIUM_WARNINGS) {
      expect(screen.queryByText(line)).toBeNull()
    }
  })
})

describe("Account deletion outcome", () => {
  const confirmDeletion = async () => {
    fireEvent.click(screen.getByRole("button", { name: "Delete Account" }))
    const submit = await waitFor(() =>
      screen.getByRole("button", { name: "Delete My Account" }),
    )
    // The confirmation is typed, and the submit stays disabled until it matches
    expect(submit).toBeDisabled()
    fireEvent.change(inputBy("placeholder", "DELETE") as HTMLInputElement, {
      target: { value: "DELETE" },
    })
    expect(submit).not.toBeDisabled()
    fireEvent.click(submit)
  }

  it("sends the user to the login page once the account is gone", async () => {
    showAccount()

    await confirmDeletion()

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/login"))
  })

  it("reports a failed deletion and keeps the user where they are", async () => {
    mockRejections.deleteMe = true
    showAccount()

    await confirmDeletion()

    await waitFor(() =>
      expect(screen.getByText("Account deleted failed!")).toBeInTheDocument(),
    )
    expect(mockNavigate).not.toHaveBeenCalled()
  })
})

describe("Subscription state on the profile", () => {
  it("treats a cancelled subscription as no longer premium", async () => {
    // Still unexpired, so only the status distinguishes it. Dropping the status
    // check from isPremiumUser would show this user the premium warnings.
    showAccount({
      ...PREMIUM_SUBSCRIPTION,
      status: SubscriptionUserStatus.CANCELED,
    })

    fireEvent.click(screen.getByRole("button", { name: "Delete Account" }))

    await waitFor(() =>
      expect(
        screen.getByText("Delete account will erase all of your data."),
      ).toBeInTheDocument(),
    )
    expect(
      screen.queryByText("Your subscription will be immediately canceled"),
    ).toBeNull()
  })

  it.each([
    ["renews while active", PREMIUM_SUBSCRIPTION, /Renew on/],
    [
      "expires when a downgrade is scheduled",
      { ...PREMIUM_SUBSCRIPTION, scheduled_downgrade: true },
      /Expires on/,
    ],
    [
      "reads as expired once it has lapsed",
      { ...PREMIUM_SUBSCRIPTION, is_expired: true },
      /Expired on/,
    ],
    [
      "reads as expired once cancelled",
      { ...PREMIUM_SUBSCRIPTION, status: SubscriptionUserStatus.CANCELED },
      /Expired on/,
    ],
  ])("says the subscription %s", async (_label, subscription, expected) => {
    showAccount(subscription)

    await waitFor(() => expect(screen.getByText(expected)).toBeInTheDocument())
  })

  it("offers a free user Upgrade and no Manage", async () => {
    showAccount(null)

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Upgrade" }),
      ).toBeInTheDocument(),
    )
    expect(screen.queryByRole("button", { name: "Manage" })).toBeNull()
  })

  it("offers a premium user Manage and no Upgrade", async () => {
    showAccount(PREMIUM_SUBSCRIPTION)

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Manage" }),
      ).toBeInTheDocument(),
    )
    expect(screen.queryByRole("button", { name: "Upgrade" })).toBeNull()
  })

  it("offers an expired user both Upgrade and Manage", async () => {
    showAccount({ ...PREMIUM_SUBSCRIPTION, is_expired: true })

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Upgrade" }),
      ).toBeInTheDocument(),
    )
    expect(screen.getByRole("button", { name: "Manage" })).toBeInTheDocument()
  })
})
