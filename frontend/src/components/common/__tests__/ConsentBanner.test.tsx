import { Provider } from "react-redux"

import configureMockStore from "redux-mock-store"

// expect is left global so the jest-dom matchers stay typed.
import { beforeEach, afterEach, describe, it } from "@jest/globals"
import { render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import ConsentBanner from "components/common/ConsentBanner"
import { ModeType } from "store/slice/Standalone/StandaloneType"
import { CONSENT_STORAGE_KEY, trackEvent } from "utils/analytics"
import {
  disableGtm,
  setUpAnalyticsTest,
  tearDownAnalyticsTest,
} from "utils/analyticsTestUtils"

const mockStore = configureMockStore()

// Takes the whole slice state: a destructuring default would silently turn an
// explicitly passed `undefined` mode back into `false`.
const renderBanner = (mode: ModeType = { mode: false, loading: false }) =>
  render(
    <Provider store={mockStore({ mode })}>
      <ConsentBanner />
    </Provider>,
  )

describe("ConsentBanner", () => {
  let gtag: ReturnType<typeof setUpAnalyticsTest>

  beforeEach(() => {
    gtag = setUpAnalyticsTest()
  })

  afterEach(tearDownAnalyticsTest)

  it("renders nothing when GTM is not configured", () => {
    disableGtm()
    renderBanner()
    expect(screen.queryByTestId("consent-banner")).not.toBeInTheDocument()
  })

  it("renders nothing when a decision is already stored", () => {
    localStorage.setItem(CONSENT_STORAGE_KEY, "denied")
    renderBanner()
    expect(screen.queryByTestId("consent-banner")).not.toBeInTheDocument()
  })

  it("renders nothing in standalone mode", () => {
    renderBanner({ mode: true, loading: false })
    expect(screen.queryByTestId("consent-banner")).not.toBeInTheDocument()
  })

  it("renders nothing before the backend has confirmed the mode", () => {
    // The slice's own pre-confirmation state: `mode` is only ever set by
    // `fulfilled`, which also clears `loading`, so an undefined mode implies it.
    renderBanner({ mode: undefined, loading: true })
    expect(screen.queryByTestId("consent-banner")).not.toBeInTheDocument()
  })

  it("renders when GTM is configured and no decision is stored", () => {
    renderBanner()
    expect(screen.getByTestId("consent-banner")).toBeInTheDocument()
  })

  it("persists the grant, updates gtag and dismisses", async () => {
    renderBanner()

    await userEvent.click(screen.getByTestId("consent-accept"))

    expect(localStorage.getItem(CONSENT_STORAGE_KEY)).toBe("granted")
    expect(gtag).toHaveBeenCalledWith("consent", "update", {
      analytics_storage: "granted",
    })
    expect(screen.queryByTestId("consent-banner")).not.toBeInTheDocument()
  })

  it("persists the denial, updates gtag and dismisses", async () => {
    renderBanner()

    await userEvent.click(screen.getByTestId("consent-decline"))

    expect(localStorage.getItem(CONSENT_STORAGE_KEY)).toBe("denied")
    expect(gtag).toHaveBeenCalledWith("consent", "update", {
      analytics_storage: "denied",
    })
    expect(screen.queryByTestId("consent-banner")).not.toBeInTheDocument()
  })

  it("releases events buffered before the visitor accepted", async () => {
    trackEvent("route_change", { page_path: "/" })
    renderBanner()

    await userEvent.click(screen.getByTestId("consent-accept"))

    expect(window.dataLayer).toEqual([
      { event: "route_change", page_path: "/" },
    ])
  })

  it("drops events buffered before the visitor declined", async () => {
    trackEvent("route_change", { page_path: "/" })
    renderBanner()

    await userEvent.click(screen.getByTestId("consent-decline"))

    expect(window.dataLayer).toEqual([])
  })

  it("gives Decline and Accept identical visual weight", () => {
    renderBanner()
    expect(screen.getByTestId("consent-decline").className).toBe(
      screen.getByTestId("consent-accept").className,
    )
  })

  it("uses a labelled region rather than role=alert", () => {
    renderBanner()
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    expect(
      screen.getByRole("region", { name: "Analytics cookie consent" }),
    ).toBeInTheDocument()
  })
})
