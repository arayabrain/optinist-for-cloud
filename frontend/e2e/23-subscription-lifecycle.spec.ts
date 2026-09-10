import { execFileSync } from "child_process"
import * as path from "path"

import { test, expect, Page, request } from "@playwright/test"

import {
  apiHeaders,
  apiUrl,
  authHeaders,
  confirmDialog,
  ERROR_RED,
  isLocalBaseUrl,
  login,
  REPO_ROOT,
  runSql,
  runSqlWriteOnDev,
  sqlLiteral,
  sqlSkipReason,
} from "./helpers"

// The subscription-state half of the lifecycle rows, split out of
// 11-lifecycle so it can run against a deployed environment.
//
// 11-lifecycle is local-stack only for a reason that applies to its *storage*
// tests and not to these: those dial a real sparse ballast file inside the
// backend container and measure what the app recalculates from disk, which
// needs docker. These seven only need a plan row and an expiry, and every
// assertion they make is a page render or a SELECT. Plan and expiry are the
// same two columns either way, so the only thing that had to change is the
// write path: one statement at a time through runSqlWriteOnDev, which is the
// sanctioned deployed-dev write, instead of the multi-statement runSql the
// docker DB accepts.
//
// Storage is pinned to zero usage rather than left alone: these rows are about
// the subscription caption, and a storage warning on top would cover the
// dialog under test. On a dedicated account usage really is zero, so the write
// only has to survive the login-time refresh, which recalculates to the same
// value.
//
//   npx playwright test e2e/23-subscription-lifecycle.spec.ts
//
// Needs its own account: LC-16 deletes the user it runs on, and these tests
// rewrite plan and expiry on every case, so pointing them at a shared premium
// fixture would corrupt it for every other spec.

const USER = {
  email: process.env.TEST_LIFECYCLE_EMAIL || "",
  password: process.env.TEST_LIFECYCLE_PASSWORD || "",
}

const GB = 1073741824
const PREMIUM_QUOTA = 200 * GB
// What registration actually leaves on a new account, and so what the cleanup
// below restores: plan 1, a 5GiB quota, expiration stamped at creation.
const FREE_QUOTA = 5 * GB

const userId = `(SELECT id FROM users WHERE email = '${sqlLiteral(USER.email)}')`

// One statement, and the deployed path refuses anything else - so plan and
// storage are written separately rather than as one script.
function write(sql: string) {
  return isLocalBaseUrl() ? runSql(sql) : runSqlWriteOnDev(sql)
}

function setPlan(planId: number, expiresIn: string, scheduledDowngrade = 0) {
  write(
    `UPDATE subscription_users SET plan_id = ${planId},
       expiration = DATE_ADD(UTC_TIMESTAMP(), ${expiresIn}),
       scheduled_downgrade = ${scheduledDowngrade}
       WHERE user_id = ${userId}`,
  )
}

function setStorage(usageBytes: number, quotaBytes: number) {
  write(
    `UPDATE user_storage_usage SET storage_usage_bytes = ${usageBytes},
       storage_quota_bytes = ${quotaBytes}, last_updated = UTC_TIMESTAMP()
       WHERE user_id = ${userId}`,
  )
}

async function loginKeepWarnings(page: Page) {
  await login(page, USER.email, USER.password, false)
}

// helpers.ts forces email_verified through the backend container, which a
// deployed run has no local copy of. Firebase is the shared dev project either
// way, so the same Admin SDK call works from here - it just needs an
// interpreter that has firebase_admin. Named rather than guessed, because the
// alternative is a test that fails for a missing dependency and reads like a
// product bug.
const FIREBASE_PYTHON = process.env.FIREBASE_ADMIN_PYTHON || "python3"

function firebaseAdminSkipReason(): string {
  try {
    execFileSync(FIREBASE_PYTHON, ["-c", "import firebase_admin"], {
      stdio: "ignore",
    })
    return ""
  } catch {
    return (
      `${FIREBASE_PYTHON} cannot import firebase_admin; set ` +
      `FIREBASE_ADMIN_PYTHON to an interpreter that can, to force ` +
      `email_verified on a deployed throwaway account`
    )
  }
}

function forceEmailVerified(email: string) {
  const key = path.join(
    REPO_ROOT,
    "studio",
    "config",
    "auth",
    "firebase_private.json",
  )
  execFileSync(
    FIREBASE_PYTHON,
    [
      "-c",
      "import sys,firebase_admin\n" +
        "from firebase_admin import auth, credentials\n" +
        "firebase_admin.initialize_app(credentials.Certificate(sys.argv[1]))\n" +
        "auth.update_user(auth.get_user_by_email(sys.argv[2]).uid, email_verified=True)\n",
      key,
      email,
    ],
    { stdio: "pipe" },
  )
}

const dialog = confirmDialog

const UNCOVERED_ELSEWHERE = "LC-06, LC-11..LC-13, LC-16, LC-21..LC-23"

test.describe.serial("Subscription state lifecycle", () => {
  let skipReason = ""

  test.beforeAll(async () => {
    if (!USER.email || !USER.password) {
      skipReason =
        `TEST_LIFECYCLE_EMAIL/TEST_LIFECYCLE_PASSWORD not set; leaves ` +
        `${UNCOVERED_ELSEWHERE} unverified`
      return
    }
    const sql = sqlSkipReason()
    if (sql) {
      skipReason = `${sql}; leaves ${UNCOVERED_ELSEWHERE} unverified`
      return
    }

    // The account must already exist and be verified: registering here would
    // leave an unverified Firebase user that cannot log in, and forcing
    // email_verified needs the Admin SDK rather than anything the API exposes.
    const api = await request.newContext({ baseURL: apiUrl() })
    try {
      const res = await api.post("/auth/login", {
        data: { email: USER.email, password: USER.password },
      })
      if (!res.ok()) {
        skipReason =
          `${USER.email} cannot log in (${res.status()}); the lifecycle ` +
          `account must exist and be email-verified. Leaves ` +
          `${UNCOVERED_ELSEWHERE} unverified`
        return
      }
      const { access_token, ex_token } = await res.json()
      const list = await api.get("/workspaces?offset=0&limit=100", {
        headers: authHeaders(access_token, ex_token),
      })
      expect(list.ok(), `GET /workspaces ${list.status()}`).toBeTruthy()
    } finally {
      await api.dispose()
    }

    // A subscription row is what setPlan updates, and a purchase row is what
    // marks the user as having once paid - without it the backend reports an
    // expired premium as a plain free user and the Expired-on caption and
    // Manage button never render. Any real formerly-premium user has both.
    write(
      `INSERT INTO subscription_users (plan_id, user_id, expiration)
         SELECT 2, ${userId}, DATE_ADD(UTC_TIMESTAMP(), INTERVAL 1 MONTH)
         WHERE NOT EXISTS
           (SELECT 1 FROM subscription_users WHERE user_id = ${userId})`,
    )
    write(
      `INSERT INTO subscription_user_purchases (plan_id, user_id)
         SELECT 2, ${userId} WHERE NOT EXISTS
           (SELECT 1 FROM subscription_user_purchases
              WHERE user_id = ${userId})`,
    )
  })

  test.beforeEach(() => {
    test.skip(!!skipReason, skipReason)
  })

  // Leave nothing fabricated behind. plan_id 2 on an account with no Stripe
  // customer is a premium user Stripe has never heard of, and unlike the
  // throwaway docker DB the original spec ran against, this row lives in a
  // shared development database where other people and the hourly sweeps read
  // it. Restores exactly what registration leaves, so the account is
  // indistinguishable from a fresh one between runs.
  test.afterAll(() => {
    if (skipReason) return
    write(
      `UPDATE subscription_users SET plan_id = 1, scheduled_downgrade = 0,
         expiration = UTC_TIMESTAMP() WHERE user_id = ${userId}`,
    )
    write(`DELETE FROM subscription_user_purchases WHERE user_id = ${userId}`)
    write(
      `UPDATE user_storage_usage SET storage_quota_bytes = ${FREE_QUOTA}
         WHERE user_id = ${userId}`,
    )
  })

  test("LC-06 - Expired premium (grace period): expiry warning on login", async ({
    page,
  }) => {
    setPlan(2, "INTERVAL -1 DAY")
    setStorage(0, PREMIUM_QUOTA)
    await loginKeepWarnings(page)

    const modal = dialog(page)
    await expect(
      modal.getByText("Premium Subscription Expired", { exact: true }),
    ).toBeVisible({ timeout: 30_000 })
    await expect(modal.getByRole("button", { name: "Upgrade" })).toBeVisible()
    await modal.getByRole("button", { name: "Handle later" }).click()
    await expect(modal).toBeHidden()

    // An expired premium account offers both recovery paths
    await page.goto("/account")
    await expect(page.locator("text=/\\(Expired on/").first()).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.locator('button:has-text("Upgrade")')).toBeVisible()
    await expect(page.locator('button:has-text("Manage")')).toBeVisible()
  })

  test("LC-11 - Expiration caption matches subscription state", async ({
    page,
  }) => {
    setStorage(0, PREMIUM_QUOTA)
    setPlan(2, "INTERVAL 1 MONTH")
    await loginKeepWarnings(page)

    await page.goto("/account")
    await expect(page.locator("text=/\\(Renew on/").first()).toBeVisible({
      timeout: 15_000,
    })

    setPlan(2, "INTERVAL 1 MONTH", 1)
    await page.goto("/account")
    await expect(page.locator("text=/\\(Expires on/").first()).toBeVisible({
      timeout: 15_000,
    })

    setPlan(2, "INTERVAL -1 DAY")
    await page.goto("/account")
    await expect(page.locator("text=/\\(Expired on/").first()).toBeVisible({
      timeout: 15_000,
    })
  })

  test("LC-12 - Downgrade opens a confirmation with retention notice; No aborts", async ({
    page,
  }) => {
    setStorage(0, PREMIUM_QUOTA)
    setPlan(2, "INTERVAL 1 MONTH")
    await loginKeepWarnings(page)

    await page.goto("/subscription")
    await page.locator('button:has-text("Downgrade")').click()

    const confirm = page.locator(
      '[role="dialog"]:has-text("Cancel Subscription")',
    )
    await expect(confirm).toBeVisible({ timeout: 15_000 })
    await expect(
      confirm.locator(".MuiDialogTitle-root"),
      "the dialog names the destructive action, not just the page",
    ).toHaveText("Cancel Subscription")
    await expect(confirm.getByText("Data Storage Notice:")).toBeVisible()
    await expect(confirm.getByText(/stored for 30 days/)).toBeVisible()
    // The Stripe-backed "Yes, Cancel Subscription" path is STRIPE-01's
    await confirm.getByRole("button", { name: "No" }).click()
    await expect(confirm).toBeHidden()
    await expect(
      page.locator('button:has-text("Current Plan")').first(),
    ).toBeVisible()
  })

  test("LC-21 - Profile after cancellation still reads Premium", async ({
    page,
  }) => {
    setStorage(0, PREMIUM_QUOTA)
    setPlan(2, "INTERVAL 1 MONTH", 1)
    await loginKeepWarnings(page)

    await page.goto("/account")
    // The status field itself, not the word anywhere on the page
    await expect(page.locator('[data-testid="account-plan-name"]')).toHaveText(
      "Premium",
      { timeout: 15_000 },
    )
    // Cancelled but not yet lapsed: the caption says Expires, not Expired, names
    // the stored expiration, and the only action is Manage - there is nothing to
    // upgrade to
    const expires = runSql(
      `SELECT DATE_FORMAT(expiration, '%c/%e/%Y') FROM subscription_users
         WHERE user_id = ${userId};`,
    )
    await expect(page.locator(`text=(Expires on ${expires})`)).toBeVisible()
    await expect(page.locator('button:has-text("Manage")')).toBeVisible()
    await expect(page.locator('button:has-text("Upgrade")')).toBeHidden()
  })

  test("LC-22 - After renewal the plan stays Premium with a later expiry", async ({
    page,
  }) => {
    setStorage(0, PREMIUM_QUOTA)
    setPlan(2, "INTERVAL 2 DAY")
    await loginKeepWarnings(page)

    // The renewal writes a new period end and nothing else
    setPlan(2, "INTERVAL 1 MONTH")
    await page.goto("/account")
    await expect(page.locator("text=Premium").first()).toBeVisible({
      timeout: 15_000,
    })
    // The caption has to name the stored expiration, not just some later date.
    // It renders the stored UTC date in the browser's short locale format, and
    // the run pins that timezone to UTC
    const renewedOn = runSql(
      `SELECT DATE_FORMAT(expiration, '%c/%e/%Y') FROM subscription_users
         WHERE user_id = ${userId};`,
    )
    await expect(
      page.locator(`text=/\\(Renew on\\s+${renewedOn}/`).first(),
    ).toBeVisible()

    await page.goto("/subscription")
    await expect(
      page.locator('button:has-text("Current Plan")').first(),
    ).toBeVisible({ timeout: 15_000 })
    await expect(page.locator("text=Subscription Canceled:")).toBeHidden()
  })

  test("LC-23 - Past the grace the account reads Expired, not Free", async ({
    page,
  }) => {
    // Past expiry plus the 30-day grace. No row is downgraded; the tier is
    // derived from the expiration.
    setStorage(0, PREMIUM_QUOTA)
    setPlan(2, "INTERVAL -40 DAY")
    const meSeen = page.waitForResponse(
      (r) => r.url().endsWith("/users/me") && r.request().method() === "GET",
    )
    await loginKeepWarnings(page)

    const me = await (await meSeen).json()
    expect(me.subscription_status).toBe("Expired")
    // The row itself is untouched; only the derived status changed
    expect(
      runSql(
        `SELECT plan_id FROM subscription_users WHERE user_id = ${userId};`,
      ),
    ).toBe("2")

    // Rows 290 / 291: Expired must also mean refused capability, not just a
    // caption - the assign route's tier guard runs before any AWS call
    const refused = await page.request.post(
      `${apiUrl()}/users/me/premium/assign`,
      { headers: await apiHeaders(page), failOnStatusCode: false },
    )
    expect(refused.status(), await refused.text()).toBe(403)
    expect(await refused.text()).toContain("Premium subscription required")

    // The expiry modal carries its own Upgrade button and re-renders on this
    // route, so dismiss the one on the page under test, not the dashboard's
    await page.goto("/account")
    const modal = dialog(page)
    await expect(
      modal.getByText("Premium Subscription Expired", { exact: true }),
    ).toBeVisible({ timeout: 30_000 })
    await modal.getByRole("button", { name: "Handle later" }).click()
    await expect(modal).toBeHidden()

    await expect(page.locator("text=/\\(Expired on/").first()).toBeVisible({
      timeout: 15_000,
    })
  })

  test("LC-13 - Cancelled subscription shows banner and Continue Plan", async ({
    page,
  }) => {
    setStorage(0, PREMIUM_QUOTA)
    setPlan(2, "INTERVAL 1 MONTH", 1)
    await loginKeepWarnings(page)

    await page.goto("/subscription")
    await expect(
      page.locator("text=Subscription Canceled:").first(),
    ).toBeVisible({ timeout: 15_000 })
    // Clicking it (reactivation) is STRIPE-01's job - display check only
    await expect(page.locator('button:has-text("Continue Plan")')).toBeVisible()
  })

  test("LC-16 - Account deletion deactivates the user", async ({ page }) => {
    const firebase = firebaseAdminSkipReason()
    test.skip(!!firebase, `rows 300-303: ${firebase}`)
    // A register, a real deletion pipeline, and polling two SELECTs that each
    // cost an SSM round trip on a deployed run - none of which fits 60s
    test.setTimeout(420_000)

    // A per-run throwaway account: the flow destroys it, and a timestamped
    // address avoids collisions with leftovers from crashed runs
    const email = `e2e_delete_${Date.now()}@test.com`
    const api = await request.newContext({ baseURL: apiUrl() })
    try {
      const reg = await api.post("/api/register", {
        data: {
          name: "E2E Delete Me",
          role_id: 20,
          email,
          password: USER.password,
        },
      })
      expect(reg.ok(), `POST /api/register: ${await reg.text()}`).toBeTruthy()
    } finally {
      await api.dispose()
    }
    forceEmailVerified(email)

    await login(page, email, USER.password, false)
    await page.goto("/account")
    const deleteAccount = page.locator('button:has-text("Delete Account")')
    await expect(deleteAccount).toBeEnabled()
    await expect(
      deleteAccount,
      "the deletion option is styled as the destructive one",
    ).toHaveCSS("background-color", ERROR_RED)
    await deleteAccount.click()

    const confirm = dialog(page)
    // The free-tier warning, exactly: the two subscription lines belong to the
    // premium copy and must not appear for an account that has no subscription
    // to lose.
    await expect(confirm.locator("li")).toHaveText(
      [
        "All your data (workspaces, experiments, files) will be permanently deleted",
        "This action cannot be undone",
      ],
      { timeout: 15_000 },
    )
    await confirm.locator('input[placeholder="DELETE"]').fill("DELETE")
    await confirm.getByRole("button", { name: "Delete My Account" }).click()

    // The account is deactivated and a deletion record is written; the
    // step pipeline completes asynchronously
    await expect
      .poll(
        () =>
          runSql(
            `SELECT active FROM users WHERE email = '${sqlLiteral(email)}';`,
          ),
        { timeout: 120_000, intervals: [5_000] },
      )
      .toBe("0")
    await expect
      .poll(
        () =>
          runSql(
            `SELECT COUNT(*) FROM user_deletion_records
               WHERE user_id = (SELECT id FROM users
                                  WHERE email = '${sqlLiteral(email)}')
               AND status = 'completed';`,
          ),
        { timeout: 120_000, intervals: [5_000] },
      )
      .not.toBe("0")
  })
})
