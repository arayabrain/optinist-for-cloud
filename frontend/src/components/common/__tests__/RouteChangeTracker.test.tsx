import { FC } from "react"
import { Provider } from "react-redux"
import { MemoryRouter, useNavigate } from "react-router-dom"

import configureMockStore from "redux-mock-store"

import { beforeEach, afterEach, describe, expect, it } from "@jest/globals"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import RouteChangeTracker from "components/common/RouteChangeTracker"
import { CONSENT_STORAGE_KEY } from "utils/analytics"
import {
  disableGtm,
  setUpAnalyticsTest,
  tearDownAnalyticsTest,
} from "utils/analyticsTestUtils"

const mockStore = configureMockStore()

const NavigateButton: FC<{ to: string }> = ({ to }) => {
  const navigate = useNavigate()
  return (
    <button onClick={() => navigate(to)} data-testid={`go-${to}`}>
      go
    </button>
  )
}

const renderTracker = (
  { isStandalone = false, initialPath = "/" } = {},
  targets: string[] = [],
) =>
  render(
    <Provider
      store={mockStore({ mode: { mode: isStandalone, loading: false } })}
    >
      <MemoryRouter initialEntries={[initialPath]}>
        <RouteChangeTracker />
        {targets.map((target) => (
          <NavigateButton key={target} to={target} />
        ))}
      </MemoryRouter>
    </Provider>,
  )

const navigateTo = (to: string) =>
  userEvent.click(screen.getByTestId(`go-${to}`))

const pageview = (pagePath: string) => ({
  event: "route_change",
  page_path: pagePath,
  page_location: `${window.location.origin}${pagePath}`,
})

describe("RouteChangeTracker", () => {
  beforeEach(() => {
    setUpAnalyticsTest()
    localStorage.setItem(CONSENT_STORAGE_KEY, "granted")
  })

  afterEach(tearDownAnalyticsTest)

  it("records the initial location", () => {
    renderTracker({ initialPath: "/public" })
    expect(window.dataLayer).toEqual([pageview("/public")])
  })

  it("records one pageview per navigation", async () => {
    renderTracker({ initialPath: "/" }, ["/login"])

    await navigateTo("/login")

    expect(window.dataLayer).toEqual([pageview("/"), pageview("/login")])
  })

  it("distinguishes different ids on the same route", async () => {
    renderTracker({ initialPath: "/workspaces/12" }, ["/workspaces/13"])

    await navigateTo("/workspaces/13")

    expect(window.dataLayer).toEqual([
      pageview("/workspaces/:id"),
      pageview("/workspaces/:id"),
    ])
  })

  it("ignores a query-string-only change and never sends the query string", async () => {
    renderTracker({ initialPath: "/account-manager" }, [
      "/account-manager?email=someone@example.com",
    ])

    await navigateTo("/account-manager?email=someone@example.com")

    expect(window.dataLayer).toEqual([pageview("/account-manager")])
    expect(JSON.stringify(window.dataLayer)).not.toContain(
      "someone@example.com",
    )
  })

  it("records nothing in standalone mode", async () => {
    renderTracker({ isStandalone: true, initialPath: "/" }, ["/login"])

    await navigateTo("/login")

    expect(window.dataLayer).toEqual([])
  })

  it("records nothing when GTM is not configured", async () => {
    disableGtm()
    renderTracker({ initialPath: "/" }, ["/login"])

    await navigateTo("/login")

    expect(window.dataLayer).toEqual([])
  })
})
