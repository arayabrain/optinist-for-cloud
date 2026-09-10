import { test, expect, Page } from "@playwright/test"

import {
  FREE_USER,
  PREMIUM_USER,
  freeStorageState,
  goToWorkspaces,
  gotoDashboard,
  login,
  logout,
  mockPremiumAssignment,
  routeGate,
  skipWithoutCreds,
} from "./helpers"

// Storage warnings on login (manual storage reload is covered by WS-04;
// over-limit/threshold states by 11-lifecycle on a local stack), plus the
// storage bar colours, warning-dismissal persistence, the reload button's
// in-flight state, and the premium scaling notice.
// Left manual: S3 verification, auto-refresh network tracing.

test("STO-01 - Free user under limit logs in without storage warning", async ({
  page,
}) => {
  skipWithoutCreds()
  // The modal is rendered from this response, so a hidden-modal assertion made
  // before it resolves passes for an over-quota user too
  const warningSeen = page.waitForResponse(
    (r) => r.url().endsWith("/storage-limit-alerts/limit-warning"),
    { timeout: 60_000 },
  )
  await page.goto("/login")
  await page.locator('[data-testid="email"]').fill(FREE_USER.email)
  await page.locator('[data-testid="password"]').fill(FREE_USER.password)
  await page.locator('[data-testid="button-submit"]').click()
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 })

  // No alert for this account is the premise; the modal staying away is the
  // claim. Both are asserted so a broken premise cannot read as a pass.
  const warning = await (await warningSeen).json()
  expect(warning?.has_alert ?? false).toBe(false)
  // The dashboard having rendered is what makes the absence an absence:
  // `toBeHidden` resolves on its first poll for an element that has not been
  // given the chance to render yet.
  await expect(page.getByRole("link", { name: "Workspaces" })).toBeVisible({
    timeout: 30_000,
  })
  await expect(page.locator("text=Storage Limit Exceeded")).toBeHidden()
})

// The endpoint answers `null` with a 200 for an account with no alert, so the
// negative above is also satisfied by a backend that says nothing at all. No
// free account can be put over quota on demand from the UI, so the direction
// that renders the modal is pinned on a fulfilled response instead.
const OVER_QUOTA_ALERT = {
  has_alert: true,
  alert_type: "storage",
  days_remaining: 7,
  excess_data_bytes: 1073741824,
  excess_data_gb: 1,
  storage_usage_bytes: 6442450944,
  storage_usage_gb: 6,
  storage_quota_bytes: 5368709120,
  storage_quota_gb: 5,
  deletion_date: "2099-01-01T00:00:00Z",
  message: "Storage limit exceeded",
}

test("STO-04 - An over-quota alert opens the storage modal on login", async ({
  page,
}) => {
  skipWithoutCreds()
  await page.route("**/storage-limit-alerts/limit-warning", (route) =>
    route.fulfill({ json: OVER_QUOTA_ALERT }),
  )
  // dismissWarning=false: the modal under test is the one login() would close
  await login(page, FREE_USER.email, FREE_USER.password, false)

  // Exact: the alert body repeats the title in lower case, and getByText is
  // case-insensitive without it
  const modal = page.locator('[role="dialog"]')
  await expect(
    modal.getByText("Storage Limit Exceeded", { exact: true }),
  ).toBeVisible({ timeout: 30_000 })
  await expect(
    modal.getByRole("button", { name: "Manage Files" }),
  ).toBeVisible()
})

// One literal, so a copy change cannot quietly defang the negative assertion
// in the other test
const DEDICATED_SNACKBAR = "Premium instance assigned successfully"
const PREPARING_SNACKBAR =
  "Please wait while your dedicated premium resource is being prepared"

const premiumShared = (page: Page) =>
  page.evaluate(() => localStorage.getItem("premium_shared"))

test("STO-02 - Premium login shows an assignment snackbar", async ({
  page,
}) => {
  skipWithoutCreds(PREMIUM_USER, "TEST_PREMIUM_EMAIL/TEST_PREMIUM_PASSWORD")

  // The real assignment flow depends on the backend's AWS access (with
  // credentials it fails into a fallback snackbar on local stacks; without
  // them it 500s silently), so it isn't assertable outside a deployed env
  // and stays a manual check there. Mock the assigned state everywhere and
  // verify the frontend announces it.
  await mockPremiumAssignment(page)
  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)

  await expect(page.locator(`text=${DEDICATED_SNACKBAR}`).first()).toBeVisible({
    timeout: 30_000,
  })
  await expect.poll(() => premiumShared(page)).toBe("false")
})

test("STO-03 - Premium login on shared resources records the fallback", async ({
  page,
}) => {
  skipWithoutCreds(PREMIUM_USER, "TEST_PREMIUM_EMAIL/TEST_PREMIUM_PASSWORD")

  await mockPremiumAssignment(page, { shared: true })
  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)

  // The only state that distinguishes "landed on shared" from "still being
  // assigned": both show the same notice, so the notice alone proves nothing
  await expect.poll(() => premiumShared(page)).toBe("true")
  await expect(page.locator(`text=${PREPARING_SNACKBAR}`).first()).toBeVisible({
    timeout: 30_000,
  })
  // Announcing a dedicated instance while on shared resources is the bug this
  // guards; give the success effect a chance to fire before ruling it out
  await page.waitForTimeout(1_000)
  await expect(page.locator(`text=${DEDICATED_SNACKBAR}`)).toHaveCount(0)
})

test("STO-09 - Scaling in progress keeps the preparing notice up", async ({
  page,
}) => {
  skipWithoutCreds(PREMIUM_USER, "TEST_PREMIUM_EMAIL/TEST_PREMIUM_PASSWORD")

  // No assignment yet and a retryable assign response: the instance is still
  // being started, which is neither the dedicated nor the shared-fallback state
  await mockPremiumAssignment(page, { scaling: true })
  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)

  await expect(page.locator(`text=${PREPARING_SNACKBAR}`).first()).toBeVisible({
    timeout: 30_000,
  })
  // Scaling must not announce an instance or record any routing verdict, and
  // the notice must persist rather than flash
  await page.waitForTimeout(1_000)
  await expect(page.locator(`text=${PREPARING_SNACKBAR}`).first()).toBeVisible()
  await expect(page.locator(`text=${DEDICATED_SNACKBAR}`)).toHaveCount(0)
  expect(await premiumShared(page)).toBeNull()
})

// ---------------------------------------------------------------------------
// Storage panel detail on /workspaces: bar colours, warning dismissal, and
// the reload button's in-flight state. These reuse the saved session - none
// of them is about login itself.
// ---------------------------------------------------------------------------

const usageAt = (percent: number) => ({
  storage_usage_bytes: 1073741824,
  storage_usage_formatted: "1.0 GB",
  storage_quota_bytes: 5368709120,
  storage_quota_formatted: "5.0 GB",
  storage_usage_percent: percent,
  alert_level: null,
  thresholds: { critical: 100, danger: 90 },
})

test.describe("Storage panel detail", () => {
  test.use({ storageState: freeStorageState() })

  test("STO-05 - Storage bar colour tracks the usage thresholds", async ({
    page,
  }) => {
    skipWithoutCreds()
    let percent = 0
    await page.route("**/storage-limit-alerts/usage", (route) =>
      route.fulfill({ json: usageAt(percent) }),
    )

    // StorageUsage.test.tsx reads its expected colours from theme.palette, so
    // a wrong palette cannot fail it; these literals are what the rows pin.
    // Theme.ts sets no palette, so they are MUI's stock primary/warning/error
    // mains and move only on a MUI major. 90 and 100 sit on the >= thresholds.
    const cases: [number, string][] = [
      [50, "rgb(25, 118, 210)"], // primary below 90
      [90, "rgb(237, 108, 2)"], // warning from exactly 90
      [95, "rgb(237, 108, 2)"],
      [100, "rgb(211, 47, 47)"], // error from exactly 100
      [105, "rgb(211, 47, 47)"],
    ]
    for (const [level, colour] of cases) {
      percent = level
      await page.goto("/workspaces")
      // The printed percent stays real even where the bar itself caps
      const readout = page.getByText(`${level.toFixed(1)}%`)
      await expect(readout).toBeVisible({ timeout: 15_000 })
      const track = readout.locator(
        "xpath=preceding-sibling::span[contains(@class,'MuiLinearProgress-root')]",
      )
      await expect(track.locator("span").first()).toHaveCSS(
        "background-color",
        colour,
      )
      // The bar's fill value caps at 100
      await expect(track).toHaveAttribute(
        "aria-valuenow",
        String(Math.min(level, 100)),
      )
    }
  })

  test("STO-06 - A dismissed storage warning stays dismissed on later pages", async ({
    page,
  }) => {
    skipWithoutCreds()
    await page.route("**/storage-limit-alerts/limit-warning", (route) =>
      route.fulfill({ json: OVER_QUOTA_ALERT }),
    )
    await page.goto("/dashboard")
    const modal = page.locator('[role="dialog"]')
    await expect(
      modal.getByText("Storage Limit Exceeded", { exact: true }),
    ).toBeVisible({ timeout: 30_000 })
    await modal.getByRole("button", { name: "Handle later" }).click()
    await expect(modal).toBeHidden()

    // Full page loads remount the alert, so staying hidden proves the
    // dismissal was persisted rather than held in component state. The
    // absence is read only after the alert fetch resolved - before it, a
    // broken dismissal also shows nothing (the STO-01 hazard).
    const pages: [string, ReturnType<Page["locator"]>][] = [
      ["/workspaces", page.locator('button:has-text("Reload")')],
      ["/dashboard", page.getByRole("link", { name: "Workspaces" })],
    ]
    for (const [path, pin] of pages) {
      const alertChecked = page.waitForResponse(
        (r) => r.url().endsWith("/storage-limit-alerts/limit-warning"),
        { timeout: 30_000 },
      )
      await page.goto(path)
      await alertChecked
      await expect(pin).toBeVisible({ timeout: 30_000 })
      await expect(page.getByText("Storage Limit Exceeded")).toHaveCount(0)
    }
  })

  test("STO-07 - Logout clears the dismissal so the warning returns", async ({
    page,
  }) => {
    skipWithoutCreds()
    await page.route("**/storage-limit-alerts/limit-warning", (route) =>
      route.fulfill({ json: OVER_QUOTA_ALERT }),
    )
    await page.goto("/dashboard")
    const modal = page.locator('[role="dialog"]')
    await expect(
      modal.getByText("Storage Limit Exceeded", { exact: true }),
    ).toBeVisible({ timeout: 30_000 })
    await modal.getByRole("button", { name: "Handle later" }).click()
    await expect(modal).toBeHidden()

    // Persisted before the logout, cleared by it (the user slice's logout
    // reducer): without the before-state the after-state proves nothing
    expect(
      await page.evaluate(() => localStorage.getItem("dismissedAlerts")),
    ).not.toBeNull()
    await logout(page)
    expect(
      await page.evaluate(() => localStorage.getItem("dismissedAlerts")),
    ).toBeNull()
    await login(page, FREE_USER.email, FREE_USER.password, false)
    await expect(
      modal.getByText("Storage Limit Exceeded", { exact: true }),
    ).toBeVisible({ timeout: 30_000 })
  })

  test("STO-08 - Reload is disabled with a spinner while refreshing", async ({
    page,
  }) => {
    skipWithoutCreds()
    // Hold the refresh open until the in-flight state has been observed
    const refresh = routeGate()
    await page.route("**/workspaces/refresh-storage", async (route) => {
      await refresh.held
      await route.continue()
    })
    await gotoDashboard(page)
    await goToWorkspaces(page)

    const reload = page.locator('button:has-text("Reload")')
    await expect(reload).toBeEnabled({ timeout: 15_000 })
    await reload.click()
    await expect(reload).toBeDisabled()
    await expect(reload.locator(".MuiCircularProgress-root")).toBeVisible()
    // Completes and re-arms
    refresh.release()
    await expect(reload).toBeEnabled({ timeout: 30_000 })
  })
})
