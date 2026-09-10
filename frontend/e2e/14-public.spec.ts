import { test, expect, Browser, Locator, Page } from "@playwright/test"

import {
  CLOUDWATCH_POLL,
  PUBLIC_LOG_GROUP,
  apiUrl,
  cloudwatchHas,
  isLocalBaseUrl,
  ensurePublishableAccount,
  ensurePublishedRecord,
  findDataviewRecord,
  dataviewRecord,
  filterPublicToRecord,
  publicRow,
  PublicRecordRef,
  freeStorageState,
  gotoDashboard,
  setPublished,
  skipWithoutCreds,
} from "./helpers"

// Public-instance behaviour a browser can observe on any tier: the SPA shell
// for deep links, the health endpoint, the chunk-load auto-reload, and the
// frontend error reporter. The @slow group publishes real records and reads
// their input data back anonymously, the way a public visitor would. ALB
// routing, CloudWatch delivery and S3 on-demand sync are pinned by
// test_public_instance_config.py, test_log_report.py and
// test_outputs_on_demand_sync.py - no e2e lane can reach them.

test.describe("Public instance", () => {
  test("PUB-01 - A deep link without login serves the SPA shell, not a 404", async ({
    page,
  }) => {
    const response = await page.goto("/workspaces/123")
    expect(response?.status()).toBe(200)
    // React Router took over: the shell booted, saw no session, and
    // client-routed to a working login form. The backend's own shell branches
    // and cache headers are pinned by test_spa_shell_and_health.py.
    await expect(page).toHaveURL(/\/login/, { timeout: 30_000 })
    await expect(page.locator('[data-testid="button-submit"]')).toBeVisible({
      timeout: 15_000,
    })
  })

  test("PUB-02 - /health answers 200 healthy to a browser navigation", async ({
    page,
  }) => {
    // A navigation sends Accept: text/html, so this also pins the SPA
    // middleware's /health carve-out: without it the browser would get the
    // shell here instead of the health payload
    const response = await page.goto(`${apiUrl()}/health`)
    expect(response?.status()).toBe(200)
    // Exact payload: a substring match would also accept the handler's
    // degraded "warning" body, or "unhealthy"
    expect(await response?.json()).toEqual({ status: "healthy" })
    await expect(page.locator("body")).toContainText("healthy")
  })

  test("PUB-03 - A chunk load error reloads the page once", async ({
    page,
  }) => {
    await page.goto("/login")
    await expect(page.locator('[data-testid="button-submit"]')).toBeVisible({
      timeout: 30_000,
    })
    await page.evaluate(() => {
      ;(window as { __pub03?: boolean }).__pub03 = true
    })

    const warned = page.waitForEvent("console", {
      predicate: (message) =>
        message
          .text()
          .includes(
            "[chunkLoadReload] Chunk load error detected; attempting reload",
          ),
      timeout: 15_000,
    })
    const reloaded = page.waitForEvent("load", { timeout: 15_000 })
    // A deploy-time chunk failure surfaces as a rejected dynamic-import
    // promise, so the unhandledrejection path is the one production takes.
    // Deferred to a task so the synchronous reload cannot destroy this
    // evaluate's own execution context mid-call.
    await page.evaluate(() => {
      setTimeout(() => {
        const error = new Error("Loading chunk 42 failed")
        error.name = "ChunkLoadError"
        Promise.reject(error)
      }, 0)
    })
    await Promise.all([warned, reloaded])

    // The document was really replaced, and the one-shot reload guard is set
    expect(
      await page.evaluate(() => (window as { __pub03?: boolean }).__pub03),
    ).toBeUndefined()
    expect(
      await page.evaluate(() =>
        sessionStorage.getItem("chunk-reload-attempted"),
      ),
    ).toBe("1")
  })
})

test.describe("Frontend error reporting", () => {
  test.use({ storageState: freeStorageState() })

  // Throws a uniquely marked error and waits for the batch carrying it. The
  // reporter flushes every 5s, so the marker is what picks this error's POST
  // out rather than whichever one happens to land first.
  async function shipMarkedError(page: Page): Promise<string> {
    await gotoDashboard(page)
    const marker = `e2e-pub04 ${Date.now()}`
    const shipped = page.waitForResponse(
      (r) =>
        r.request().method() === "POST" &&
        r.url().includes("/log-report/frontend-errors") &&
        (r.request().postData() ?? "").includes(marker),
      { timeout: 30_000 },
    )
    await page.evaluate((message) => {
      setTimeout(() => {
        throw new Error(message)
      }, 0)
    }, marker)

    const response = await shipped
    expect(response.status()).toBe(200)
    expect((await response.json()).count).toBeGreaterThanOrEqual(1)

    // The marker rode in a real error entry, not just anywhere in the body
    const batch = JSON.parse(response.request().postData() ?? "{}") as {
      errors: { level: string; message: string }[]
    }
    const entry = batch.errors.find((e) => e.message.includes(marker))
    expect(entry?.level).toBe("error")
    return marker
  }

  test("PUB-04 - A thrown error is shipped to /log-report/frontend-errors", async ({
    page,
  }) => {
    skipWithoutCreds()
    await shipMarkedError(page)
  })

  // Row 829's other half, as its own row rather than an inline branch: a 200
  // only means the endpoint accepted the batch. Kept separate so a local run
  // reports it skipped instead of passing PUB-04 with this half never run.
  test("PUB-07 - The shipped error reaches the public tier's log group", async ({
    page,
  }) => {
    test.skip(
      isLocalBaseUrl(),
      "reads the deployed tier's CloudWatch log group; BASE_URL is local",
    )
    skipWithoutCreds()
    test.setTimeout(360_000)

    const since = Date.now() - 60_000
    const marker = await shipMarkedError(page)
    // /log-report/* is an ALB rule onto the public tier, so that is where it
    // must appear.
    await expect
      .poll(() => cloudwatchHas(PUBLIC_LOG_GROUP, marker, since), {
        ...CLOUDWATCH_POLL,
        message: `${marker} never reached ${PUBLIC_LOG_GROUP}`,
      })
      .toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Published input data, read back anonymously. Only success records reach the
// dataview and the sample data ships metadata YAML only, so each record is
// minted with a real workflow run - Tutorial1 carries the CSV and TIFF input
// nodes, Tutorial4 the HDF5 and MAT ones.
// ---------------------------------------------------------------------------

// The public page must be readable with no session at all; the shared
// storage state is cleared explicitly because a new context inherits it
async function anonymousPage(browser: Browser): Promise<Page> {
  const context = await browser.newContext({
    baseURL: process.env.BASE_URL || "http://localhost:3000",
    storageState: undefined,
  })
  return context.newPage()
}

// Open the record's Workflow Inputs dialog from the public listing and
// return the dialog locator. Takes the record rather than the name: the listing
// spans every account, and a name match with .first() picks silently
async function openPublicInputs(page: Page, record: PublicRecordRef) {
  await page.goto("/public")
  // Really anonymous: rows 815-818 are about a visitor with no session
  expect(
    await page.evaluate(() => localStorage.getItem("access_token")),
  ).toBeNull()
  // Server-side filter first: the grid is virtualized, so an unfiltered
  // listing can hold the target row outside the DOM
  expect(await filterPublicToRecord(page, record)).toHaveLength(1)
  const row = publicRow(page, record)
  await expect(row).toBeVisible({ timeout: 30_000 })
  await row
    .locator(
      '[data-field="input_data"] img, [data-field="input_data"] [data-testid="ImageIcon"]',
    )
    .first()
    .click()
  const dialog = page.locator('[role="dialog"]:has-text("Workflow Inputs")')
  await expect(dialog).toBeVisible({ timeout: 15_000 })
  return dialog
}

// Each input node renders one panel titled by a "Type: <dataType>" chip. The
// panel's plot rendering is what "data loads correctly" means: a failed load
// renders the panel's error text instead and never mounts a plot
function inputPanel(dialog: Locator, dataType: string) {
  return dialog
    .locator(".MuiGrid-item")
    .filter({ hasText: `Type: ${dataType}` })
}

test.describe("Public input data loads @slow", () => {
  test.use({ storageState: freeStorageState() })

  test.beforeAll(() => {
    ensurePublishableAccount()
  })

  test.beforeEach(async ({ page }) => {
    skipWithoutCreds()
    // Publish plus two anonymous data loads; minting raises this further
    test.setTimeout(300_000)
    await gotoDashboard(page)
  })

  test("PUB-05 - HDF5 and MAT input data load on the public page", async ({
    page,
    browser,
  }) => {
    // Read the prior state before mutating it, and publish inside the try - a
    // publish that throws half-way still committed, so it has to reach the
    // cleanup, which must leave a pre-existing public record public
    const before = await findDataviewRecord(page, "Tutorial4")
    const unpublishAfter = before?.publish_status !== 1
    const viewer = await anonymousPage(browser)
    try {
      await ensurePublishedRecord(page, "Tutorial4")
      const dialog = await openPublicInputs(
        viewer,
        await dataviewRecord(page, "Tutorial4"),
      )
      for (const dataType of ["hdf5", "matlab"]) {
        await expect(
          inputPanel(dialog, dataType).locator(".js-plotly-plot").first(),
        ).toBeVisible({ timeout: 120_000 })
      }
    } finally {
      await viewer.context().close()
      // Best-effort: a cleanup failure must not mask the assertion that
      // failed, and a record that was already public must stay that way
      if (unpublishAfter) {
        await setPublished(page, "Tutorial4", false).catch(() => {})
      }
    }
  })

  test("PUB-06 - CSV and TIFF input data load on the public page", async ({
    page,
    browser,
  }) => {
    // Read the prior state before mutating it, and publish inside the try - a
    // publish that throws half-way still committed, so it has to reach the
    // cleanup, which must leave a pre-existing public record public
    const before = await findDataviewRecord(page, "Tutorial1")
    const unpublishAfter = before?.publish_status !== 1
    const viewer = await anonymousPage(browser)
    try {
      await ensurePublishedRecord(page, "Tutorial1")
      const dialog = await openPublicInputs(
        viewer,
        await dataviewRecord(page, "Tutorial1"),
      )
      // The CSV panel renders a data table, the TIFF one a plotly image
      await expect(
        inputPanel(dialog, "csv").locator(".MuiDataGrid-row").first(),
      ).toBeVisible({ timeout: 120_000 })
      await expect(
        inputPanel(dialog, "image").locator(".js-plotly-plot").first(),
      ).toBeVisible({ timeout: 120_000 })
    } finally {
      await viewer.context().close()
      // Best-effort: a cleanup failure must not mask the assertion that
      // failed, and a record that was already public must stay that way
      if (unpublishAfter) {
        await setPublished(page, "Tutorial1", false).catch(() => {})
      }
    }
  })
})
