import { safeLocalStorage } from "utils/safeStorage"

export const CONSENT_STORAGE_KEY = "analyticsConsent"

export type ConsentDecision = "granted" | "denied"

type AnalyticsEvent = {
  event: string
  params?: Record<string, unknown>
}

const MAX_PENDING_EVENTS = 10

// Must stay identical to the copies in public/index.html, config-overrides.js and ecr_build_push.sh: divergence means a consent notice with nothing behind it.
const GTM_ID_PATTERN = /^GTM-[A-Z0-9]+$/

// Events raised before the visitor answered the banner, so accepting still
// records the entry pageview instead of losing it.
let _pending: AnalyticsEvent[] = []

// Survives a localStorage that refuses writes (Safari "block all cookies",
// private modes), which would otherwise leave GTM granted but nothing pushed.
let _sessionConsent: ConsentDecision | null = null

export function isGtmEnabled(): boolean {
  return GTM_ID_PATTERN.test(process.env.REACT_APP_GTM_ID ?? "")
}

export function getAnalyticsConsent(): ConsentDecision | null {
  if (_sessionConsent) return _sessionConsent
  const stored = safeLocalStorage.getItem(CONSENT_STORAGE_KEY)
  return stored === "granted" || stored === "denied" ? stored : null
}

function updateGtagConsent(decision: ConsentDecision): void {
  window.gtag?.("consent", "update", { analytics_storage: decision })
}

export function trackEvent(
  event: string,
  params?: Record<string, unknown>,
): void {
  if (!isGtmEnabled()) return

  const consent = getAnalyticsConsent()
  if (consent === null) {
    if (_pending.length < MAX_PENDING_EVENTS) _pending.push({ event, params })
    return
  }
  if (consent === "denied") return

  window.dataLayer?.push({ ...params, event })
}

// Module state alone is not reactive, so a component that read the consent at
// mount never learns about a decision made elsewhere in the same document --
// the banner and the Account page render side by side on a first visit.
const CONSENT_CHANGE_EVENT = "analytics-consent-change"

export function subscribeAnalyticsConsent(
  listener: (decision: ConsentDecision) => void,
): () => void {
  const handler = (event: Event) =>
    listener((event as CustomEvent<ConsentDecision>).detail)
  window.addEventListener(CONSENT_CHANGE_EVENT, handler)
  return () => window.removeEventListener(CONSENT_CHANGE_EVENT, handler)
}

export function setAnalyticsConsent(decision: ConsentDecision): void {
  _sessionConsent = decision
  safeLocalStorage.setItem(CONSENT_STORAGE_KEY, decision)
  updateGtagConsent(decision)
  window.dispatchEvent(
    new CustomEvent<ConsentDecision>(CONSENT_CHANGE_EVENT, {
      detail: decision,
    }),
  )

  const pending = _pending
  _pending = []
  if (decision === "granted") {
    pending.forEach(({ event, params }) => trackEvent(event, params))
  }
}

export function initAnalyticsConsent(): void {
  if (!isGtmEnabled()) return
  const consent = getAnalyticsConsent()
  if (consent) updateGtagConsent(consent)
}

export function normalizePath(pathname: string): string {
  return pathname.replace(/\/\d+(?=\/|$)/g, "/:id")
}

export function _resetForTesting(): void {
  _pending = []
  _sessionConsent = null
}
