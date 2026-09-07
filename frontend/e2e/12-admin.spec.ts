import { test, expect, Locator, Page, request } from "@playwright/test"

import {
  ADMIN_STORAGE_STATE,
  activeUserRows,
  adminStorageState,
  apiUrl,
  confirmDialog,
  deleteFirebaseUser,
  dismissStorageWarning,
  ensureRegisteredUser,
  freeStorageState,
  FREE_USER,
  isLocalBaseUrl,
  localStackSkipReason,
  login,
  logout,
  gotoDashboard,
  runSql,
  saveStorageState,
  sqlLiteral,
  verifyEmail,
} from "./helpers"

// The admin Account Manager, from a real admin login.
//
// Local stack only, because there is no way to mint an admin through the API:
// `/register` is the only unauthenticated path that creates a user, and it
// forces the role to operator, so a client cannot self-elevate. The role is
// promoted with one UPDATE against the docker DB.
//
// Only the confirmed-deletion test destroys anything, and it destroys an account
// it registered for the purpose. Every other delete assertion stops at the
// confirmation dialog, so the shared accounts survive.

const ADMIN = {
  // A local default distinct from CI's `e2e_ci_admin@test.com`: one dev
  // Firebase project serves both, so sharing the address means a local run
  // re-creates the account CI's bootstrap just deleted, and CI's own
  // registration then answers EMAIL_EXISTS with no DB row to log in to.
  email: process.env.TEST_ADMIN_EMAIL || "e2e_local_admin@test.com",
  // Defaults to the free user's password so a local run needs no extra config
  password: process.env.TEST_ADMIN_PASSWORD || FREE_USER.password,
}

const ADMIN_ROLE_ID = 1

// For throwaway accounts created through forms: those enforce the frontend
// character-class rule, so the env-supplied password cannot be trusted there
const SCRATCH_PASSWORD = "e2ePass!1"

// Sign-off rows that this group alone covers, named in the skip reason so a run
// that does not execute it says which rows it left unverified
const UNCOVERED_ELSEWHERE = "ADMIN-01..22"

// LIMIT 1 because nothing in the schema stops two active rows sharing an
// address; as a scalar subquery a second one is error 1242, which takes the
// whole group down in beforeAll rather than failing the row it belongs to.
const userIdSql = (email: string) =>
  `(SELECT id FROM users WHERE email = '${sqlLiteral(email)}' AND active = 1
      ORDER BY id DESC LIMIT 1)`

async function ensureAdminUser() {
  await ensureRegisteredUser(ADMIN.email, ADMIN.password, "E2E Admin")
  runSql(
    `UPDATE user_roles SET role_id = ${ADMIN_ROLE_ID}
       WHERE user_id = ${userIdSql(ADMIN.email)};`,
  )
}

async function openLeftMenu(page: Page) {
  await page.locator('[aria-label="open drawer"]').click()
}

async function gotoAccountManager(page: Page, email?: string) {
  // The page mirrors its filter into the query string and reads it back on
  // load, so this is the page's own filter rather than a test-only entry point.
  const query = email
    ? `?email=${encodeURIComponent(email)}&limit=50&offset=0`
    : ""
  await page.goto(`/account-manager${query}`)
  await expect(
    page.getByRole("heading", { name: "Account Manager" }),
  ).toBeVisible({ timeout: 30_000 })
  await dismissStorageWarning(page)
}

const grid = (page: Page) => page.locator('[role="grid"]')

// Filter the list down to one address before locating its row: the grid
// virtualizes rows, so an account further down the list renders nowhere in the
// DOM and `has-text` would find nothing.
async function rowFor(page: Page, email: string) {
  await gotoAccountManager(page, email)
  const row = page.locator(`[role="row"]:has-text("${email}")`).first()
  await expect(row).toBeVisible({ timeout: 30_000 })
  return row
}

// MUI's Tooltip puts its string title on the child as aria-label, so these are
// the app's own names rather than @mui/icons-material's data-testids
const rowAction = (row: Locator, name: string) =>
  row.getByRole("button", { name })

// Cancel is "no write", and reading the database back cannot prove that: the
// modal closes without awaiting the dispatch, so a query issued straight after
// can beat the request it is meant to catch. Watch the requests instead.
function watchAdminRequests(page: Page) {
  const methods: string[] = []
  page.on("request", (req) => {
    if (/\/admin\/users/.test(req.url())) methods.push(req.method())
  })
  return methods
}

function expectNoAdminWrites(methods: string[]) {
  // The listing GET is the positive control: it proves the watcher was attached
  // and matching, so "no writes" is a real absence
  expect(methods).toContain("GET")
  expect(methods.filter((method) => method !== "GET")).toEqual([])
}

// The grid wrapper is 80% of the viewport and its columns total ~1190px, so at
// Playwright's 1280 default the action column falls outside the rendered window
// and MUI virtualizes it away.
test.use({
  viewport: { width: 1600, height: 900 },
  // Resolved per test rather than at module load: the admin account is created
  // by this spec's own beforeAll, so the file does not exist yet when the module
  // is imported. Falling back to undefined keeps the skip path from throwing.
  storageState: async ({}, use) => {
    await use(adminStorageState())
  },
})

test.describe.serial("Admin Account Manager", () => {
  let skipReason = ""
  let freeUserName = ""
  let freeUserId = ""
  // Set by ADMIN-08 once its throwaway is really gone; ADMIN-22 re-registers it
  let deletedEmail = ""

  // A local run that cannot execute this group is a broken environment, not a
  // configuration choice: skipping there hands a green summary to a sign-off
  // sheet whose rows nothing else covers. Elsewhere the group genuinely cannot
  // run, because promoting a role needs the docker DB.
  function unrunnable(reason: string) {
    if (isLocalBaseUrl()) {
      throw new Error(`${reason}; ${UNCOVERED_ELSEWHERE} cannot run`)
    }
    skipReason = `${reason}; leaves ${UNCOVERED_ELSEWHERE} unverified`
  }

  test.beforeAll(async () => {
    // The admin credentials default to the free user's, so this covers both
    if (!FREE_USER.email || !FREE_USER.password) {
      unrunnable("TEST_USER_EMAIL/TEST_USER_PASSWORD not set")
      return
    }
    const localStack = localStackSkipReason()
    if (localStack) {
      unrunnable(localStack)
      return
    }
    await ensureAdminUser()
    freeUserName = runSql(
      `SELECT name FROM users WHERE email = '${sqlLiteral(FREE_USER.email)}'
         AND active = 1 ORDER BY id DESC LIMIT 1;`,
    )
    if (!freeUserName) {
      throw new Error(
        `no active user row for ${FREE_USER.email}; ` +
          `${UNCOVERED_ELSEWHERE} cannot run`,
      )
    }
    freeUserId = runSql(
      `SELECT id FROM users WHERE email = '${sqlLiteral(FREE_USER.email)}'
         AND active = 1 ORDER BY id DESC LIMIT 1;`,
    )
    // One Firebase sign-in for the whole group. The project is rate limited and
    // this describe is `.serial`, so a single flake re-runs every test in it.
    await saveStorageState(ADMIN_STORAGE_STATE, ADMIN.email, ADMIN.password)
  })

  test.beforeEach(() => {
    test.skip(!!skipReason, skipReason)
  })

  test.afterAll(() => {
    if (skipReason) return
    // Leave the account as an operator so a run that never reaches the
    // bootstrap cannot inherit admin rights from a previous one
    runSql(
      `UPDATE user_roles SET role_id = 20
         WHERE user_id = ${userIdSql(ADMIN.email)};`,
    )
  })

  test("ADMIN-01 - Admin login reaches the Account Manager", async ({
    page,
  }) => {
    await gotoDashboard(page)
    await gotoAccountManager(page)
    // The list is populated, not just the heading rendered
    await expect(grid(page).locator('[role="row"]').nth(1)).toBeVisible({
      timeout: 30_000,
    })
    for (const column of [
      "ID",
      "Name",
      "Role",
      "Mail",
      "Data size",
      "Subscription Status",
      "Storage Usage",
      "Bucket name",
    ]) {
      await expect(
        page.getByRole("columnheader", { name: column, exact: true }),
      ).toBeVisible()
    }
  })

  test("ADMIN-02 - The Account Manager menu entry is visible to an admin", async ({
    page,
  }) => {
    await gotoDashboard(page)
    await openLeftMenu(page)
    const entry = page.getByRole("button", { name: "Account Manager" })
    await expect(entry).toBeVisible({ timeout: 15_000 })
    await entry.click()
    await expect(page).toHaveURL(/\/account-manager/, { timeout: 15_000 })
  })

  // Its own session, because the surrounding group arrives as the admin and this
  // row is about what an operator cannot see. global-setup already saved it.
  test.describe(() => {
    test.use({ storageState: freeStorageState() })

    test("ADMIN-03 - A non-admin has neither the menu entry nor the page", async ({
      page,
    }) => {
      await gotoDashboard(page)

      // Positive control first: the menu rendered, so the absence below is an
      // absence and not an unopened drawer
      await openLeftMenu(page)
      await expect(page.getByRole("button", { name: "Dashboard" })).toBeVisible(
        { timeout: 15_000 },
      )
      await expect(
        page.getByRole("button", { name: "Account Manager" }),
      ).toBeHidden()

      await page.goto("/account-manager")
      await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 })
      await expect(
        page.getByRole("heading", { name: "Account Manager" }),
      ).toBeHidden()
    })
  })

  test("ADMIN-04 - Edit Account opens on the row's values; Cancel saves nothing", async ({
    page,
  }) => {
    const requests = watchAdminRequests(page)
    const row = await rowFor(page, FREE_USER.email)
    await rowAction(row, "Edit Account").click()

    const modal = confirmDialog(page)
    await expect(modal.getByText("Edit Account")).toBeVisible({
      timeout: 15_000,
    })
    await expect(modal.locator('input[name="name"]')).toHaveValue(freeUserName)
    await expect(modal.locator('input[name="email"]')).toHaveValue(
      FREE_USER.email,
    )
    await expect(modal.getByText("OPERATOR")).toBeVisible()
    // An existing account is never asked for a password
    await expect(modal.locator('input[name="password"]')).toHaveCount(0)

    await modal.locator('input[name="name"]').fill("Abandoned Rename")
    await modal.getByRole("button", { name: "Cancel" }).click()

    await expect(modal).toBeHidden({ timeout: 15_000 })
    expectNoAdminWrites(requests)
    expect(
      runSql(
        `SELECT name FROM users WHERE email = '${sqlLiteral(FREE_USER.email)}'
           AND active = 1 ORDER BY id DESC LIMIT 1;`,
      ),
    ).toBe(freeUserName)
  })

  test("ADMIN-05 - Cancelling Add creates no account", async ({ page }) => {
    const requests = watchAdminRequests(page)
    await gotoAccountManager(page)

    const email = `e2e_admin_cancelled_${Date.now()}@test.com`
    await page.getByRole("button", { name: "Add" }).click()

    const modal = confirmDialog(page)
    await expect(modal.getByText("Add Account")).toBeVisible({
      timeout: 15_000,
    })
    await modal.locator('input[name="name"]').fill("Never Created")
    await modal.locator('input[name="email"]').fill(email)
    await modal.locator('input[name="password"]').fill("e2ePass!1")
    await modal.locator('input[name="confirmPassword"]').fill("e2ePass!1")
    await modal.getByRole("button", { name: "Cancel" }).click()

    await expect(modal).toBeHidden({ timeout: 15_000 })
    expectNoAdminWrites(requests)
    expect(
      runSql(
        `SELECT COUNT(*) FROM users WHERE email = '${sqlLiteral(email)}';`,
      ),
    ).toBe("0")
  })

  test("ADMIN-06 - Delete asks for confirmation, and Cancel aborts it", async ({
    page,
  }) => {
    // Three form logins against Firebase, plus a logout
    test.setTimeout(120_000)
    const requests = watchAdminRequests(page)
    const row = await rowFor(page, FREE_USER.email)
    const deleteButton = rowAction(row, "Delete Account")
    await expect(deleteButton).toBeVisible()
    await deleteButton.click()

    const modal = confirmDialog(page)
    // Names the account being destroyed, so a mis-click is recoverable. Asserted
    // with the id as well, or the dialog could be describing any row.
    await expect(
      modal.getByText(`Name:${freeUserName}`, { exact: false }),
    ).toBeVisible({ timeout: 15_000 })
    await expect(modal.getByText(`ID:${freeUserId}`)).toBeVisible()
    // Confirming takes more than a click: the button stays disabled until the
    // word is typed exactly, which is the whole guard against a mis-click
    const confirm = modal.getByRole("button", { name: "Delete Account" })
    await expect(confirm).toBeDisabled()
    await modal.locator('input[placeholder="DELETE"]').fill("delete")
    await expect(confirm).toBeDisabled()
    await modal.locator('input[placeholder="DELETE"]').fill("DELETE")
    await expect(confirm).toBeEnabled()

    await modal.getByRole("button", { name: "Cancel" }).click()

    await expect(modal).toBeHidden({ timeout: 15_000 })
    expectNoAdminWrites(requests)
    expect(
      runSql(
        `SELECT active FROM users
           WHERE email = '${sqlLiteral(FREE_USER.email)}' ORDER BY id DESC
           LIMIT 1;`,
      ),
    ).toBe("1")
    // Still logs in, which is what "not deleted" means to the user
    await logout(page)
    await login(page, FREE_USER.email, FREE_USER.password)
  })

  test("ADMIN-07 - An admin's own row offers no Delete", async ({ page }) => {
    // The UI half of the self-delete guard; the server-side 403 is covered by
    // the router tests.
    const ownRow = await rowFor(page, ADMIN.email)
    // Positive control: the row does render the actions that are not suppressed
    await expect(rowAction(ownRow, "Edit Account")).toBeVisible()
    await expect(rowAction(ownRow, "Edit Subscription")).toBeVisible()
    await expect(rowAction(ownRow, "Delete Account")).toHaveCount(0)
    // Proxy SignIn is suppressed on the same row, by the same check
    await expect(rowAction(ownRow, "Proxy SignIn")).toHaveCount(0)

    // And another user's row does offer both, so the absence is about the row
    const otherRow = await rowFor(page, FREE_USER.email)
    await expect(rowAction(otherRow, "Delete Account")).toBeVisible()
    await expect(rowAction(otherRow, "Proxy SignIn")).toBeVisible()
  })

  // The one destructive test in this spec, on an account created for it. Safe to
  // run repeatedly and safe in CI: `crud_users.delete_user` deletes the Firebase
  // account as its first step, so the throwaway cleans up the half that would
  // otherwise accumulate. The `users` row survives, inactive, by design.
  test("ADMIN-08 - Confirming the deletion deactivates the account", async ({
    page,
  }) => {
    // The deletion pipeline poll alone is budgeted 60s, which is the whole
    // default test timeout
    test.setTimeout(300_000)
    const email = `e2e_admin_deleted_${Date.now()}@test.com`
    // Owning something is the point: deleting an account takes its data with
    // it, and an account that owns nothing cannot show that.
    const wsName = `e2e-admin-deleted-${Date.now()}`
    const api = await request.newContext({ baseURL: apiUrl() })
    try {
      const registered = await api.post("/api/register", {
        data: {
          name: "E2E Delete Me",
          role_id: 20,
          email,
          password: ADMIN.password,
        },
      })
      expect(
        registered.ok(),
        `register ${registered.status()}: ${await registered.text()}`,
      ).toBeTruthy()
      verifyEmail(email)

      const loggedIn = await api.post("/auth/login", {
        data: { email, password: ADMIN.password },
      })
      expect(
        loggedIn.ok(),
        `login ${loggedIn.status()}: ${await loggedIn.text()}`,
      ).toBeTruthy()
      const { access_token } = await loggedIn.json()
      const created = await api.post("/workspace", {
        data: { name: wsName },
        headers: { Authorization: `Bearer ${access_token}` },
      })
      expect(
        created.ok(),
        `create workspace ${created.status()}: ${await created.text()}`,
      ).toBeTruthy()
    } finally {
      await api.dispose()
    }
    const userId = runSql(
      `SELECT id FROM users WHERE email = '${sqlLiteral(email)}';`,
    )
    expect(userId).not.toBe("")
    expect(
      runSql(
        `SELECT deleted FROM workspaces WHERE user_id = ${userId}
           AND name = '${sqlLiteral(wsName)}';`,
      ),
    ).toBe("0")

    const row = await rowFor(page, email)
    await rowAction(row, "Delete Account").click()

    const modal = confirmDialog(page)
    await expect(modal.locator('input[placeholder="DELETE"]')).toBeVisible({
      timeout: 15_000,
    })
    await modal.locator('input[placeholder="DELETE"]').fill("DELETE")
    await modal.getByRole("button", { name: "Delete Account" }).click()

    await expect(page.getByText("Account deleted successfully!")).toBeVisible({
      timeout: 30_000,
    })

    // Deactivated rather than removed: the row survives so the address can be
    // registered again as a new row
    await expect
      .poll(() => runSql(`SELECT active FROM users WHERE id = ${userId};`), {
        timeout: 30_000,
      })
      .toBe("0")
    // The deletion pipeline ran to the end rather than stopping at "in progress"
    await expect
      .poll(
        () =>
          runSql(
            `SELECT step, status FROM user_deletion_records
               WHERE user_id = ${userId} ORDER BY id DESC LIMIT 1;`,
          ),
        { timeout: 60_000 },
      )
      .toMatch(/completed\s+completed/)

    // The owned workspace is soft-deleted (row kept, flag
    // flipped), its experiments go with it, preferences are removed and the
    // role link deliberately survives. Polled because the workspace hand-off
    // runs after the deletion record reaches `completed`.
    await expect
      .poll(
        () =>
          runSql(`SELECT deleted FROM workspaces WHERE user_id = ${userId};`),
        { timeout: 30_000 },
      )
      .toBe("1")
    expect(
      runSql(
        `SELECT COUNT(*) FROM experiment_records er
           JOIN workspaces w ON w.id = er.workspace_id
          WHERE w.user_id = ${userId};`,
      ),
    ).toBe("0")
    expect(
      runSql(
        `SELECT COUNT(*) FROM user_preferences WHERE user_id = ${userId};`,
      ),
    ).toBe("0")
    expect(
      runSql(`SELECT COUNT(*) FROM user_roles WHERE user_id = ${userId};`),
    ).toBe("1")

    // And it leaves the admin's list, which is what the admin sees. The empty
    // overlay is the positive control: without waiting for it, the count is zero
    // simply because the grid has not finished fetching.
    const listed = page.waitForResponse(
      (response) => /\/admin\/users\?/.test(response.url()) && response.ok(),
    )
    await gotoAccountManager(page, email)
    await listed
    await expect(grid(page).getByText("No rows")).toBeVisible({
      timeout: 30_000,
    })
    await expect(page.locator(`[role="row"]:has-text("${email}")`)).toHaveCount(
      0,
    )
    deletedEmail = email
  })

  test("ADMIN-09 - Proxy SignIn asks before switching session, and Cancel keeps it", async ({
    page,
  }) => {
    // The highest-privilege action on the page: it swaps the admin's own session
    // for another user's. Only the confirmation is exercised - actually switching
    // would leave the worker signed in as somebody else for the serial tests
    // that follow.
    const requests = watchAdminRequests(page)
    const row = await rowFor(page, FREE_USER.email)
    await rowAction(row, "Proxy SignIn").click()

    const modal = confirmDialog(page)
    // Names who the admin is about to become, and with which id
    await expect(modal.getByText("Proxy SignIn")).toBeVisible({
      timeout: 15_000,
    })
    await expect(modal.getByText(freeUserName)).toBeVisible()
    await expect(modal.getByText(`ID.${freeUserId}`)).toBeVisible()

    await modal.getByRole("button", { name: "Cancel" }).click()

    await expect(modal).toBeHidden({ timeout: 15_000 })
    expectNoAdminWrites(requests)
    // Still the admin, which is what "not switched" means
    await gotoAccountManager(page)
    await expect(
      page.getByRole("heading", { name: "Account Manager" }),
    ).toBeVisible()
  })

  test("ADMIN-10 - Edit Subscription requires a reason, and Cancel writes nothing", async ({
    page,
  }) => {
    const requests = watchAdminRequests(page)
    const row = await rowFor(page, FREE_USER.email)
    await rowAction(row, "Edit Subscription").click()

    const modal = confirmDialog(page)
    await expect(modal.getByText("Edit Subscription")).toBeVisible({
      timeout: 15_000,
    })

    // Every change to a paid plan is audited, so Save is gated on a reason
    const save = modal.getByRole("button", { name: "Save" })
    await expect(save).toBeDisabled()
    // Whitespace is not a reason
    await modal.getByLabel("Reason for manual edit").fill("   ")
    await expect(save).toBeDisabled()
    await modal.getByLabel("Reason for manual edit").fill("e2e check")
    await expect(save).toBeEnabled()

    // The quota is clamped rather than accepted as typed
    const quota = modal.getByLabel("Storage Quota (GB)")
    await quota.fill("99999")
    await expect(quota).toHaveValue("9999")
    await quota.fill("0")
    await expect(quota).toHaveValue("1")

    await modal.getByRole("button", { name: "Cancel" }).click()

    await expect(modal).toBeHidden({ timeout: 15_000 })
    expectNoAdminWrites(requests)
  })

  test("ADMIN-11 - The dashboard tile to the Account Manager is admin-only", async ({
    page,
  }) => {
    // A second, independent gate on the same page: the drawer entry and this tile
    // are separate conditions, so one can be removed without the other noticing.
    test.setTimeout(120_000)
    await gotoDashboard(page)

    const tile = page.getByRole("link", { name: "Account Manager" })
    await expect(tile).toBeVisible({ timeout: 15_000 })
    await tile.click()
    await expect(page).toHaveURL(/\/account-manager/, { timeout: 15_000 })

    await logout(page)
    await login(page, FREE_USER.email, FREE_USER.password)

    // Positive control: the tiles rendered, so the absence is an absence
    await expect(page.getByRole("link", { name: "Workspaces" })).toBeVisible({
      timeout: 15_000,
    })
    await expect(
      page.getByRole("link", { name: "Account Manager" }),
    ).toHaveCount(0)
  })

  test("ADMIN-12 - The list's own sort and rows-per-page controls", async ({
    page,
  }) => {
    // The other tests reach the list through the query string, which exercises
    // only the read-it-back-on-load half. These are the controls themselves.
    await gotoAccountManager(page)
    await expect(grid(page).locator('[role="row"]').nth(1)).toBeVisible({
      timeout: 30_000,
    })

    // Role lives on a joined table, so the route has to map the column name
    // before the query will run at all - unmapped, it answers 400
    const sorted = page.waitForResponse(
      (response) =>
        /\/admin\/users\?/.test(response.url()) &&
        /sort=role/.test(response.url()),
    )
    await grid(page).getByRole("columnheader", { name: "Role" }).click()
    expect((await sorted).status()).toBe(200)

    // Rows per page is the page's own control rather than the query string
    const relimited = page.waitForResponse(
      (response) =>
        /\/admin\/users\?/.test(response.url()) &&
        /limit=10\b/.test(response.url()),
    )
    await page.locator('select[name="limit"]').selectOption("10")
    expect((await relimited).status()).toBe(200)
    await expect(page.getByText(/1 - \d+ of \d+/)).toBeVisible({
      timeout: 15_000,
    })
  })

  // The happy paths the Cancel tests above deliberately stop short of. All
  // mutations land on disposable accounts, never on the shared ones, because a
  // changed password, email or role would invalidate the saved storage state
  // for every spec after this one.
  test.describe("Mutating paths (ADMIN-13..21)", () => {
    // Tests that rename this account update `mutable`, so later tests and the
    // cleanup below always address its current identity
    const mutable = { email: "", name: "", id: "" }
    const scratch: { email: string; id: string }[] = [mutable]

    test.beforeAll(async () => {
      if (skipReason) return
      mutable.email = `e2e_admin_mutable_${Date.now()}@test.com`
      mutable.name = "E2E Mutable"
      await ensureRegisteredUser(mutable.email, ADMIN.password, mutable.name)
      mutable.id = runSql(
        `SELECT id FROM users WHERE email = '${sqlLiteral(mutable.email)}'
           AND active = 1 ORDER BY id DESC LIMIT 1;`,
      )
      expect(mutable.id).not.toBe("")
    })

    // Retired the light way: these accounts own nothing, so the full deletion
    // pipeline (and its extra Firebase sign-in) buys nothing here
    test.afterAll(() => {
      if (skipReason) return
      for (const account of scratch) {
        if (!account.id) continue
        deleteFirebaseUser(account.email)
        runSql(`UPDATE users SET active = 0 WHERE id = ${account.id};`)
      }
    })

    async function openAddModal(page: Page) {
      await gotoAccountManager(page)
      await page.getByRole("button", { name: "Add" }).click()
      const modal = confirmDialog(page)
      await expect(modal.getByText("Add Account")).toBeVisible({
        timeout: 15_000,
      })
      return modal
    }

    // The role Select opens into a portal, so the option lives on the page
    // rather than in the dialog
    async function pickRole(page: Page, modal: Locator, role: string) {
      await modal.locator(".MuiSelect-select").click()
      await page.getByRole("option", { name: role }).click()
    }

    // One contract for both roles: the account exists and is usable, the role
    // stuck, the free subscription row exists, and nothing touched Stripe
    async function createAndExpect(
      page: Page,
      role: "ADMIN" | "OPERATOR",
      roleCell: string,
      roleId: number,
    ) {
      const email = `e2e_admin_created_${role.toLowerCase()}_${Date.now()}@test.com`
      const name = `E2E Created ${role}`
      const modal = await openAddModal(page)
      await modal.locator('input[name="name"]').fill(name)
      await pickRole(page, modal, role)
      await modal.locator('input[name="email"]').fill(email)
      await modal.locator('input[name="password"]').fill(SCRATCH_PASSWORD)
      await modal
        .locator('input[name="confirmPassword"]')
        .fill(SCRATCH_PASSWORD)
      await modal.getByRole("button", { name: "Ok" }).click()

      await expect(
        page.getByText("Your account has been created successfully!"),
      ).toBeVisible({ timeout: 30_000 })
      const row = await rowFor(page, email)
      await expect(row.getByText(name)).toBeVisible()
      await expect(row.getByText(roleCell, { exact: true })).toBeVisible()

      const userId = runSql(
        `SELECT id FROM users WHERE email = '${sqlLiteral(email)}'
           AND active = 1 ORDER BY id DESC LIMIT 1;`,
      )
      expect(userId).not.toBe("")
      scratch.push({ email, id: userId })
      expect(
        runSql(`SELECT role_id FROM user_roles WHERE user_id = ${userId};`),
      ).toBe(String(roleId))
      expect(
        runSql(
          `SELECT COUNT(*), MIN(plan_id) FROM subscription_users
             WHERE user_id = ${userId};`,
        ),
      ).toMatch(/^1\s+1$/)
      expect(
        runSql(
          `SELECT COUNT(*) FROM subscription_user_accounts
             WHERE user_id = ${userId};`,
        ),
      ).toBe("0")
      expect(
        runSql(
          `SELECT COUNT(*) FROM subscription_user_purchases
             WHERE user_id = ${userId};`,
        ),
      ).toBe("0")

      // Admin-created accounts arrive verified, so the login must work at once
      const api = await request.newContext({ baseURL: apiUrl() })
      try {
        const loggedIn = await api.post("/auth/login", {
          data: { email, password: SCRATCH_PASSWORD },
        })
        expect(
          loggedIn.ok(),
          `created account login ${loggedIn.status()}: ${await loggedIn.text()}`,
        ).toBeTruthy()
      } finally {
        await api.dispose()
      }
    }

    test("ADMIN-13 - Add creates an admin account", async ({ page }) => {
      test.setTimeout(120_000)
      await createAndExpect(page, "ADMIN", "Admin", ADMIN_ROLE_ID)
    })

    test("ADMIN-14 - Add creates an operator account", async ({ page }) => {
      test.setTimeout(120_000)
      await createAndExpect(page, "OPERATOR", "OPERATOR", 20)
    })

    test("ADMIN-15 - Add refuses empty fields, a bad email and a weak password", async ({
      page,
    }) => {
      const requests = watchAdminRequests(page)
      const modal = await openAddModal(page)

      await modal.getByRole("button", { name: "Ok" }).click()
      // All five fields report at once, and the refused submit keeps the modal up
      await expect(modal.getByText("This field is required")).toHaveCount(5)
      await expect(modal.getByText("Add Account")).toBeVisible()

      await modal.locator('input[name="email"]').fill("not-an-email")
      await expect(modal.getByText("Invalid email format")).toBeVisible()

      await modal.locator('input[name="password"]').fill("abc")
      await expect(modal.getByText(/at least 6 characters/)).toBeVisible()
      // Only the frontend rule forbids characters outside the allowed set; the
      // backend regex would accept this one
      await modal.locator('input[name="password"]').fill("Passw0rd!?")
      await expect(
        modal.getByText("Allowed special characters (!#$%&()*+,-./@_|)"),
      ).toBeVisible()

      await modal.getByRole("button", { name: "Cancel" }).click()
      await expect(modal).toBeHidden({ timeout: 15_000 })
      expectNoAdminWrites(requests)
    })

    test("ADMIN-16 - A duplicate email is reported and writes nothing", async ({
      page,
    }) => {
      const rowsBefore = runSql(
        `SELECT COUNT(*) FROM users
           WHERE email = '${sqlLiteral(FREE_USER.email)}';`,
      )
      const modal = await openAddModal(page)
      await modal.locator('input[name="name"]').fill("Duplicate Address")
      await pickRole(page, modal, "OPERATOR")
      await modal.locator('input[name="email"]').fill(FREE_USER.email)
      await modal.locator('input[name="password"]').fill(SCRATCH_PASSWORD)
      await modal
        .locator('input[name="confirmPassword"]')
        .fill(SCRATCH_PASSWORD)

      const created = page.waitForResponse(
        (response) =>
          response.request().method() === "POST" &&
          /\/admin\/users/.test(response.url()),
      )
      await modal.getByRole("button", { name: "Ok" }).click()
      // The server refused it, not just the UI: any snackbar-only assertion
      // would also pass on a network error
      expect((await created).status()).toBe(400)
      await expect(page.getByText("This email already exists!")).toBeVisible({
        timeout: 15_000,
      })
      expect(activeUserRows(FREE_USER.email)).toBe(1)
      expect(
        runSql(
          `SELECT COUNT(*) FROM users
             WHERE email = '${sqlLiteral(FREE_USER.email)}';`,
        ),
      ).toBe(rowsBefore)
    })

    test("ADMIN-17 - Editing name and email saves to the DB and the list", async ({
      page,
    }) => {
      const renamed = "E2E Mutable Renamed"
      const newEmail = `e2e_admin_mutable_renamed_${Date.now()}@test.com`
      const row = await rowFor(page, mutable.email)
      await rowAction(row, "Edit Account").click()

      const modal = confirmDialog(page)
      await expect(modal.getByText("Edit Account")).toBeVisible({
        timeout: 15_000,
      })
      await modal.locator('input[name="name"]').fill(renamed)
      await modal.locator('input[name="email"]').fill(newEmail)
      await modal.getByRole("button", { name: "Ok" }).click()

      await expect(
        page.getByText("Your account has been edited successfully!"),
      ).toBeVisible({ timeout: 30_000 })
      // Track the new identity as soon as the save is announced, or a failed
      // assertion below would leave the cleanup pointed at the old address
      mutable.name = renamed
      mutable.email = newEmail

      const renamedRow = await rowFor(page, newEmail)
      await expect(renamedRow.getByText(renamed)).toBeVisible()
      expect(
        runSql(`SELECT name, email FROM users WHERE id = ${mutable.id};`),
      ).toBe(`${renamed}\t${newEmail}`)
    })

    test("ADMIN-18 - Edit refuses an empty name and a bad email, saving nothing", async ({
      page,
    }) => {
      const requests = watchAdminRequests(page)
      const row = await rowFor(page, mutable.email)
      await rowAction(row, "Edit Account").click()

      const modal = confirmDialog(page)
      await expect(modal.getByText("Edit Account")).toBeVisible({
        timeout: 15_000,
      })
      await modal.locator('input[name="name"]').fill("")
      await modal.getByRole("button", { name: "Ok" }).click()
      await expect(modal.getByText("This field is required")).toBeVisible()
      await expect(modal.getByText("Edit Account")).toBeVisible()

      await modal.locator('input[name="name"]').fill("Never Saved")
      await modal.locator('input[name="email"]').fill("not-an-email")
      await modal.getByRole("button", { name: "Ok" }).click()
      await expect(modal.getByText("Invalid email format")).toBeVisible()
      await expect(modal.getByText("Edit Account")).toBeVisible()

      await modal.getByRole("button", { name: "Cancel" }).click()
      await expect(modal).toBeHidden({ timeout: 15_000 })
      expectNoAdminWrites(requests)
      expect(runSql(`SELECT name FROM users WHERE id = ${mutable.id};`)).toBe(
        mutable.name,
      )
    })

    test("ADMIN-19 - A role change saves, and a demoted admin loses access", async ({
      page,
      browser,
    }) => {
      // Two modal round-trips plus a real login as the demoted account
      test.setTimeout(180_000)
      const saved = page.getByText("Your account has been edited successfully!")

      let row = await rowFor(page, mutable.email)
      await rowAction(row, "Edit Account").click()
      let modal = confirmDialog(page)
      await expect(modal.getByText("Edit Account")).toBeVisible({
        timeout: 15_000,
      })
      await pickRole(page, modal, "ADMIN")
      await modal.getByRole("button", { name: "Ok" }).click()
      await expect(saved).toBeVisible({ timeout: 30_000 })
      expect(
        runSql(`SELECT role_id FROM user_roles WHERE user_id = ${mutable.id};`),
      ).toBe("1")

      // Let the first snackbar clear so the second assertion cannot match it
      await expect(saved).toBeHidden({ timeout: 15_000 })

      row = await rowFor(page, mutable.email)
      await rowAction(row, "Edit Account").click()
      modal = confirmDialog(page)
      await expect(modal.getByText("Edit Account")).toBeVisible({
        timeout: 15_000,
      })
      await pickRole(page, modal, "OPERATOR")
      await modal.getByRole("button", { name: "Ok" }).click()
      await expect(saved).toBeVisible({ timeout: 30_000 })
      expect(
        runSql(
          `SELECT COUNT(*), MIN(role_id) FROM user_roles
             WHERE user_id = ${mutable.id};`,
        ),
      ).toMatch(/^1\s+20$/)

      // The demotion holds where it matters: the demoted account's own fresh
      // session. The empty storageState is load-bearing: a manual newContext
      // inherits the file-level admin state, and an omitted (or undefined)
      // value hands this "fresh" session the admin's login.
      const context = await browser.newContext({
        baseURL: process.env.BASE_URL || "http://localhost:3000",
        storageState: { cookies: [], origins: [] },
      })
      try {
        const demoted = await context.newPage()
        const demotedMe = demoted.waitForResponse(
          (response) =>
            response.ok() &&
            response.request().method() === "GET" &&
            /\/users\/me$/.test(new URL(response.url()).pathname),
          { timeout: 60_000 },
        )
        await demoted.goto("/login")
        await demoted.locator('[data-testid="email"]').fill(mutable.email)
        await demoted.locator('[data-testid="password"]').fill(ADMIN.password)
        await demoted.locator('[data-testid="button-submit"]').click()
        await expect(demoted).toHaveURL(/\/dashboard/, { timeout: 60_000 })
        await dismissStorageWarning(demoted)
        // The session's own view of the role, not just the admin's
        expect((await (await demotedMe).json()).role_id).toBe(20)

        // Positive control first: the tiles rendered, so the absence is real
        await expect(
          demoted.getByRole("link", { name: "Workspaces" }),
        ).toBeVisible({ timeout: 15_000 })
        await expect(
          demoted.getByRole("link", { name: "Account Manager" }),
        ).toHaveCount(0)

        await demoted.goto("/account-manager")
        await expect(demoted).toHaveURL(/\/dashboard/, { timeout: 15_000 })
        await expect(
          demoted.getByRole("heading", { name: "Account Manager" }),
        ).toBeHidden()
      } finally {
        await context.close()
      }
    })

    test("ADMIN-20 - The Subscription Status column tracks the plan in the DB", async ({
      page,
    }) => {
      // The sheet's own mapping: plan_id 1 = Free, 2 = Premium
      expect(
        runSql(
          `SELECT plan_id FROM subscription_users
             WHERE user_id = ${mutable.id};`,
        ),
      ).toBe("1")
      let row = await rowFor(page, mutable.email)
      await expect(row.getByText("Free", { exact: true })).toBeVisible()

      // Days remaining rounds up, so 30 needs an expiry inside (29d, 30d]:
      // the hour comes off the 30 days, not onto them
      runSql(
        `UPDATE subscription_users
            SET plan_id = 2,
                expiration = DATE_SUB(DATE_ADD(UTC_TIMESTAMP(), INTERVAL 30 DAY),
                                      INTERVAL 1 HOUR)
          WHERE user_id = ${mutable.id};`,
      )
      row = await rowFor(page, mutable.email)
      await expect(row.getByText("Premium (30 days left)")).toBeVisible()
    })

    test("ADMIN-21 - The Storage Usage column mirrors the DB after Reload", async ({
      page,
    }) => {
      const row = await rowFor(page, mutable.email)
      // The account's real state first, so the change below is provably the
      // Reload's doing and not a leftover render
      await expect(row.getByText("0 Bytes / 5 GB")).toBeVisible({
        timeout: 15_000,
      })
      expect(
        runSql(
          `SELECT COUNT(*) FROM user_storage_usage
             WHERE user_id = ${mutable.id};`,
        ),
      ).toBe("1")
      // 1 GB of a non-default 8 GB quota: a hardcoded default cannot pass, and
      // 12.5% is exact under both the backend's rounding and the cell's
      runSql(
        `UPDATE user_storage_usage
            SET storage_usage_bytes = 1073741824,
                storage_quota_bytes = 8589934592
          WHERE user_id = ${mutable.id};`,
      )

      const relisted = page.waitForResponse(
        (response) => /\/admin\/users\?/.test(response.url()) && response.ok(),
      )
      await page.getByRole("button", { name: "Reload" }).click()
      await relisted
      await expect(row.getByText("1 GB / 8 GB")).toBeVisible({
        timeout: 15_000,
      })
      await expect(row.getByText("12.5% used")).toBeVisible()
    })
  })

  // The address ADMIN-08 deleted registers again as a brand-new account. A
  // clean session, because registration is for the signed-out.
  test.describe(() => {
    test.use({ storageState: { cookies: [], origins: [] } })

    test("ADMIN-22 - A deleted address can register again as a new account", async ({
      page,
    }) => {
      test.setTimeout(120_000)
      expect(
        deletedEmail,
        "ADMIN-08 must have deleted its account first",
      ).not.toBe("")
      expect(
        runSql(
          `SELECT COUNT(*) FROM users
             WHERE email = '${sqlLiteral(deletedEmail)}';`,
        ),
      ).toBe("1")

      await page.goto("/register")
      await expect(page.locator('button:has-text("Sign Up")')).toBeVisible({
        timeout: 30_000,
      })
      await page.locator('input[name="name"]').fill("E2E Deleted Reborn")
      await page.locator('input[name="email"]').fill(deletedEmail)
      await page.locator('input[name="password"]').fill(SCRATCH_PASSWORD)
      await page.locator('input[name="confirmPassword"]').fill(SCRATCH_PASSWORD)
      const agree = page.locator("#agree-to-terms")
      if (await agree.count()) await agree.check()
      await page.locator('button:has-text("Sign Up")').click()
      await expect(
        page.locator("text=Registration Almost Complete!"),
      ).toBeVisible({ timeout: 30_000 })

      // Two rows now share the address: the deleted one stays inactive with
      // its original uid, the new one is active under a fresh uid
      expect(
        runSql(
          `SELECT COUNT(*) FROM users
             WHERE email = '${sqlLiteral(deletedEmail)}';`,
        ),
      ).toBe("2")
      expect(
        runSql(
          `SELECT active FROM users
             WHERE email = '${sqlLiteral(deletedEmail)}'
             ORDER BY id ASC LIMIT 1;`,
        ),
      ).toBe("0")
      expect(
        runSql(
          `SELECT active FROM users
             WHERE email = '${sqlLiteral(deletedEmail)}'
             ORDER BY id DESC LIMIT 1;`,
        ),
      ).toBe("1")
      expect(
        runSql(
          `SELECT COUNT(DISTINCT uid) FROM users
             WHERE email = '${sqlLiteral(deletedEmail)}';`,
        ),
      ).toBe("2")

      deleteFirebaseUser(deletedEmail)
      runSql(
        `UPDATE users SET active = 0
           WHERE email = '${sqlLiteral(deletedEmail)}' AND active = 1;`,
      )
    })
  })
})
