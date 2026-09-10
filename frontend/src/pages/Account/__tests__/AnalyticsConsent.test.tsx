import { Provider } from "react-redux"
import { MemoryRouter } from "react-router-dom"

import { SnackbarProvider } from "notistack"
import configureStore from "redux-mock-store"

// expect is left global so the jest-dom matchers stay typed.
import { describe, it, beforeEach, jest, afterEach } from "@jest/globals"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import { SubscriptionUserStatus } from "const/Subscription"
import Account from "pages/Account"
import { CONSENT_STORAGE_KEY, trackEvent } from "utils/analytics"
import {
  disableGtm,
  setUpAnalyticsTest,
  tearDownAnalyticsTest,
} from "utils/analyticsTestUtils"

jest.mock("api/subscriptions/Subscriptions")

jest.mock("utils/auth/AuthUtils", () => ({
  getToken: () => "mock-token",
}))

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
  updateDeletionPriority: () => ({
    type: "subscription/updateDeletionPriority/mock",
  }),
}))

const mockStoreCreator = configureStore([])

const renderAccount = () =>
  render(
    <Provider
      store={mockStoreCreator({
        user: {
          currentUser: {
            id: 1,
            name: "Test User",
            email: "test@example.com",
            data_usage: 1000,
            attributes: { remote_bucket_name: "test-bucket" },
          },
          loading: false,
        },
        subscription: {
          plans: [],
          userSubscription: {
            id: 1,
            plan_id: 2,
            user_id: 1,
            expiration: "2026-12-31",
            is_expired: false,
            scheduled_downgrade: false,
            status: SubscriptionUserStatus.SUBSCRIBED,
            plan_name: "Premium",
            plan_price: 10,
          },
          loading: false,
          checkoutLoading: false,
          error: null,
          plansLoading: false,
          userSubscriptionLoading: false,
          serverTime: null,
          deletionPriority: "preserve_outputs",
          deletionPriorityLoading: false,
        },
      })}
    >
      <MemoryRouter>
        <SnackbarProvider>
          <Account />
        </SnackbarProvider>
      </MemoryRouter>
    </Provider>,
  )

const consentSwitch = () =>
  screen.queryByRole("checkbox", { name: "Allow analytics cookies" })

describe("Account analytics consent control", () => {
  let gtag: ReturnType<typeof setUpAnalyticsTest>

  beforeEach(() => {
    gtag = setUpAnalyticsTest()
  })

  afterEach(tearDownAnalyticsTest)

  it("is absent when GTM is not configured", () => {
    localStorage.setItem(CONSENT_STORAGE_KEY, "granted")
    disableGtm()
    renderAccount()
    expect(consentSwitch()).not.toBeInTheDocument()
  })

  it("is absent while the visitor has not answered the notice", () => {
    renderAccount()
    expect(consentSwitch()).not.toBeInTheDocument()
  })

  it("reflects the stored decision", () => {
    localStorage.setItem(CONSENT_STORAGE_KEY, "granted")
    renderAccount()
    expect(consentSwitch()).toBeChecked()
  })

  it("withdraws consent without a reload", async () => {
    localStorage.setItem(CONSENT_STORAGE_KEY, "granted")
    renderAccount()

    await userEvent.click(consentSwitch() as HTMLElement)

    expect(localStorage.getItem(CONSENT_STORAGE_KEY)).toBe("denied")
    expect(gtag).toHaveBeenCalledWith("consent", "update", {
      analytics_storage: "denied",
    })
    expect(consentSwitch()).not.toBeChecked()

    trackEvent("route_change", { page_path: "/account" })
    expect(window.dataLayer).toEqual([])
  })

  it("re-grants consent from the same control", async () => {
    localStorage.setItem(CONSENT_STORAGE_KEY, "denied")
    renderAccount()

    await userEvent.click(consentSwitch() as HTMLElement)

    expect(localStorage.getItem(CONSENT_STORAGE_KEY)).toBe("granted")
    trackEvent("route_change", { page_path: "/account" })
    expect(window.dataLayer).toEqual([
      { event: "route_change", page_path: "/account" },
    ])
  })
})
