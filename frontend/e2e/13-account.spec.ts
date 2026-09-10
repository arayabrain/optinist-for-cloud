import * as fs from "fs"
import * as path from "path"

import { test, expect, Page, request } from "@playwright/test"

import {
  apiUrl,
  confirmDialog,
  deleteFirebaseUser,
  dismissStorageWarning,
  ensureRegisteredUser,
  isLocalBaseUrl,
  localStackSkipReason,
  runSql,
  saveStorageState,
  sqlLiteral,
} from "./helpers"

// The Account Profile page's self-service mutations: password change and the
// inline name edit. Everything runs as a disposable per-run account, because a
// password change invalidates the shared account's saved storage state. Local
// stack only: registering and verifying the account needs the docker backend.

const PASSWORD = "Test@123"
const NEW_PASSWORD = "Test@456"

// Sign-off rows that this group alone covers, named in the skip reason so a run
// that does not execute it says which rows it left unverified
const UNCOVERED_ELSEWHERE = "ACC-01..06"

const ACCOUNT_STORAGE_STATE = path.join(__dirname, ".auth", "account.json")

test.use({
  // Resolved per test: the account (and its storage state) is created by this
  // spec's own beforeAll, so the file does not exist at module load
  storageState: async ({}, use) => {
    await use(
      fs.existsSync(ACCOUNT_STORAGE_STATE) ? ACCOUNT_STORAGE_STATE : undefined,
    )
  },
})

async function gotoAccountProfile(page: Page) {
  await page.goto("/account")
  await expect(
    page.getByRole("heading", { name: "Account Profile" }),
  ).toBeVisible({ timeout: 30_000 })
  await dismissStorageWarning(page)
}

async function openChangePassword(page: Page) {
  await page.getByRole("button", { name: "Change Password" }).click()
  const modal = confirmDialog(page)
  await expect(modal.getByText("Change Password")).toBeVisible({
    timeout: 15_000,
  })
  return modal
}

// "No write happened" cannot be proven by reading state back (the modal closes
// without awaiting the dispatch); watch the requests instead. The page-load
// GET /users/me is the positive control that the watcher matches.
function watchMeRequests(page: Page) {
  const calls: string[] = []
  page.on("request", (req) => {
    const { pathname } = new URL(req.url())
    if (/\/users\/me/.test(pathname)) calls.push(`${req.method()} ${pathname}`)
  })
  return calls
}

function expectNoMeWrites(calls: string[]) {
  expect(calls.join("\n")).toContain("GET /users/me")
  expect(calls.filter((call) => /PUT|POST|DELETE/.test(call))).toEqual([])
}

test.describe.serial("Account Profile self-service", () => {
  let skipReason = ""
  const account = { email: "", name: "", id: "" }

  // A local run that cannot execute this group is a broken environment, and
  // skipping there would hand a green summary to rows nothing else covers
  function unrunnable(reason: string) {
    if (isLocalBaseUrl()) {
      throw new Error(`${reason}; ${UNCOVERED_ELSEWHERE} cannot run`)
    }
    skipReason = `${reason}; leaves ${UNCOVERED_ELSEWHERE} unverified`
  }

  test.beforeAll(async () => {
    const localStack = localStackSkipReason()
    if (localStack) {
      unrunnable(localStack)
      return
    }
    account.email = `e2e_account_${Date.now()}@test.com`
    account.name = "E2E Account"
    await ensureRegisteredUser(account.email, PASSWORD, account.name)
    account.id = runSql(
      `SELECT id FROM users WHERE email = '${sqlLiteral(account.email)}'
         AND active = 1 ORDER BY id DESC LIMIT 1;`,
    )
    expect(account.id).not.toBe("")
    await saveStorageState(ACCOUNT_STORAGE_STATE, account.email, PASSWORD)
  })

  test.beforeEach(() => {
    test.skip(!!skipReason, skipReason)
  })

  // Retired the light way: the account owns nothing, so the full deletion
  // pipeline buys nothing here
  test.afterAll(() => {
    if (skipReason || !account.id) return
    deleteFirebaseUser(account.email)
    runSql(`UPDATE users SET active = 0 WHERE id = ${account.id};`)
  })

  test("ACC-01 - Change Password opens with three inputs, all required", async ({
    page,
  }) => {
    const requests = watchMeRequests(page)
    await gotoAccountProfile(page)
    const modal = await openChangePassword(page)

    for (const name of ["password", "new_password", "confirm_password"]) {
      await expect(modal.locator(`input[name="${name}"]`)).toBeVisible()
    }

    await modal.getByRole("button", { name: "UPDATE" }).click()
    await expect(modal.getByText("This field is required")).toHaveCount(3)
    await expect(modal.getByText("Change Password")).toBeVisible()

    await modal.getByRole("button", { name: "Close" }).click()
    await expect(modal).toBeHidden({ timeout: 15_000 })
    expectNoMeWrites(requests)
  })

  test("ACC-02 - Mismatched confirmation is refused before any request", async ({
    page,
  }) => {
    const requests = watchMeRequests(page)
    await gotoAccountProfile(page)
    const modal = await openChangePassword(page)

    await modal.locator('input[name="password"]').fill(PASSWORD)
    await modal.locator('input[name="new_password"]').fill(NEW_PASSWORD)
    await modal.locator('input[name="confirm_password"]').fill("Test@457")
    await expect(modal.getByText("Passwords do not match")).toBeVisible()

    await modal.getByRole("button", { name: "UPDATE" }).click()
    // Still open and still refusing: the mismatch gates the submit itself
    await expect(modal.getByText("Passwords do not match")).toBeVisible()
    await expect(modal.getByText("Change Password")).toBeVisible()

    await modal.getByRole("button", { name: "Close" }).click()
    await expect(modal).toBeHidden({ timeout: 15_000 })
    expectNoMeWrites(requests)
  })

  test("ACC-03 - A wrong current password is rejected by the server", async ({
    page,
  }) => {
    await gotoAccountProfile(page)
    const modal = await openChangePassword(page)

    await modal.locator('input[name="password"]').fill("Wrong@999")
    await modal.locator('input[name="new_password"]').fill(NEW_PASSWORD)
    await modal.locator('input[name="confirm_password"]').fill(NEW_PASSWORD)

    const rejected = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        /\/users\/me\/password/.test(response.url()),
    )
    await modal.getByRole("button", { name: "UPDATE" }).click()
    // The server refused it, not just the UI: the old password is
    // re-authenticated for real before anything changes
    expect((await rejected).status()).toBe(400)
    await expect(page.getByText("Failed to Change Password!")).toBeVisible({
      timeout: 15_000,
    })
    await expect(modal).toBeHidden({ timeout: 15_000 })
  })

  test("ACC-04 - Inline name edit saves on Enter and discards on Escape", async ({
    page,
  }) => {
    const renamed = "E2E Account Renamed"
    const requests = watchMeRequests(page)
    await gotoAccountProfile(page)

    await page.getByRole("button", { name: "Edit name" }).click()
    const input = page.getByRole("textbox", { name: "Name" })
    await input.fill(renamed)
    await input.press("Enter")

    await expect(page.getByText("Full name edited successfully!")).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByText(renamed)).toBeVisible()
    expect(runSql(`SELECT name FROM users WHERE id = ${account.id};`)).toBe(
      renamed,
    )
    account.name = renamed

    // The save's own PUT is the positive control: the watcher demonstrably
    // sees writes, so an unchanged count after Escape is a real absence
    const putsAfterSave = requests.filter((call) =>
      call.startsWith("PUT"),
    ).length
    expect(putsAfterSave).toBeGreaterThan(0)

    await page.getByRole("button", { name: "Edit name" }).click()
    await input.fill("Discarded Edit")
    await input.press("Escape")
    await expect(input).toBeHidden()
    await expect(page.getByText(renamed)).toBeVisible()
    expect(requests.filter((call) => call.startsWith("PUT")).length).toBe(
      putsAfterSave,
    )
    expect(runSql(`SELECT name FROM users WHERE id = ${account.id};`)).toBe(
      renamed,
    )
  })

  test("ACC-05 - An empty name is refused and the old name restored", async ({
    page,
  }) => {
    const requests = watchMeRequests(page)
    await gotoAccountProfile(page)

    await page.getByRole("button", { name: "Edit name" }).click()
    const input = page.getByRole("textbox", { name: "Name" })
    await input.fill("")
    await input.press("Enter")

    await expect(page.getByText("Full name can't be empty!")).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByText(account.name)).toBeVisible()
    expectNoMeWrites(requests)
    expect(runSql(`SELECT name FROM users WHERE id = ${account.id};`)).toBe(
      account.name,
    )
  })

  // Last in the group: after this, PASSWORD no longer opens the account
  test("ACC-06 - A correct password change takes effect at login", async ({
    page,
  }) => {
    test.setTimeout(120_000)
    await gotoAccountProfile(page)
    const modal = await openChangePassword(page)

    await modal.locator('input[name="password"]').fill(PASSWORD)
    await modal.locator('input[name="new_password"]').fill(NEW_PASSWORD)
    await modal.locator('input[name="confirm_password"]').fill(NEW_PASSWORD)
    await modal.getByRole("button", { name: "UPDATE" }).click()

    await expect(
      page.getByText("Your password has been successfully changed!"),
    ).toBeVisible({ timeout: 30_000 })
    await expect(modal).toBeHidden({ timeout: 15_000 })

    // The change is real: the new password signs in and the old one no longer
    // does. Checked over the API to spend one login, not two form round-trips.
    const api = await request.newContext({ baseURL: apiUrl() })
    try {
      const fresh = await api.post("/auth/login", {
        data: { email: account.email, password: NEW_PASSWORD },
      })
      expect(
        fresh.ok(),
        `login with new password: ${fresh.status()} ${await fresh.text()}`,
      ).toBeTruthy()
      const stale = await api.post("/auth/login", {
        data: { email: account.email, password: PASSWORD },
      })
      expect(stale.ok()).toBe(false)
    } finally {
      await api.dispose()
    }
  })
})
