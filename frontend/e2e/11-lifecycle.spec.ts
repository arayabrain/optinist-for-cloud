import * as fs from "fs"
import * as path from "path"

import { test, expect, Page, request } from "@playwright/test"

import {
  apiHeaders,
  apiUrl,
  authHeaders,
  confirmDialog,
  ensureTutorialRecords,
  ensureWorkspaceId,
  isLocalBaseUrl,
  login,
  logout,
  mockPremiumAssignment,
  openWorkspace,
  REPO_ROOT,
  reproduceTutorial,
  runInBackend,
  runInDeployedBackend,
  runSql,
  runSqlWriteOnDev,
  sqlLiteral,
  sqlSkipReason,
  verifyEmail,
} from "./helpers"

// The subscription/storage warning lifecycle, on the local docker stack AND on
// deployed dev.
//
// Plan and expiry are driven directly in the database (the same knobs the
// README documents for manual account bootstrap) because there is no Stripe in
// either environment: multi-statement through docker locally, one statement at
// a time through `runSqlWriteOnDev` on deployed dev.
//
// Storage usage, however, must be REAL: on every login the app recalculates
// usage from the workspace and overwrites the DB value (Layout's per-session
// refresh), so a faked usage number never survives to the warning check. The
// two environments reach that from opposite directions:
//
//   local     a sparse ballast file (zero disk cost; folder sizes sum st_size)
//             sits in the user's workspace, and each test dials
//             storage_quota_bytes to put the measured real usage at the
//             percentage under test.
//   deployed  the same recalculation reads S3 object sizes, where nothing is
//             sparse. Two provisioned fixture accounts already hold the data,
//             so those rows rent their state instead of creating it:
//               TEST_PREMIUM_OVER_*  active premium already over its own quota
//               TEST_GRACE_OVER_*    expired premium holding >5GiB of real S3
//                                    data, above the hardcoded free limit
//
// Rows whose assertions are environment-independent share one body and branch
// only on setup, so a deployed run and a local run verify the same thing.
//
// On a local run every reason this group cannot execute is a broken
// environment, and it FAILS rather than skipping: several LC rows have no
// coverage but this spec, and a skipped row reads as a pass on the sheet.
// Rows that genuinely cannot run in the current environment skip individually
// with a reason naming what they leave unverified.

const USER = {
  email: process.env.TEST_LIFECYCLE_EMAIL || "",
  password: process.env.TEST_LIFECYCLE_PASSWORD || "",
}
// Provisioned deployed fixtures; unused on a local run
const PREMIUM_OVER = {
  email: process.env.TEST_PREMIUM_OVER_EMAIL || "",
  password: process.env.TEST_PREMIUM_OVER_PASSWORD || "",
}
const GRACE_OVER = {
  email: process.env.TEST_GRACE_OVER_EMAIL || "",
  password: process.env.TEST_GRACE_OVER_PASSWORD || "",
}
// A real formerly-premium account carrying 6.9GiB of provisioned S3 data.
// Dropping its premium row is a genuine downgrade: determine_lifecycle then
// reports FREE, which is the state LC-08 is about.
const DOWNGRADE = {
  email: process.env.TEST_FREE_DOWNGRADE_EMAIL || "",
  password: process.env.TEST_FREE_DOWNGRADE_PASSWORD || "",
}

const LOCAL = isLocalBaseUrl()
const PREMIUM_PLAN_ID = 2
// The instance the cleanup rows seed their assignment against. No real worker
// ever resolves this id, and DataCleanupJob filters on
// `instance_id == resolve_instance_id()`, so a row carrying it is invisible to
// every scheduled sweep - which is what makes backdating logged_out_at safe on
// shared dev rather than a way to get this account's data deleted mid-test.
const E2E_INSTANCE = "i-e2e"

const GB = 1073741824
const FREE_QUOTA = 5 * GB
const PREMIUM_QUOTA = 200 * GB
// An expired premium account is held to the free-tier limit whatever its quota
// record says (`_effective_quota_bytes`), so the combined grace + over-quota
// state needs real usage above that limit rather than a dialed quota
const OVER_FREE_QUOTA = 6 * GB
// Well under the 5GiB free quota (even with sample data imported on top) so
// the ballast only "exceeds" when a test shrinks the quota; also under the
// hardcoded free limit that grace/overdue states compare against, keeping
// those popups subscription-only
const BALLAST = 3 * GB

// Measured in beforeAll via the same backend recalculation the app runs on
// login. Quotas are dialed from this rather than from BALLAST because the
// workspace accumulates other real data (sample import for LC-09/10), and
// the login-time refresh overwrites any faked usage with reality.
let realUsage = 0
const overQuota = () => Math.floor(realUsage / 1.1) // usage ≈ 110%
const nearQuota = () => Math.floor(realUsage / 0.95) // usage ≈ 95%

const userIdOf = (email: string) =>
  `(SELECT id FROM users WHERE email = '${sqlLiteral(email)}')`
const userId = userIdOf(USER.email)

// The docker DB takes a script; the deployed write path takes one statement.
function write(sql: string): string {
  return LOCAL ? runSql(sql) : runSqlWriteOnDev(sql)
}

// Primes the cached value so the warning check agrees with the ballast even
// before the login-time refresh has run (20-minute freshness window)
function setStorage(usageBytes: number, quotaBytes: number) {
  write(
    `UPDATE user_storage_usage SET storage_usage_bytes = ${usageBytes},
       storage_quota_bytes = ${quotaBytes}, last_updated = UTC_TIMESTAMP()
       WHERE user_id = ${userId}`,
  )
}

// expiresIn examples: "INTERVAL 1 MONTH", "INTERVAL -1 DAY"
function setPlan(planId: number, expiresIn: string, scheduledDowngrade = 0) {
  write(
    `UPDATE subscription_users SET plan_id = ${planId},
       expiration = DATE_ADD(UTC_TIMESTAMP(), ${expiresIn}),
       scheduled_downgrade = ${scheduledDowngrade}
       WHERE user_id = ${userId}`,
  )
}

let workspaceId = 0
const ballastPath = () =>
  `/app/studio_data/input/${workspaceId}/e2e-ballast.bin`

// Always (re)sizes rather than only creating, so a test that grew the ballast
// cannot leak that size into the next one
function ensureBallast(bytes = BALLAST) {
  runInBackend(
    `sh -c "mkdir -p /app/studio_data/input/${workspaceId} && ` +
      `rm -f ${ballastPath()} && ` +
      `dd if=/dev/zero of=${ballastPath()} bs=1 count=0 seek=${bytes} 2>/dev/null"`,
  )
}

function removeBallast() {
  runInBackend(`rm -f ${ballastPath()}`)
}

// Register + verify the dedicated lifecycle user if it doesn't exist yet,
// and find-or-create its ballast workspace. Idempotent: an "already
// registered" response falls through to re-verify and log in, so a
// half-created user from an aborted run self-heals.
async function ensureUserAndWorkspace() {
  const api = await request.newContext({ baseURL: apiUrl() })
  try {
    const creds = { email: USER.email, password: USER.password }
    let loginRes = await api.post("/auth/login", { data: creds })
    if (!loginRes.ok()) {
      await api.post("/api/register", {
        data: { name: "E2E Lifecycle", role_id: 20, ...creds },
      })
      verifyEmail(USER.email)
      loginRes = await api.post("/auth/login", { data: creds })
      if (!loginRes.ok()) {
        throw new Error(
          `Lifecycle user bootstrap failed (login ${loginRes.status()}): ` +
            `${await loginRes.text()}`,
        )
      }
    }
    const { access_token, ex_token } = await loginRes.json()
    const headers = authHeaders(access_token, ex_token)
    const list = await api.get("/workspaces?offset=0&limit=100", { headers })
    if (!list.ok()) {
      throw new Error(`GET /workspaces ${list.status()}: ${await list.text()}`)
    }
    const { items } = await list.json()
    const found = items.find(
      (w: { name: string }) => w.name === "e2e-lifecycle",
    )
    if (found) {
      workspaceId = found.id
    } else {
      const created = await api.post("/workspace", {
        headers,
        data: { name: "e2e-lifecycle" },
      })
      workspaceId = (await created.json()).id
    }

    ensureBallast()
    // Recalculate real usage (ballast + whatever else the workspace holds)
    // exactly the way the app does on login, then read the result back
    await api.post("/workspaces/refresh-storage", { headers })
    realUsage = Number(
      runSql(
        `SELECT storage_usage_bytes FROM user_storage_usage
           WHERE user_id = ${userId};`,
      ),
    )
    if (!realUsage) {
      throw new Error("storage measurement returned 0 — ballast missing?")
    }
  } finally {
    await api.dispose()
  }
}

// Deployed dev has no Admin SDK reachable from here, so the account must
// already exist and be verified; registering would leave an unverified
// Firebase user that cannot log in. Fails loudly rather than skipping: a
// missing account is a broken lane, not an absent capability.
async function ensureDeployedAccount() {
  const api = await request.newContext({ baseURL: apiUrl() })
  try {
    const res = await api.post("/auth/login", {
      data: { email: USER.email, password: USER.password },
    })
    expect(
      res.ok(),
      `${USER.email} cannot log in (${res.status()}); the lifecycle account ` +
        `must exist and be email-verified on deployed dev`,
    ).toBeTruthy()
  } finally {
    await api.dispose()
  }
}

// The cleanup job's selection joins Workspace, so an account with none is
// never eligible however its assignment row is stamped - a silent vacuous pass.
// LC-05 deletes the shared workspace mid-run, so the cleanup rows ask for one
// themselves rather than depending on which siblings ran before them.
async function ensureDeployedWorkspace() {
  // The job's own join predicate, not a proxy for it: GET /workspaces answers a
  // different question (it can list rows this join will not match), and a
  // mismatch here reads as "the grace period did not select the user" rather
  // than "the account owns no workspace".
  const ownedLive = () =>
    runSql(
      `SELECT COUNT(*) FROM workspaces
         WHERE user_id = ${userId} AND deleted = 0`,
    ).trim()
  if (ownedLive() !== "0") return

  const api = await request.newContext({ baseURL: apiUrl() })
  try {
    const res = await api.post("/auth/login", {
      data: { email: USER.email, password: USER.password },
    })
    expect(res.ok(), `${USER.email} cannot log in`).toBeTruthy()
    const { access_token, ex_token } = await res.json()
    const created = await api.post("/workspace", {
      headers: authHeaders(access_token, ex_token),
      data: { name: "e2e-lifecycle" },
    })
    expect(created.ok(), `POST /workspace ${created.status()}`).toBeTruthy()
  } finally {
    await api.dispose()
  }
  expect(
    ownedLive(),
    "created a workspace the cleanup join still cannot see",
  ).not.toBe("0")
}

// login() with dismissWarning=false so the warning modals under test are
// still open when the assertions run
async function loginKeepWarnings(page: Page) {
  await login(page, USER.email, USER.password, false)
}

// Same locator the admin spec uses
const dialog = confirmDialog

// Sign-off rows this group alone covers, named in the reason so a run that does
// not execute it says which rows it left unverified
const UNCOVERED_ELSEWHERE =
  "LC-01..LC-05, LC-07..LC-10, LC-14..LC-15, LC-17..LC-20, LC-24..LC-31"
// Rows whose state is rented from the provisioned deployed fixtures
const FIXTURE_ROWS = "LC-03, LC-04, LC-08, LC-18..LC-20"

test.describe.serial("Subscription/storage warning lifecycle", () => {
  let skipReason = ""
  let fixtureReason = ""
  let originalExpiration = ""
  let originalPremiumQuota = ""
  let downgradeRestore: string[] = []

  function unrunnable(reason: string) {
    if (LOCAL) {
      throw new Error(`${reason}; ${UNCOVERED_ELSEWHERE} cannot run`)
    }
    skipReason = `${reason}; leaves ${UNCOVERED_ELSEWHERE} unverified`
  }

  // Every fixture precondition is read from the database rather than assumed:
  // an account whose provisioned S3 data was cleared still logs in fine, and
  // would turn the rows that rent it into vacuous passes.
  function fixtureDrift(email: string, wantActive: boolean): string {
    const cell = (sql: string) => runSql(sql).trim()
    const rows = cell(
      `SELECT COUNT(*) FROM subscription_users WHERE user_id = ${userIdOf(email)}
         AND plan_id = ${PREMIUM_PLAN_ID}`,
    )
    if (rows !== "1") {
      return `${email} has ${rows} premium subscription rows, expected exactly 1`
    }
    const future = cell(
      `SELECT expiration > UTC_TIMESTAMP() FROM subscription_users
         WHERE user_id = ${userIdOf(email)} AND plan_id = ${PREMIUM_PLAN_ID}`,
    )
    if (future !== (wantActive ? "1" : "0")) {
      return `${email} expiration is on the wrong side of now for this fixture`
    }
    const over = cell(
      `SELECT storage_usage_bytes > ${wantActive ? "storage_quota_bytes" : FREE_QUOTA}
         FROM user_storage_usage WHERE user_id = ${userIdOf(email)}`,
    )
    if (over !== "1") {
      return (
        `${email} is no longer over its ${wantActive ? "own" : "free-tier"} quota; ` +
        `the provisioned S3 data has been removed or the quota was raised`
      )
    }
    return ""
  }

  // GRACE is expiry..expiry+30d, so a statically provisioned expiry rots into
  // OVERDUE on its own. Re-stamped per run, restored in afterAll.
  function stampGraceFixture() {
    originalExpiration = runSql(
      `SELECT expiration FROM subscription_users
         WHERE user_id = ${userIdOf(GRACE_OVER.email)}
         AND plan_id = ${PREMIUM_PLAN_ID}`,
    ).trim()
    expect(originalExpiration, "stored expiration to restore").toMatch(
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/,
    )
    runSqlWriteOnDev(
      `UPDATE subscription_users
         SET expiration = DATE_SUB(UTC_TIMESTAMP(), INTERVAL 1 DAY)
         WHERE user_id = ${userIdOf(GRACE_OVER.email)}
         AND plan_id = ${PREMIUM_PLAN_ID}`,
    )
  }

  // Puts the provisioned over-quota fixture at an exact usage ratio by moving
  // its quota, never its data: the S3 bytes are the expensive half.
  function dialFixtureQuota(ratio: number) {
    const cell = (col: string) =>
      runSql(
        `SELECT ${col} FROM user_storage_usage
           WHERE user_id = ${userIdOf(PREMIUM_OVER.email)}`,
      ).trim()
    if (!originalPremiumQuota) {
      originalPremiumQuota = cell("storage_quota_bytes")
      expect(originalPremiumQuota, "fixture quota to restore").toMatch(/^\d+$/)
    }
    const usage = Number(cell("storage_usage_bytes"))
    expect(usage, "fixture usage").toBeGreaterThan(0)
    runSqlWriteOnDev(
      `UPDATE user_storage_usage
         SET storage_quota_bytes = ${Math.floor(usage / ratio)},
             last_updated = UTC_TIMESTAMP()
         WHERE user_id = ${userIdOf(PREMIUM_OVER.email)}`,
    )
  }

  // A real downgrade: the premium row goes, so determine_lifecycle reports
  // FREE and the free-tier limit becomes the effective quota. The account's
  // provisioned data is untouched - only its plan moves.
  function stageDowngradedFreeUser() {
    const uid = userIdOf(DOWNGRADE.email)
    const plan = runSql(
      `SELECT plan_id FROM subscription_users WHERE user_id = ${uid}`,
    ).trim()
    const quota = runSql(
      `SELECT storage_quota_bytes FROM user_storage_usage WHERE user_id = ${uid}`,
    ).trim()
    expect(plan, "downgrade fixture plan to restore").toMatch(/^\d+$/)
    expect(quota, "downgrade fixture quota to restore").toMatch(/^\d+$/)
    downgradeRestore = [
      `UPDATE subscription_users SET plan_id = ${plan} WHERE user_id = ${uid}`,
      `UPDATE user_storage_usage SET storage_quota_bytes = ${quota} WHERE user_id = ${uid}`,
    ]
    runSqlWriteOnDev(
      `UPDATE subscription_users SET plan_id = 1 WHERE user_id = ${uid}`,
    )
    runSqlWriteOnDev(
      `UPDATE user_storage_usage SET storage_quota_bytes = ${FREE_QUOTA}
         WHERE user_id = ${uid}`,
    )
  }

  // Deployed, the account's own imported sample data plays the ballast's part:
  // real S3 objects it owns, that globalSetup clears at the start of each run.
  // Only the quota moves to reach the ratio under test.
  async function refreshAndMeasure(page: Page): Promise<number> {
    const res = await page.request.post(
      `${apiUrl()}/workspaces/refresh-storage`,
      { headers: await apiHeaders(page) },
    )
    expect(res.ok(), await res.text()).toBe(true)
    // The endpoint answers success:true even when every workspace threw (the
    // loop continues past them) and the user-total update was caught and only
    // logged, so a stale usage figure would satisfy the >0 check below. The
    // counts are the only proof the recalculation actually ran.
    const body = await res.json()
    expect(body.total_workspaces, "workspaces to refresh").toBeGreaterThan(0)
    expect(body.refreshed_workspaces, "workspaces refreshed").toBe(
      body.total_workspaces,
    )
    const bytes = Number(
      runSql(
        `SELECT storage_usage_bytes FROM user_storage_usage
           WHERE user_id = ${userId}`,
      ).trim(),
    )
    expect(bytes, "measured usage after the sample import").toBeGreaterThan(0)
    return bytes
  }

  function setQuota(bytes: number) {
    write(
      `UPDATE user_storage_usage SET storage_quota_bytes = ${bytes},
         last_updated = UTC_TIMESTAMP() WHERE user_id = ${userId}`,
    )
  }

  test.beforeAll(async () => {
    if (!USER.email || !USER.password) {
      unrunnable("TEST_LIFECYCLE_EMAIL/TEST_LIFECYCLE_PASSWORD not set")
      return
    }
    const sql = sqlSkipReason()
    if (sql) {
      unrunnable(sql)
      return
    }
    if (LOCAL) {
      // The local-testing default in studio/config/.env disables every storage
      // lookup backend-side (deployed envs run with it false), so no warning
      // under test can ever fire while it's on
      const backendEnv = path.join(REPO_ROOT, "studio", "config", ".env")
      if (
        fs.existsSync(backendEnv) &&
        /^SKIP_STORAGE_CHECKS=true/m.test(fs.readFileSync(backendEnv, "utf-8"))
      ) {
        unrunnable(
          "SKIP_STORAGE_CHECKS=true in studio/config/.env disables storage warnings",
        )
        return
      }
      await ensureUserAndWorkspace()
    } else {
      await ensureDeployedAccount()
      if (!PREMIUM_OVER.email || !GRACE_OVER.email || !DOWNGRADE.email) {
        fixtureReason =
          `TEST_PREMIUM_OVER_* / TEST_GRACE_OVER_* / TEST_FREE_DOWNGRADE_* not ` +
          `set; leaves ${FIXTURE_ROWS} unverified`
      } else {
        const overFree = runSql(
          `SELECT storage_usage_bytes > ${FREE_QUOTA} FROM user_storage_usage
             WHERE user_id = ${userIdOf(DOWNGRADE.email)}`,
        ).trim()
        const drift =
          fixtureDrift(PREMIUM_OVER.email, true) ||
          fixtureDrift(GRACE_OVER.email, false) ||
          (overFree === "1"
            ? ""
            : `${DOWNGRADE.email} no longer holds more than the free-tier ` +
              `limit, so a downgrade would not put it over quota`)
        if (drift) {
          fixtureReason = `${drift}; leaves ${FIXTURE_ROWS} unverified`
        } else {
          stampGraceFixture()
        }
      }
    }
    // A purchase row is what marks the user as having once paid; without it
    // the backend reports an expired premium as a plain free user and the
    // expired-state UI (Expired-on caption, Manage button) never renders.
    // Any real formerly-premium user has one.
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

  // Rows that cannot run in this environment say so individually, so the skip
  // summary names them rather than reporting the whole group as unverified.
  // The storage-state rows: ballast-driven locally (guaranteed by beforeAll),
  // fixture-driven deployed, where the fixtures can be absent or drifted.
  function needsStorageState() {
    test.skip(!LOCAL && !!fixtureReason, fixtureReason)
  }

  // A test that grew the ballast must not leave it grown: the percentages every
  // later test dials are derived from a measurement taken at the default size,
  // and an inline restore does not run when the test fails
  test.afterEach(() => {
    if (!skipReason && LOCAL) ensureBallast()
  })

  test.afterAll(() => {
    // Every restore first, assertions only after: a failed check must not
    // abort the restores behind it and leave a fixture permanently staged on
    // shared dev, where the next run reads the drift as missing S3 data.
    if (originalPremiumQuota) {
      runSqlWriteOnDev(
        `UPDATE user_storage_usage
           SET storage_quota_bytes = ${originalPremiumQuota}
           WHERE user_id = ${userIdOf(PREMIUM_OVER.email)}`,
      )
    }
    for (const sql of downgradeRestore) runSqlWriteOnDev(sql)
    if (originalExpiration) {
      runSqlWriteOnDev(
        `UPDATE subscription_users SET expiration = '${originalExpiration}'
           WHERE user_id = ${userIdOf(GRACE_OVER.email)}
           AND plan_id = ${PREMIUM_PLAN_ID}`,
      )
    }
    if (originalPremiumQuota) {
      expect(
        runSql(
          `SELECT storage_quota_bytes FROM user_storage_usage
             WHERE user_id = ${userIdOf(PREMIUM_OVER.email)}`,
        ).trim(),
        "the over-quota fixture's quota was restored",
      ).toBe(originalPremiumQuota)
    }
    if (originalExpiration) {
      expect(
        runSql(
          `SELECT expiration FROM subscription_users
             WHERE user_id = ${userIdOf(GRACE_OVER.email)}
             AND plan_id = ${PREMIUM_PLAN_ID}`,
        ).trim(),
        "the grace fixture's expiration was restored",
      ).toBe(originalExpiration)
    }
    if (skipReason) return
    // Leave the user as a clean free account for the next run, without the
    // fake instance rows LC-24/25 seeded
    if (LOCAL) removeBallast()
    setPlan(1, "INTERVAL 0 DAY")
    setStorage(0, FREE_QUOTA)
    write(`DELETE FROM free_user_assignments WHERE user_id = ${userId}`)
    write(
      `DELETE FROM instance_usage_log WHERE user_id = ${userId}
         AND instance_id = '${E2E_INSTANCE}'`,
    )
  })

  test("LC-01 - Free baseline: no warning, account shows Free", async ({
    page,
  }) => {
    setPlan(1, "INTERVAL 0 DAY")
    setStorage(realUsage, FREE_QUOTA)
    await loginKeepWarnings(page)

    await expect(dialog(page)).toBeHidden()
    await page.goto("/account")
    await expect(page.locator("text=Free").first()).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.locator('button:has-text("Upgrade")')).toBeVisible()
  })

  test("LC-02 - Upgrade to premium: account shows Premium, no warning", async ({
    page,
  }) => {
    setPlan(2, "INTERVAL 1 MONTH")
    setStorage(realUsage, PREMIUM_QUOTA)
    // The plan is what this row asserts; the assignment that a premium login
    // would otherwise trigger is not. Unmocked, a deployed run claims a real
    // t3.large with its own ALB target group and never releases it.
    await mockPremiumAssignment(page)
    await loginKeepWarnings(page)

    await expect(dialog(page)).toBeHidden()
    await page.goto("/account")
    await expect(page.locator("text=Premium").first()).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.locator('button:has-text("Manage")')).toBeVisible()
  })

  test("LC-03 - Premium over quota: Storage Limit Exceeded modal on login", async ({
    page,
  }) => {
    needsStorageState()
    test.setTimeout(120_000)
    // Locally the ballast is dialed to 110%; deployed, a provisioned account
    // is already over its own quota. The assertions below are the same either
    // way, which is the point of renting the fixture rather than faking it.
    const user = LOCAL ? USER : PREMIUM_OVER
    if (LOCAL) {
      ensureBallast()
      setPlan(2, "INTERVAL 1 MONTH")
      setStorage(realUsage, overQuota())
    }
    // The fixture really is premium, so an unmocked login would claim a real
    // instance and an ALB target group for it. The modal under test comes from
    // the limit-warning payload, which the mock does not touch.
    await mockPremiumAssignment(page)
    const warningSeen = page.waitForResponse((r) =>
      r.url().endsWith("/storage-limit-alerts/limit-warning"),
    )
    await login(page, user.email, user.password, false)

    const warning = await (await warningSeen).json()
    expect(warning.alert_type).toBe("storage")
    expect(warning.excess_data_gb).toBeGreaterThan(0)
    // An active premium is held to its own quota, so there is no deletion
    // timeline; that is what makes the Upgrade button absent below.
    expect(warning.deletion_date).toBeNull()

    const modal = dialog(page)
    await expect(modal.getByText("Storage Limit Exceeded")).toBeVisible({
      timeout: 30_000,
    })
    await expect(
      modal.getByRole("button", { name: "Manage Files" }),
    ).toBeVisible()
    // Active premium can't upgrade its way out of an over-quota state
    await expect(modal.getByRole("button", { name: "Upgrade" })).toBeHidden()
    // Handle later dismisses and stays on the dashboard
    await modal.getByRole("button", { name: "Handle later" }).click()
    await expect(modal).toBeHidden()
    await expect(page).toHaveURL(/\/dashboard/)

    await page.goto("/workspaces")
    await expect(page.getByText("Storage usage is over quota")).toBeVisible({
      timeout: 30_000,
    })
  })

  test("LC-04 - Premium at 95% quota: no modal, usage-high indicator only", async ({
    page,
  }) => {
    needsStorageState()
    test.setTimeout(120_000)
    const user = LOCAL ? USER : PREMIUM_OVER
    if (LOCAL) {
      ensureBallast()
      setPlan(2, "INTERVAL 1 MONTH")
      setStorage(realUsage, nearQuota())
    } else {
      dialFixtureQuota(0.95)
    }
    await mockPremiumAssignment(page)
    await login(page, user.email, user.password, false)

    await page.goto("/workspaces")
    await expect(page.getByText("Storage usage is high")).toBeVisible({
      timeout: 30_000,
    })
    await expect(dialog(page)).toBeHidden()
  })

  test("LC-05 - Storage reload picks up freed space and clears the warning", async ({
    page,
  }) => {
    test.setTimeout(300_000)
    if (LOCAL) {
      setPlan(2, "INTERVAL 1 MONTH")
      ensureBallast()
      setStorage(realUsage, nearQuota())
      await loginKeepWarnings(page)
    } else {
      // The subject is the recalculation, not the plan. Staying on free keeps
      // this row from claiming a real premium instance, and it uploads real
      // data so the mocked-assignment routing is the wrong tool here.
      setPlan(1, "INTERVAL 1 MONTH")
      setStorage(0, FREE_QUOTA)
      await loginKeepWarnings(page)
      // Give the account real data of its own rather than renting a fixture's:
      // this row deletes what it measures, which no provisioned account can lend
      await openWorkspace(page, "e2e-lifecycle")
      await ensureTutorialRecords(page, "e2e-lifecycle")
      setQuota(Math.floor((await refreshAndMeasure(page)) / 0.95))
    }

    await page.goto("/workspaces")
    await expect(page.getByText("Storage usage is high")).toBeVisible({
      timeout: 30_000,
    })
    // Free the space for real, then reload - the recalculation should clear
    // the warning state
    if (LOCAL) {
      removeBallast()
    } else {
      const id = await ensureWorkspaceId(page, "e2e-lifecycle")
      const deleted = await page.request.delete(`${apiUrl()}/workspace/${id}`, {
        headers: await apiHeaders(page),
      })
      expect(deleted.ok(), await deleted.text()).toBe(true)
    }
    await page.locator('button:has-text("Reload")').click()
    await expect(page.locator("text=/Storage refreshed/i").first()).toBeVisible(
      { timeout: 60_000 },
    )
    await expect(page.getByText("Storage usage is high")).toBeHidden()
  })

  test("LC-07 - Long-expired premium: overdue modal requires acknowledgment", async ({
    page,
  }) => {
    // Past expiry + 30-day grace + 30-day warning window → OVERDUE
    setPlan(2, "INTERVAL -70 DAY")
    setStorage(0, PREMIUM_QUOTA)
    await loginKeepWarnings(page)

    const modal = dialog(page)
    await expect(modal.getByText("Urgent: Data Deletion Imminent")).toBeVisible(
      { timeout: 30_000 },
    )
    await expect(modal.getByText("Data Cleanup Overdue")).toBeVisible()
    const remindLater = modal.getByRole("button", {
      name: "I understand, remind me later",
    })
    await expect(remindLater).toBeDisabled()
    await modal.getByRole("checkbox").check()
    await expect(remindLater).toBeEnabled()
    await remindLater.click()
    await expect(modal).toBeHidden()
  })

  test("LC-08 - Downgraded free user over quota: warning offers upgrade", async ({
    page,
  }) => {
    needsStorageState()
    test.setTimeout(120_000)
    const user = LOCAL ? USER : DOWNGRADE
    if (LOCAL) {
      ensureBallast()
      setPlan(1, "INTERVAL 0 DAY")
      setStorage(realUsage, overQuota())
    } else {
      stageDowngradedFreeUser()
    }
    await login(page, user.email, user.password, false)

    const modal = dialog(page)
    await expect(modal.getByText("Storage Limit Exceeded")).toBeVisible({
      timeout: 30_000,
    })
    // Unlike LC-03, a free user gets the upgrade path and a deletion window
    await expect(modal.getByRole("button", { name: "Upgrade" })).toBeVisible()
    await expect(modal.getByText("Time Remaining")).toBeVisible()
    // Manage Files dismisses and lands on the workspace list
    await modal.getByRole("button", { name: "Manage Files" }).click()
    await expect(modal).toBeHidden()
    await expect(page).toHaveURL(/\/workspaces/, { timeout: 15_000 })
  })

  // Expired premium still inside the grace window AND over the free limit: the
  // one combined state neither the grace-only nor the free-over-quota case
  // reaches. The ballast has to grow for real, because the effective quota is
  // the hardcoded free limit rather than the quota column - no amount of dialing
  // storage_quota_bytes puts a grace user over it.
  async function loginExpiredPremiumOverQuota(page: Page) {
    // A real login, whose retry ladder is up to 45s, on top of the DB writes and
    // a ballast resize: the default 60s budget can expire during setup
    test.setTimeout(120_000)
    needsStorageState()
    if (!LOCAL) {
      // The fixture already holds >5GiB of real S3 data and was stamped back
      // into the grace window by beforeAll; nothing to stage per test.
      await login(page, GRACE_OVER.email, GRACE_OVER.password, false)
      return
    }
    ensureBallast(OVER_FREE_QUOTA)
    setPlan(2, "INTERVAL -1 DAY")
    setStorage(OVER_FREE_QUOTA, PREMIUM_QUOTA)
    await loginKeepWarnings(page)
  }

  test("LC-18 - Expired premium in grace and over quota: combined warning", async ({
    page,
  }) => {
    // Not the sibling `/limit-warning/check`, which returns a different shape
    const warningSeen = page.waitForResponse((r) =>
      r.url().endsWith("/storage-limit-alerts/limit-warning"),
    )
    await loginExpiredPremiumOverQuota(page)

    // The payload carries the whole deletion timeline; the modal renders only
    // the expiry date and the time remaining, so assert the dates where they
    // actually exist.
    const warning = await (await warningSeen).json()
    expect(warning.alert_type).toBe("grace")
    expect(warning.subscription_end_date).toBeTruthy()
    expect(warning.grace_end_date).toBeTruthy()
    expect(warning.deletion_date).toBeTruthy()
    // The quota that bites is the free one, not the premium 200GB on the record
    expect(warning.storage_quota_gb).toBe(FREE_QUOTA / GB)
    expect(warning.excess_data_gb).toBeGreaterThan(0)

    const modal = dialog(page)
    // The subscription-expiry variant, not the plain storage one
    await expect(
      modal.getByText("Premium Subscription Expired", { exact: true }),
    ).toBeVisible({ timeout: 30_000 })
    await expect(modal.getByText(/exceeds the free plan limit/)).toBeVisible()
    await expect(modal.getByText("Time Remaining")).toBeVisible()
    // Both recovery paths: pay again, or delete data
    await expect(modal.getByRole("button", { name: "Upgrade" })).toBeVisible()
    await expect(
      modal.getByRole("button", { name: "Manage Files" }),
    ).toBeVisible()
  })

  test("LC-19 - Handle later on the expiry warning returns to the dashboard", async ({
    page,
  }) => {
    await loginExpiredPremiumOverQuota(page)

    const modal = dialog(page)
    await expect(
      modal.getByText("Premium Subscription Expired", { exact: true }),
    ).toBeVisible({ timeout: 30_000 })
    await modal.getByRole("button", { name: "Handle later" }).click()
    await expect(modal).toBeHidden()
    await expect(page).toHaveURL(/\/dashboard/)
  })

  test("LC-20 - Upgrade on the expiry warning lands on /subscription", async ({
    page,
  }) => {
    await loginExpiredPremiumOverQuota(page)

    const modal = dialog(page)
    await expect(
      modal.getByText("Premium Subscription Expired", { exact: true }),
    ).toBeVisible({ timeout: 30_000 })
    await modal.getByRole("button", { name: "Upgrade" }).click()
    await expect(modal).toBeHidden()
    await expect(page).toHaveURL(/\/subscription$/, { timeout: 15_000 })
  })

  // LC-09/10 need a runnable workflow: sample data is imported into the
  // lifecycle workspace on first need (records persist across runs), then
  // Tutorial1 is reproduced. Quota is only shrunk AFTER the import — uploads
  // are themselves rejected over quota.
  async function loadRunnableWorkflow(page: Page) {
    await openWorkspace(page, "e2e-lifecycle")
    await ensureTutorialRecords(page, "e2e-lifecycle")
    await reproduceTutorial(page, "Tutorial1")
  }

  const runAllButton = (page: Page) =>
    page.locator('button:has-text("RUN ALL"), button:has-text("RUN")').first()
  const runNameDialog = (page: Page) =>
    page.locator('[role="dialog"]:has-text("Name and run workflow")')

  // After a reproduce the split button defaults to "RUN" (rerun by uid),
  // which starts immediately with no name dialog — switch it to "RUN ALL"
  // so the not-blocked case stops at the dialog instead of really running
  async function selectRunAllMode(page: Page) {
    await page.locator('button:has([data-testid="ArrowDropDownIcon"])').click()
    await page.locator('li:has-text("RUN ALL")').click()
    await expect(page.locator('button:has-text("RUN ALL")')).toBeVisible()
  }

  test("LC-09 - RUN over quota is blocked before the run dialog", async ({
    page,
  }) => {
    test.setTimeout(300_000)
    if (LOCAL) {
      setPlan(2, "INTERVAL 1 MONTH")
      ensureBallast()
      setStorage(realUsage, PREMIUM_QUOTA)
    } else {
      // The run gate reads the effective quota, which for a free account is
      // the quota column - so this row needs no premium plan, and staying off
      // it means no real instance is claimed. It imports sample data, so the
      // mocked-assignment routing the other rows use is not an option here.
      setPlan(1, "INTERVAL 1 MONTH")
      setStorage(0, FREE_QUOTA)
    }
    await loginKeepWarnings(page)
    await loadRunnableWorkflow(page)
    await selectRunAllMode(page)

    if (LOCAL) {
      setStorage(realUsage, overQuota())
    } else {
      setQuota(Math.floor((await refreshAndMeasure(page)) / 1.1))
    }
    await runAllButton(page).click()
    await expect(
      page.locator("text=Cannot run job: Storage quota exceeded").first(),
    ).toBeVisible({ timeout: 15_000 })
    await expect(runNameDialog(page)).toBeHidden()
  })

  test("LC-10 - RUN at 95% quota warns but is not blocked", async ({
    page,
  }) => {
    test.setTimeout(300_000)
    if (LOCAL) {
      setPlan(2, "INTERVAL 1 MONTH")
      ensureBallast()
      setStorage(realUsage, PREMIUM_QUOTA)
    } else {
      // The run gate reads the effective quota, which for a free account is
      // the quota column - so this row needs no premium plan, and staying off
      // it means no real instance is claimed. It imports sample data, so the
      // mocked-assignment routing the other rows use is not an option here.
      setPlan(1, "INTERVAL 1 MONTH")
      setStorage(0, FREE_QUOTA)
    }
    await loginKeepWarnings(page)
    await loadRunnableWorkflow(page)
    await selectRunAllMode(page)

    if (LOCAL) {
      setStorage(realUsage, nearQuota())
    } else {
      setQuota(Math.floor((await refreshAndMeasure(page)) / 0.95))
    }
    await runAllButton(page).click()
    // "Storage usage is high" on its own is also the storage panel's caption;
    // this is the run-time snackbar, percentage and advice included
    await expect(
      page
        .locator(
          "text=/^Warning: Storage usage is high \\(\\d+\\.\\d% used\\)\\. Consider freeing up space\\.$/",
        )
        .first(),
    ).toBeVisible({ timeout: 15_000 })
    // The run-name dialog opening proves the run was not blocked; cancel it
    // so no real workflow executes
    await expect(runNameDialog(page)).toBeVisible({ timeout: 15_000 })
    await runNameDialog(page).getByRole("button", { name: "Cancel" }).click()
    await expect(runNameDialog(page)).toBeHidden()
  })

  // The inactivity monitor only arms while the frontend holds a premium
  // assignment, which never succeeds locally (no ECS) — mock the premium
  // endpoints so the frontend believes it is assigned, then drive the
  // timers with the fake clock. This automates the manual "Triggering the
  // Inactivity Warning via DevTools" Date.now() override from the System
  // test sheet's Reference tab.
  // Log in with the fake clock installed and wait until the (mocked)
  // assignment has been processed, so the inactivity interval is armed
  // before the clock jumps
  async function loginWithArmedInactivityMonitor(page: Page) {
    await page.clock.install()
    const statusSeen = page.waitForResponse((r) =>
      r.url().includes("/users/me/premium/status"),
    )
    await loginKeepWarnings(page)
    await statusSeen
    await page.waitForTimeout(1_000)
  }

  test("LC-14 - Inactivity warning after 1h; Stay Active resets it", async ({
    page,
  }) => {
    setStorage(0, PREMIUM_QUOTA)
    setPlan(2, "INTERVAL 1 MONTH")

    // 65 min is past the 1h warning threshold but below the 2h auto-release
    await mockPremiumAssignment(page)
    await loginWithArmedInactivityMonitor(page)

    await page.clock.fastForward(65 * 60 * 1000)
    const warning = page.locator("text=Premium Instance Inactivity Warning")
    await expect(warning).toBeVisible({ timeout: 15_000 })

    await page.getByRole("button", { name: "Stay Active" }).click()
    await expect(warning).toBeHidden({ timeout: 15_000 })

    // Timer reset: another 30 fake minutes stay quiet (next warning is a
    // full hour from the Stay Active click)
    await page.clock.fastForward(30 * 60 * 1000)
    await expect(warning).toBeHidden()
  })

  // Mutation check: LC-14 above is the intact-session variant, the same
  // arming with a live session must dismiss the warning instead
  test("LC-31 - Stay Active on a dead session: real 401, Session Expired", async ({
    page,
  }) => {
    setStorage(0, PREMIUM_QUOTA)
    setPlan(2, "INTERVAL 1 MONTH")

    // LC-14's arming, but the heartbeat reaches the real backend: the leg
    // under test is the 401 handling, so nothing may fulfill the auth path
    await mockPremiumAssignment(page)
    await page.unroute("**/users/me/premium/heartbeat")
    await loginWithArmedInactivityMonitor(page)

    await page.clock.fastForward(65 * 60 * 1000)
    const warning = page.locator("text=Premium Instance Inactivity Warning")
    await expect(warning).toBeVisible({ timeout: 15_000 })

    // Deactivating the row is the one real-backend state where the heartbeat
    // still answers 401 after a successful token refresh: a dead access token
    // alone is cured by the refresh, and a dead refresh token makes the axios
    // interceptor itself log out (on the refresh 400) before the alert flips
    write(
      `UPDATE users SET active = 0
         WHERE email = '${sqlLiteral(USER.email)}'`,
    )
    try {
      // The corrupted token makes the first heartbeat a genuine dead-token
      // 401 (ExToken is ignored locally; auth is the Firebase bearer token)
      await page.evaluate(() =>
        localStorage.setItem("access_token", "e2e-dead-token"),
      )

      const heartbeat401 = page.waitForResponse(
        (r) =>
          r.url().includes("/users/me/premium/heartbeat") && r.status() === 401,
        { timeout: 15_000 },
      )
      // A real click's pointerdown feeds the window activity listener, which
      // closes the snackbar before the expired copy can render; dispatching
      // only the click event isolates the Stay Active handler under test
      await page
        .getByRole("button", { name: "Stay Active" })
        .dispatchEvent("click")
      await heartbeat401

      // The alert flips only after the retry ladder exhausts (3 attempts
      // with backed-off sleeps), so this visibility wait spans real seconds
      const expired = page
        .locator(".MuiAlert-filledError")
        .filter({ hasText: "Session Expired" })
      await expect(expired).toBeVisible({ timeout: 15_000 })
      await expect(expired).toContainText(
        "Your session has expired. Redirecting to login...",
      )
      await expect(
        page.getByRole("button", { name: "Stay Active" }),
      ).toBeHidden()

      // The component holds the copy up for a 2s read delay, then logs out
      await expect(page).toHaveURL(/\/login/, { timeout: 15_000 })
    } finally {
      write(
        `UPDATE users SET active = 1
           WHERE email = '${sqlLiteral(USER.email)}'`,
      )
    }
  })

  test("LC-15 - 2h inactivity auto-releases the instance via beacon", async ({
    page,
  }) => {
    setStorage(0, PREMIUM_QUOTA)
    setPlan(2, "INTERVAL 1 MONTH")

    await mockPremiumAssignment(page)
    // The release path is a sendBeacon POST — the same plumbing the
    // browser-close release uses. Mock it and watch for the call.
    await page.route("**/users/me/premium/release-beacon", (route) =>
      route.fulfill({ json: { released: true } }),
    )
    await loginWithArmedInactivityMonitor(page)

    const beaconFired = page.waitForRequest(
      (r) => r.url().includes("/users/me/premium/release-beacon"),
      { timeout: 15_000 },
    )
    // Past the 2h threshold in one jump: the check must release, not warn
    await page.clock.fastForward(125 * 60 * 1000)
    await beaconFired
    await expect(
      page.locator("text=Premium Instance Inactivity Warning"),
    ).toBeHidden()
  })

  test("LC-17 - Release clears every routing key; a gesture re-seeds them", async ({
    page,
  }) => {
    setStorage(0, PREMIUM_QUOTA)
    setPlan(2, "INTERVAL 1 MONTH")

    await mockPremiumAssignment(page)
    await page.route("**/users/me/premium/release-beacon", (route) =>
      route.fulfill({ json: { released: true } }),
    )
    await loginWithArmedInactivityMonitor(page)

    const routingKeys = () =>
      page.evaluate(() => ({
        routingId: localStorage.getItem("routing_id"),
        assigned: localStorage.getItem("premium_assigned"),
        instanceId: localStorage.getItem("premium_instance_id"),
        shared: localStorage.getItem("premium_shared"),
      }))

    await expect.poll(async () => (await routingKeys()).assigned).toBe("true")

    // Release boundary: 2h of inactivity fires the release beacon.
    const beaconFired = page.waitForRequest(
      (r) => r.url().includes("/users/me/premium/release-beacon"),
      { timeout: 15_000 },
    )
    await page.clock.fastForward(125 * 60 * 1000)
    await beaconFired

    // Every routing key is cleared so premium_assigned never outlives the
    // token (the unrecoverable pair).
    await expect.poll(routingKeys).toEqual({
      routingId: null,
      assigned: "false",
      instanceId: null,
      shared: "false",
    })

    // Reassign boundary: a gesture re-runs auto-assign.
    await page.mouse.click(5, 5)
    await expect.poll(async () => (await routingKeys()).assigned).toBe("true")

    // Logout boundary: teardown clears the token and drops the assigned flag.
    await logout(page)
    await expect.poll(async () => (await routingKeys()).routingId).toBe(null)
    expect((await routingKeys()).assigned).not.toBe("true")
  })

  // The local stack never creates these rows on its own (the activity
  // middleware refuses instance_id "local"), so the assignment a deployed
  // login would have made is seeded directly; the endpoints under test only
  // ever UPDATE them.
  function seedFreeAssignment(loggedOut: boolean) {
    const stamp = loggedOut ? "UTC_TIMESTAMP()" : "NULL"
    for (const sql of [
      `DELETE FROM free_user_assignments WHERE user_id = ${userId}`,
      `DELETE FROM instance_usage_log WHERE user_id = ${userId}
         AND instance_id = '${E2E_INSTANCE}'`,
      `INSERT INTO free_user_assignments
         (user_id, instance_id, assigned_at, last_activity, logged_out_at)
         VALUES (${userId}, '${E2E_INSTANCE}', UTC_TIMESTAMP(),
                 UTC_TIMESTAMP(), ${stamp})`,
      `INSERT INTO instance_usage_log
         (user_id, instance_id, tier, started_at, ended_at)
         VALUES (${userId}, '${E2E_INSTANCE}', 'free', UTC_TIMESTAMP(), ${stamp})`,
    ]) {
      write(sql)
    }
  }

  test("LC-24 - Free logout stamps the assignment and closes the usage log", async ({
    page,
  }) => {
    setPlan(1, "INTERVAL 1 MONTH")
    // The local stack never creates the row itself (the activity middleware
    // refuses instance_id "local"); a deployed login makes a real one.
    if (LOCAL) seedFreeAssignment(false)
    await login(page, USER.email, USER.password)
    if (!LOCAL) {
      expect(
        runSql(
          `SELECT COUNT(*) FROM free_user_assignments WHERE user_id = ${userId}`,
        ).trim(),
        "the deployed login did not create a free assignment row",
      ).toBe("1")
    }

    const freeLogout = page.waitForResponse(
      (r) =>
        r.url().includes("/users/me/free/logout") &&
        r.request().method() === "POST",
      { timeout: 30_000 },
    )
    await logout(page)
    const response = await freeLogout
    expect(response.status()).toBe(200)
    expect((await response.json()).logged_out).toBe(true)

    // logout() asserted the redirect; the session tokens must be gone too
    expect(
      await page.evaluate(() => localStorage.getItem("access_token")),
    ).toBeNull()

    expect(
      runSql(
        `SELECT logged_out_at IS NOT NULL FROM free_user_assignments
           WHERE user_id = ${userId};`,
      ),
    ).toBe("1")
    expect(
      runSql(
        `SELECT ended_at IS NOT NULL FROM instance_usage_log
           WHERE user_id = ${userId} AND tier = 'free'
           ORDER BY id DESC LIMIT 1;`,
      ),
    ).toBe("1")
  })

  test("LC-25 - Re-login during the grace period clears logged_out_at", async ({
    page,
  }) => {
    setPlan(1, "INTERVAL 1 MONTH")
    if (LOCAL) {
      seedFreeAssignment(true)
    } else {
      // A real login then logout leaves exactly the state under test, stamped
      // by the product rather than seeded, and with no backdating - so the
      // cleanup job's grace window still protects the account throughout.
      test.setTimeout(180_000)
      await login(page, USER.email, USER.password)
      await logout(page)
    }
    await login(page, USER.email, USER.password)

    // The auth path cleared the stamp, so the cleanup job's
    // "logged_out_at IS NOT NULL" filter no longer selects this user
    expect(
      runSql(
        `SELECT COUNT(*) FROM free_user_assignments
           WHERE user_id = ${userId} AND logged_out_at IS NOT NULL;`,
      ),
    ).toBe("0")
    // Cleared, not deleted: the assignment row itself survives
    expect(
      runSql(
        `SELECT COUNT(*) FROM free_user_assignments WHERE user_id = ${userId};`,
      ),
    ).toBe("1")
  })

  // The cleanup job's selection, asked of the real code against the real
  // database. Picking the wrong user here deletes a live user's data, so the
  // boundary is the safety-critical part; the deletion it then performs is
  // covered by test_cleanup_job.py and is deliberately not run against this
  // account's own files.
  // `userId` above is a SQL subquery, not a value, so the numeric id has to be
  // read out separately to compare against what the job returns.
  function numericUserId(): number {
    const id = runSql(
      `SELECT id FROM users WHERE email = '${sqlLiteral(USER.email)}';`,
    ).trim()
    expect(id, `no local row for ${USER.email}`).toMatch(/^\d+$/)
    return Number(id)
  }

  const CLEANUP_SELECT =
    "from studio.app.common.core.background.cleanup_job import DataCleanupJob\n" +
    "print([u[0] for u in DataCleanupJob._get_users_for_cleanup()])\n"

  // Deployed, INSTANCE_ID is pinned to the id the seed used, so the same
  // per-instance filter the real workers apply narrows the answer to this
  // account. Locally resolve_instance_id() returns "local" and the job skips
  // the filter, which is why the cap check below exists there.
  //
  // The seed is checked first, before the several-minute app boot below. The
  // synthetic instance id keeps the row away from the selection every real
  // worker runs, but NOT from the orphan sweep, which is not instance-scoped
  // and reads an id EC2 cannot resolve as a terminated instance - it deletes
  // the row outright. A swept seed would otherwise surface as "the job did not
  // select the user", which is this row's product-bug signal.
  function eligibleUserIds(): number[] {
    expect(
      runSql(
        `SELECT COUNT(*) FROM free_user_assignments
           WHERE user_id = ${userId} AND instance_id = '${E2E_INSTANCE}'`,
      ).trim(),
      "the seeded assignment is gone; the orphan sweep collected it mid-test " +
        "(it treats an unresolvable instance id as terminated), so this is " +
        "not a verdict on the cleanup selection",
    ).toBe("1")
    const out = LOCAL
      ? runInBackend("poetry run python -", CLEANUP_SELECT)
      : runInDeployedBackend(CLEANUP_SELECT, E2E_INSTANCE)
    const list = out.slice(out.lastIndexOf("["))
    return JSON.parse(list) as number[]
  }

  function stampLogout(minutesAgo: number, activeWorkflows = 0) {
    write(
      `UPDATE free_user_assignments
         SET logged_out_at = UTC_TIMESTAMP() - INTERVAL ${minutesAgo} MINUTE,
             active_workflow_count = ${activeWorkflows},
             last_workflow_start = UTC_TIMESTAMP() - INTERVAL 45 MINUTE
         WHERE user_id = ${userId}`,
    )
  }

  test("LC-26 - Cleanup selects a logged-out free user only past the grace period", async () => {
    // Each eligibleUserIds() boots the studio app in the container.
    test.setTimeout(240_000)
    setPlan(1, "INTERVAL 1 MONTH")
    if (!LOCAL) await ensureDeployedWorkspace()
    seedFreeAssignment(true)

    // The grace period is 60 minutes, so half an hour out is still protected.
    stampLogout(30)
    const inGrace = eligibleUserIds()
    // The job limits to MAX_USERS_PER_RUN with no ORDER BY, so at the cap an
    // absence could be truncation rather than the grace period.
    expect(inGrace.length, "eligible set is at the job's cap").toBeLessThan(50)
    expect(
      inGrace,
      "a user inside the grace period must not be selected",
    ).not.toContain(numericUserId())

    stampLogout(61)
    expect(
      eligibleUserIds(),
      "a user past the grace period must be selected",
    ).toContain(numericUserId())
  })

  test("LC-27 - A workflow still marked active blocks cleanup until it is recovered", async () => {
    // Each eligibleUserIds() boots the studio app in the container.
    test.setTimeout(240_000)
    setPlan(1, "INTERVAL 1 MONTH")
    if (!LOCAL) await ensureDeployedWorkspace()
    seedFreeAssignment(true)

    // Past the grace period, but the count says a workflow is running. Deleting
    // now would pull data out from under it, so the job must pass the user over
    // however long ago they logged out.
    stampLogout(61, 1)
    expect(
      eligibleUserIds(),
      "a user with an active workflow must never be selected",
    ).not.toContain(numericUserId())

    // The count is stale, not real: last_workflow_start is 45 minutes old and
    // the threshold is 30, which is what a crashed run leaves behind. Recovery
    // resets it and the user becomes collectable again.
    // Unlike the selection, the recovery sweep is NOT instance-scoped: it
    // resets every stale row it finds, in the premium table as well as the
    // free one. Assert no other row on either table qualifies before firing
    // it, so the blast radius is provably ours.
    const STALE_MINUTES = 30
    expect(
      runSql(
        `SELECT COUNT(*) FROM free_user_assignments
           WHERE active_workflow_count > 0
             AND last_workflow_start
                 < UTC_TIMESTAMP() - INTERVAL ${STALE_MINUTES} MINUTE
             AND user_id <> ${userId}`,
      ).trim(),
      "another account also has a stale free workflow count, and the " +
        "recovery sweep would reset theirs too",
    ).toBe("0")
    expect(
      runSql(
        `SELECT COUNT(*) FROM premium_user_assignments
           WHERE active_workflow_count > 0
             AND last_workflow_start
                 < UTC_TIMESTAMP() - INTERVAL ${STALE_MINUTES} MINUTE
             AND is_standby = 0`,
      ).trim(),
      "a premium account has a stale workflow count; the sweep resets the " +
        "premium table too, and zeroing a live count invites a delete under " +
        "a running workflow",
    ).toBe("0")
    const RECOVER =
      "from studio.app.common.core.workflow.workflow_count_recovery import " +
      "recover_stale_workflow_counts\n" +
      "print(recover_stale_workflow_counts()[0])\n"
    const recovered = LOCAL
      ? runInBackend("poetry run python -", RECOVER)
      : runInDeployedBackend(RECOVER)
    expect(
      Number(recovered.trim().split("\n").pop()),
      "users recovered",
    ).toBeGreaterThan(0)
    expect(
      runSql(
        `SELECT active_workflow_count FROM free_user_assignments
           WHERE user_id = ${userId};`,
      ),
    ).toBe("0")
    expect(
      eligibleUserIds(),
      "after recovery the user must be selectable again",
    ).toContain(numericUserId())
  })

  // Rows 427 / 433: the log panel opens, and what it shows is this user's own
  // trail - /logs filters by the caller's uid server-side, and the login that
  // just happened leaves calculate_limit_warning lines for exactly this user.
  test("LC-28 - The log panel opens and carries this user's own limit-check lines", async ({
    page,
  }) => {
    // calculate_limit_warning logs on entry for every tier, and the login path
    // calls it either way - so the premium plan buys this row nothing, while
    // costing a real instance (unmocked) or an assignment-success snackbar
    // that covers the floating log button (mocked).
    setPlan(LOCAL ? 2 : 1, "INTERVAL 1 MONTH")
    await loginKeepWarnings(page)

    await page.getByRole("button", { name: "show logs" }).click()
    await expect(page.getByText(/Service: /)).toBeVisible({ timeout: 15_000 })

    const res = await page.request.get(
      `${apiUrl()}/logs?search=calculate_limit_warning&offset=-1&limit=50`,
      { headers: await apiHeaders(page) },
    )
    expect(res.ok(), await res.text()).toBe(true)
    const { data } = (await res.json()) as { data: string[] }
    expect(
      data.some((line) => line.includes("calculate_limit_warning")),
      `no calculate_limit_warning line for this user in ${data.length} lines`,
    ).toBe(true)
  })

  // Row 6211's cross-tab half: a UI logout in one tab logs every tab out via
  // the localStorage broadcast, and only the tab that logged out fires the
  // release beacon. The AWS half - the soft release landing and the monitor
  // finalizing the row - is PREM-02 / PREM-04 against real infrastructure.
  test("LC-29 - Logout in one tab logs the others out, releasing exactly once", async ({
    page,
  }) => {
    setPlan(2, "INTERVAL 1 MONTH")
    setStorage(0, PREMIUM_QUOTA)

    let releases = 0
    const armTab = async (tab: Page) => {
      await mockPremiumAssignment(tab)
      await tab.route("**/users/me/premium/release-beacon", (route) => {
        releases += 1
        return route.fulfill({ json: { released: true } })
      })
    }
    await armTab(page)
    await loginKeepWarnings(page)

    const others: Page[] = []
    try {
      for (let i = 0; i < 2; i++) {
        const tab = await page.context().newPage()
        await armTab(tab)
        await tab.goto("/dashboard")
        await expect(tab).toHaveURL(/\/dashboard/, { timeout: 30_000 })
        others.push(tab)
      }

      await logout(page)
      await expect(page).toHaveURL(/\/login/, { timeout: 15_000 })

      // The other tabs observe the broadcast and log out on their own,
      // without being interacted with
      for (const tab of others) {
        await expect(tab).toHaveURL(/\/login/, { timeout: 10_000 })
      }
      // ...and the logging-out tab was the only one to release
      expect(releases, "release beacons fired across the three tabs").toBe(1)
    } finally {
      for (const tab of others) await tab.close().catch(() => {})
    }
  })

  // Row 6228's cross-tab half: Stay Active in one tab writes the shared
  // activity timestamp, and the other tab's warning clears off the broadcast
  // alone - it is never touched. The real heartbeat write behind Stay Active
  // is PREM-04 on real infrastructure.
  test("LC-30 - Stay Active in one tab clears the other tab's warning", async ({
    page,
  }) => {
    setStorage(0, PREMIUM_QUOTA)
    setPlan(2, "INTERVAL 1 MONTH")

    await mockPremiumAssignment(page)
    await loginWithArmedInactivityMonitor(page)

    const tabB = await page.context().newPage()
    await mockPremiumAssignment(tabB)
    await tabB.clock.install()
    const statusSeen = tabB.waitForResponse((r) =>
      r.url().includes("/users/me/premium/status"),
    )
    await tabB.goto("/dashboard")
    await statusSeen
    await tabB.waitForTimeout(1_000)

    try {
      // Playwright's clock is per BrowserContext, not per page: one
      // fast-forward moves both tabs. Advancing each would total 130 fake
      // minutes and trip the 2h auto-release instead of holding the warning.
      await page.clock.fastForward(65 * 60 * 1000)
      const warningA = page.locator("text=Premium Instance Inactivity Warning")
      const warningB = tabB.locator("text=Premium Instance Inactivity Warning")
      await expect(warningA).toBeVisible({ timeout: 15_000 })
      await expect(warningB).toBeVisible({ timeout: 15_000 })

      await page.getByRole("button", { name: "Stay Active" }).click()
      await expect(warningA).toBeHidden({ timeout: 15_000 })
      // Tab B is not interacted with; a beat of its own clock re-evaluates
      // the shared timestamp the broadcast delivered
      await tabB.clock.fastForward(60 * 1000)
      await expect(warningB).toBeHidden({ timeout: 15_000 })
    } finally {
      await tabB.close().catch(() => {})
    }
  })
})
