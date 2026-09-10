import * as fs from "fs"
import * as path from "path"

import { chromium, request } from "@playwright/test"

import {
  apiLogin,
  authHeaders,
  deleteE2eWorkspaces,
  isLocalBaseUrl,
} from "./helpers"

// 1. Checks the credentials over the API, so a bad password fails here.
// 2. Deletes workspaces named e2e-* left behind by previous runs (leftover
//    rows push new ones out of the virtualized grid).
// 3. Logs in once via the UI and saves storage state for the authed specs,
//    keeping Firebase logins per run to a handful (rate limits).
// 4. Deletes the Firebase accounts AUTH-04 leaves behind, if admin creds exist.
// Steps 2 and 4 run only against a local or development BASE_URL.
export default async function globalSetup() {
  const email = process.env.TEST_USER_EMAIL
  const password = process.env.TEST_USER_PASSWORD
  if (!email || !password) return

  const baseURL = process.env.BASE_URL || "http://localhost:3000"

  // The read-only AWS lanes document pointing BASE_URL at production, and
  // globalSetup runs whatever the file filter is, so the deletions below are
  // confined to the environments whose data is disposable.
  const disposable =
    isLocalBaseUrl() || baseURL.includes("development-optinist")

  // Before the browser login: bad credentials there are three silent 60s
  // waits for /dashboard, which reads as a hang rather than an auth error
  const { api, headers } = await apiLogin(email, password)
  try {
    await saveLoginState(baseURL, email, password)
    if (disposable) {
      await deleteE2eWorkspaces(api, headers)
      await deleteStaleUnverifiedUsers(api)
    } else {
      console.warn(
        `globalSetup: ${baseURL} is not a disposable environment; skipping cleanup`,
      )
    }
  } finally {
    await api.dispose()
  }
}

async function saveLoginState(
  baseURL: string,
  email: string,
  password: string,
) {
  const statePath = path.join(__dirname, ".auth", "free.json")
  fs.mkdirSync(path.dirname(statePath), { recursive: true })
  const browser = await chromium.launch()
  try {
    const page = await browser.newPage({ baseURL })
    for (let attempt = 1; ; attempt++) {
      await page.goto("/login")
      await page.locator('[data-testid="email"]').fill(email)
      await page.locator('[data-testid="password"]').fill(password)
      await page.locator('[data-testid="button-submit"]').click()
      try {
        await page.waitForURL(/\/dashboard/, { timeout: 60_000 })
        break
      } catch (e) {
        if (attempt >= 3) throw e
      }
    }
    // Keeps the analytics consent notice from fronting the UI when the build
    // under test was compiled with a GTM container ID.
    await page.evaluate(() =>
      localStorage.setItem("analyticsConsent", "denied"),
    )
    await page.context().storageState({ path: statePath })
  } finally {
    await browser.close()
  }
}

// The admin delete drops the Firebase user first, so the accounts AUTH-04
// leaves behind stop accumulating in the console, not just in the DB
async function deleteStaleUnverifiedUsers(
  api: Awaited<ReturnType<typeof request.newContext>>,
) {
  const email = process.env.TEST_ADMIN_EMAIL
  const password = process.env.TEST_ADMIN_PASSWORD
  if (!email || !password) return

  const loginRes = await api.post("/auth/login", { data: { email, password } })
  if (!loginRes.ok()) return
  const { access_token, ex_token } = await loginRes.json()
  const auth = authHeaders(access_token, ex_token)

  // AUTH-04's unverified registrations. The delete is a soft delete (it clears
  // `active`), so the accounts stop being usable but their storage rows survive
  // - which is why the reconciliation job has to tolerate a row whose owner is
  // no longer active.
  const listRes = await api.get(
    `/admin/users?email=e2e_unverified&offset=0&limit=100`,
    { headers: auth },
  )
  if (!listRes.ok()) {
    // 403 here means TEST_ADMIN_* is not an admin account, silent otherwise
    console.warn(
      `Stale-user sweep skipped (${listRes.status()}): ${await listRes.text()}`,
    )
    return
  }
  const { items } = await listRes.json()
  for (const user of items) {
    await api
      .delete(`/admin/users/${user.id}`, { headers: auth })
      .catch(() => {})
  }
}
