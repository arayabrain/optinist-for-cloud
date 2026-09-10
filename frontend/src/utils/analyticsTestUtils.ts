import { jest } from "@jest/globals"

import { _resetForTesting } from "utils/analytics"

export const TEST_GTM_ID = "GTM-TESTID"

// A dotenv-provided container ID leaks into the test environment, so every
// suite must pin and restore this rather than trusting the ambient value.
const originalGtmId = process.env.REACT_APP_GTM_ID

export function setUpAnalyticsTest() {
  localStorage.clear()
  _resetForTesting()
  process.env.REACT_APP_GTM_ID = TEST_GTM_ID
  window.dataLayer = []
  const gtag = jest.fn()
  window.gtag = gtag as unknown as Window["gtag"]
  return gtag
}

export function tearDownAnalyticsTest(): void {
  if (originalGtmId === undefined) {
    delete process.env.REACT_APP_GTM_ID
  } else {
    process.env.REACT_APP_GTM_ID = originalGtmId
  }
  delete window.dataLayer
  delete window.gtag
}

export function disableGtm(): void {
  delete process.env.REACT_APP_GTM_ID
}
