/**
 * Shared store, render wrapper and locators for the Account page tests.
 *
 * Lives outside `__tests__` because jest collects every module in there as a
 * suite. The `jest.mock` calls stay in each test file, since jest hoists them
 * per module; everything else was duplicated, which is how the two files ended
 * up disagreeing about which edit button was which.
 */

import { Provider } from "react-redux"
import { MemoryRouter } from "react-router-dom"

import { SnackbarProvider } from "notistack"
import configureStore from "redux-mock-store"

import { render, screen } from "@testing-library/react"

import { SubscriptionUserStatus } from "const/Subscription"
import Account from "pages/Account"

const mockStoreCreator = configureStore([])

export type MockStore = ReturnType<typeof mockStoreCreator>

export const PREMIUM_SUBSCRIPTION = {
  id: 1,
  plan_id: 2,
  user_id: 1,
  expiration: "2027-12-31",
  is_expired: false,
  scheduled_downgrade: false,
  status: SubscriptionUserStatus.SUBSCRIBED,
  plan_name: "Premium",
  plan_price: 10,
}

export const createAccountStore = ({
  user = {},
  subscription = {},
}: {
  user?: Record<string, unknown>
  subscription?: Record<string, unknown>
} = {}): MockStore =>
  mockStoreCreator({
    user: {
      currentUser: {
        id: 1,
        name: "Original Name",
        email: "test@example.com",
        data_usage: 1000,
        attributes: { remote_bucket_name: "test-bucket" },
      },
      loading: false,
      ...user,
    },
    subscription: {
      plans: [],
      userSubscription: null,
      loading: false,
      checkoutLoading: false,
      error: null,
      plansLoading: false,
      userSubscriptionLoading: false,
      serverTime: null,
      deletionPriority: "preserve_outputs",
      deletionPriorityLoading: false,
      ...subscription,
    },
  })

export const renderAccount = (store: MockStore) =>
  render(
    <Provider store={store}>
      <MemoryRouter>
        <SnackbarProvider>
          <Account />
        </SnackbarProvider>
      </MemoryRouter>
    </Provider>,
  )

export const nameInput = () =>
  screen.queryByLabelText("Name") as HTMLInputElement | null

export const editNameButton = () =>
  screen.getByRole("button", { name: "Edit name" })

export const editDeletionPriorityButton = () =>
  screen.getByRole("button", { name: "Edit deletion priority" })

export const dispatchedActions = (store: MockStore, type: string) =>
  store
    .getActions()
    .filter((action: { type: string }) => action.type.startsWith(type))
