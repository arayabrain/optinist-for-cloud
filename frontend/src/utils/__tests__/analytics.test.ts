import fs from "fs"
import path from "path"

import {
  beforeEach,
  afterEach,
  describe,
  expect,
  it,
  jest,
} from "@jest/globals"

import {
  CONSENT_STORAGE_KEY,
  getAnalyticsConsent,
  initAnalyticsConsent,
  isGtmEnabled,
  normalizePath,
  setAnalyticsConsent,
  trackEvent,
} from "utils/analytics"
import {
  disableGtm,
  setUpAnalyticsTest,
  tearDownAnalyticsTest,
  TEST_GTM_ID,
} from "utils/analyticsTestUtils"

describe("analytics", () => {
  let gtag: ReturnType<typeof setUpAnalyticsTest>

  beforeEach(() => {
    gtag = setUpAnalyticsTest()
  })

  afterEach(tearDownAnalyticsTest)

  describe("isGtmEnabled", () => {
    it("is false when unset", () => {
      disableGtm()
      expect(isGtmEnabled()).toBe(false)
    })

    it("is false when empty", () => {
      process.env.REACT_APP_GTM_ID = ""
      expect(isGtmEnabled()).toBe(false)
    })

    it("is false for the un-interpolated CRA placeholder", () => {
      process.env.REACT_APP_GTM_ID = "%REACT_APP_GTM_ID%"
      expect(isGtmEnabled()).toBe(false)
    })

    it("rejects the ids that index.html also rejects", () => {
      // Diverging from that guard would show a consent notice on a site whose
      // container never loads.
      process.env.REACT_APP_GTM_ID = "gtm-lowercase"
      expect(isGtmEnabled()).toBe(false)
      process.env.REACT_APP_GTM_ID = "GTM-"
      expect(isGtmEnabled()).toBe(false)
      process.env.REACT_APP_GTM_ID = "GTM-ABC123&extra=1"
      expect(isGtmEnabled()).toBe(false)
    })

    it("is true for a container ID", () => {
      expect(isGtmEnabled()).toBe(true)
    })
  })

  describe("getAnalyticsConsent", () => {
    it("returns null with nothing stored", () => {
      expect(getAnalyticsConsent()).toBeNull()
    })

    it("returns null for an unrecognised stored value", () => {
      localStorage.setItem(CONSENT_STORAGE_KEY, "yes-please")
      expect(getAnalyticsConsent()).toBeNull()
    })

    it("round-trips both decisions", () => {
      setAnalyticsConsent("granted")
      expect(getAnalyticsConsent()).toBe("granted")
      setAnalyticsConsent("denied")
      expect(getAnalyticsConsent()).toBe("denied")
    })
  })

  describe("trackEvent", () => {
    it("pushes nothing when GTM is not configured", () => {
      disableGtm()
      setAnalyticsConsent("granted")
      trackEvent("route_change", { page_path: "/" })
      expect(window.dataLayer).toEqual([])
    })

    it("pushes the event and params once consent is granted", () => {
      setAnalyticsConsent("granted")
      trackEvent("route_change", { page_path: "/public" })
      expect(window.dataLayer).toEqual([
        { event: "route_change", page_path: "/public" },
      ])
    })

    it("pushes nothing before the visitor has decided", () => {
      trackEvent("route_change", { page_path: "/" })
      expect(window.dataLayer).toEqual([])
    })

    it("pushes nothing after the visitor declined", () => {
      setAnalyticsConsent("denied")
      trackEvent("route_change", { page_path: "/" })
      trackEvent("login")
      expect(window.dataLayer).toEqual([])
    })

    it("does not throw when the GTM head snippet never ran", () => {
      delete window.dataLayer
      delete window.gtag
      setAnalyticsConsent("granted")
      expect(() => trackEvent("login")).not.toThrow()
    })

    it("does not let params override the event name", () => {
      setAnalyticsConsent("granted")
      trackEvent("login", { event: "spoofed" })
      expect(window.dataLayer).toEqual([{ event: "login" }])
    })
  })

  describe("setAnalyticsConsent", () => {
    it("flushes the event buffered before the decision when granted", () => {
      trackEvent("route_change", { page_path: "/login" })
      setAnalyticsConsent("granted")
      expect(window.dataLayer).toEqual([
        { event: "route_change", page_path: "/login" },
      ])
    })

    it("flushes every buffered event in order", () => {
      // A first visit to /public buffers the pageview and the page's own event.
      trackEvent("route_change", { page_path: "/public" })
      trackEvent("view_public_data")
      setAnalyticsConsent("granted")
      expect(window.dataLayer).toEqual([
        { event: "route_change", page_path: "/public" },
        { event: "view_public_data" },
      ])
    })

    it("caps the buffer instead of growing without bound", () => {
      for (let i = 0; i < 25; i++) {
        trackEvent("route_change", { page_path: `/page-${i}` })
      }
      setAnalyticsConsent("granted")
      expect(window.dataLayer).toHaveLength(10)
      expect(window.dataLayer?.[0]).toEqual({
        event: "route_change",
        page_path: "/page-0",
      })
    })

    it("discards the buffered event when denied", () => {
      trackEvent("route_change", { page_path: "/" })
      setAnalyticsConsent("denied")
      expect(window.dataLayer).toEqual([])
    })

    it("does not replay the buffered event on a later grant", () => {
      trackEvent("route_change", { page_path: "/" })
      setAnalyticsConsent("denied")
      setAnalyticsConsent("granted")
      expect(window.dataLayer).toEqual([])
    })

    it("updates gtag consent in both directions", () => {
      setAnalyticsConsent("granted")
      expect(gtag).toHaveBeenCalledWith("consent", "update", {
        analytics_storage: "granted",
      })

      setAnalyticsConsent("denied")
      expect(gtag).toHaveBeenLastCalledWith("consent", "update", {
        analytics_storage: "denied",
      })
    })

    it("honours the decision for the session when localStorage refuses writes", () => {
      const setItem = jest
        .spyOn(Storage.prototype, "setItem")
        .mockImplementation(() => {
          throw new Error("Storage disabled")
        })

      trackEvent("route_change", { page_path: "/" })
      setAnalyticsConsent("granted")

      expect(getAnalyticsConsent()).toBe("granted")
      trackEvent("login")
      expect(window.dataLayer).toEqual([
        { event: "route_change", page_path: "/" },
        { event: "login" },
      ])

      setItem.mockRestore()
    })
  })

  describe("initAnalyticsConsent", () => {
    it("re-applies a stored grant", () => {
      localStorage.setItem(CONSENT_STORAGE_KEY, "granted")
      initAnalyticsConsent()
      expect(gtag).toHaveBeenCalledWith("consent", "update", {
        analytics_storage: "granted",
      })
    })

    it("re-applies a stored denial", () => {
      localStorage.setItem(CONSENT_STORAGE_KEY, "denied")
      initAnalyticsConsent()
      expect(gtag).toHaveBeenCalledWith("consent", "update", {
        analytics_storage: "denied",
      })
    })

    it("does nothing with no stored decision", () => {
      initAnalyticsConsent()
      expect(gtag).not.toHaveBeenCalled()
    })

    it("does nothing when GTM is not configured", () => {
      disableGtm()
      localStorage.setItem(CONSENT_STORAGE_KEY, "granted")
      initAnalyticsConsent()
      expect(gtag).not.toHaveBeenCalled()
    })

    it("does not throw when the GTM head snippet never ran", () => {
      delete window.gtag
      localStorage.setItem(CONSENT_STORAGE_KEY, "granted")
      expect(() => initAnalyticsConsent()).not.toThrow()
    })
  })

  describe("index.html head snippet", () => {
    const inlineScripts = (
      fs
        .readFileSync(
          path.join(__dirname, "../../../public/index.html"),
          "utf8",
        )
        .match(/<script>[\s\S]*?<\/script>/g) ?? []
    )
      .map((tag) => tag.replace(/<\/?script>/g, ""))
      .filter((source) => /consent|gtm\.js/.test(source))

    // window.eval, not eval: only the former makes the gtag declaration a
    // property of window, which is the browser behaviour utils/analytics.ts
    // relies on. Returns the src of the container script the loader inserted.
    const runSnippet = (gtmId: string): string | null => {
      delete window.dataLayer
      delete window.gtag
      // The loader inserts itself before the first existing script tag.
      document.head.appendChild(document.createElement("script"))
      inlineScripts.forEach((source) =>
        window.eval(source.replace("%REACT_APP_GTM_ID%", gtmId)),
      )
      return (
        document.querySelector<HTMLScriptElement>("script[src]")?.src ?? null
      )
    }

    afterEach(() => {
      document.head.innerHTML = ""
    })

    it("finds both inline scripts", () => {
      expect(inlineScripts).toHaveLength(2)
    })

    it("defines the gtag shim that utils/analytics.ts calls through", () => {
      runSnippet(TEST_GTM_ID)
      expect(typeof window.gtag).toBe("function")
    })

    it("defaults every consent signal to denied before the container loads", () => {
      runSnippet(TEST_GTM_ID)
      // The shim forwards `arguments`, which is what gtm.js reads back.
      expect(Array.from(window.dataLayer?.[0] as IArguments)).toEqual([
        "consent",
        "default",
        {
          ad_storage: "denied",
          ad_user_data: "denied",
          ad_personalization: "denied",
          analytics_storage: "denied",
          wait_for_update: 500,
        },
      ])
    })

    it("loads the container for a well-formed id", () => {
      expect(runSnippet(TEST_GTM_ID)).toBe(
        `https://www.googletagmanager.com/gtm.js?id=${TEST_GTM_ID}`,
      )
    })

    it("loads nothing when CRA left the placeholder un-interpolated", () => {
      expect(runSnippet("%REACT_APP_GTM_ID%")).toBeNull()
    })

    it("rejects exactly the ids isGtmEnabled rejects", () => {
      // Divergence between the two guards is a consent notice on a site whose
      // container never loads, or the reverse.
      for (const id of ["", "gtm-lowercase", "GTM-", "GTM-ABC123&extra=1"]) {
        process.env.REACT_APP_GTM_ID = id
        expect(isGtmEnabled()).toBe(false)
        expect(runSnippet(id)).toBeNull()
      }
    })
  })

  describe("normalizePath", () => {
    it("leaves paths without numeric segments alone", () => {
      expect(normalizePath("/")).toBe("/")
      expect(normalizePath("/account-manager")).toBe("/account-manager")
      expect(normalizePath("/subscription/thanks")).toBe("/subscription/thanks")
    })

    it("collapses numeric segments", () => {
      expect(normalizePath("/workspaces/12")).toBe("/workspaces/:id")
      expect(normalizePath("/dataview/12")).toBe("/dataview/:id")
      expect(normalizePath("/workspaces/12/runs/34")).toBe(
        "/workspaces/:id/runs/:id",
      )
    })

    it("does not collapse segments that merely contain digits", () => {
      expect(normalizePath("/optinist2")).toBe("/optinist2")
      expect(normalizePath("/v1beta/thing")).toBe("/v1beta/thing")
    })
  })
})
