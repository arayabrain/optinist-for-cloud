import { test, expect, Page, Response } from "@playwright/test"

import {
  apiHeaders,
  login,
  sqlSkipReason,
  runSql,
  skipWithoutCreds,
  freeStorageState,
  gotoDashboard,
  ensureWorkspaceId,
  ensureCompletedTutorialRun,
  ensurePublishableAccount,
  ensurePublishedRecord,
  findDataviewRecord,
  dataviewRecord,
  setPublished,
  filterColumn,
  filterWorkspace,
  filterPublicToRecord,
  publicRow,
  openWorkspace,
  apiUrl,
  RUN_TEST_TIMEOUT_MS,
  DATA_WS,
} from "./helpers"

// Dataview: table display, public access, filters, sort, pagination,
// dialogs, thumbnails, publish/unpublish (UI + public listing). Left manual:
// DB/S3 sync verification, sync status states.

// Global-setup wipes the e2e-* workspaces each run, so the success records
// the data-dependent tests need are minted once per run: the fast no-op
// rerun of the imported Tutorial1, then a record COPY of it (bulk operations
// need "multiple" records, and only Tutorial1's rerun is a reliable no-op —
// Tutorial2's recomputes CaImAn locally and fails). The registration lands
// slightly after "Workflow finished", hence the reload-poll.

// Reload-poll the dataview until it lists `min` data rows; each attempt
// must WAIT for the grid to render — counting right after reload races the
// page's loading state
async function waitForDataRows(page: Page, wsId: number, min: number) {
  await page.goto(`/dataview/${wsId}`)
  await expect(async () => {
    await page.reload()
    await expect(
      page.locator('[role="grid"] [role="row"]').nth(min),
    ).toBeVisible({ timeout: 8_000 })
  }).toPass({ timeout: 90_000 })
}

const BASE_RECORD = "Tutorial1"
const COPY_RECORD = "Tutorial1_copy"

let recordsMinted = false
async function ensureDataviewRows(page: Page): Promise<number> {
  const id = await ensureWorkspaceId(page, DATA_WS)
  if (!recordsMinted) {
    // Read the names from the list response: counting grid rows right after
    // the container renders races the data fetch and causes spurious re-mints.
    // The gate is the copy's presence, not a row count: under RUN_SLOW the
    // tutorial specs leave two success records behind, so a count of 2 was
    // satisfied without Tutorial1_copy ever being made and DV-13 / DV-15 then
    // failed looking for it.
    // Explicit budget: the run's first load of this route compiles it, which
    // outlasts the config's 15s default action timeout
    const listSeen = page.waitForResponse(
      (r) => r.url().includes("/api/dataview"),
      { timeout: 60_000 },
    )
    await page.goto(`/dataview/${id}`)
    const listed = (await (await listSeen).json()) as {
      items?: { name?: string }[]
    }
    const names = (listed.items ?? []).map((item) => item.name)
    if (!names.includes(COPY_RECORD)) {
      if (!names.includes(BASE_RECORD)) {
        await openWorkspace(page, DATA_WS)
        await ensureCompletedTutorialRun(page, DATA_WS, BASE_RECORD)
        // The record is registered slightly AFTER "Workflow finished": the
        // copy must wait for it, or it duplicates a not-yet-successful row
        // that the dataview never lists
        await waitForDataRows(page, id, 1)
      }

      // Copy the success record; the copy keeps its success state in the DB
      await openWorkspace(page, DATA_WS)
      await page.locator('button[role="tab"]:has-text("Record")').click()
      const t1row = page
        .locator('tr:has([data-testid="reproduce-button"])')
        .filter({ has: page.getByText(BASE_RECORD, { exact: true }) })
        .first()
      await t1row.locator('input[type="checkbox"]').check()
      await page.locator('button:has-text("COPY")').click()
      await page.locator('[role="dialog"] button:has-text("copy")').click()
      await expect(
        page.getByText(COPY_RECORD, { exact: true }).first(),
      ).toBeVisible({ timeout: 60_000 })

      await waitForDataRows(page, id, 2)
    }
    recordsMinted = true
  }
  return id
}

// @slow on the describe tags every test inside it. Only success records reach
// the dataview (the listing filters on ExperimentRecord.success), the sample
// data ships metadata YAML only, and global setup wipes the e2e-* workspaces
// each run - so the first test here always pays for a real snakemake run. The
// public group below publishes one of them, so it is @slow for the same reason.
test.describe("Private Dataview @slow", () => {
  test.use({ storageState: freeStorageState() })

  let dataviewId = 0

  test.beforeAll(() => {
    ensurePublishableAccount()
  })

  test.beforeEach(async ({ page }) => {
    skipWithoutCreds()
    // The first hook mints its rows with a real Tutorial1 run (the sample data
    // ships metadata YAML only, so snakemake recomputes). A budget below
    // runTutorial's own wait expires mid-run and reports the timeout against
    // this hook rather than against the run.
    if (!recordsMinted) test.setTimeout(RUN_TEST_TIMEOUT_MS)
    await gotoDashboard(page)
    dataviewId = await ensureDataviewRows(page)
    await page.goto(`/dataview/${dataviewId}`)
    await expect(page.locator('[role="grid"]')).toBeVisible({
      timeout: 15_000,
    })
    // The minted rows are a precondition, not a maybe. Each test used to open
    // with a skip whose probe swallowed its own timeout, and a skipped test
    // reads as a pass in the summary the sheets are signed off against.
    await expect(page.locator(".MuiDataGrid-row").first()).toBeVisible({
      timeout: 30_000,
    })
  })

  test("DV-01 - Table displays with expected columns", async ({ page }) => {
    const headers = page.locator('[role="grid"] [role="columnheader"]')
    for (const name of [
      "ID",
      "Name",
      "Workspace",
      "Inputs",
      "Outputs",
      "Details",
      "Timestamp",
    ]) {
      await expect(headers.filter({ hasText: name }).first()).toBeVisible()
    }
  })

  test("DV-02 - Publish toggle shown per record", async ({ page }) => {
    const headers = page.locator('[role="grid"] [role="columnheader"]')
    await expect(headers.filter({ hasText: "Publish" }).first()).toBeVisible()

    // Scoped to the Publish cell: an unscoped checkbox/switch selector resolves
    // to the grid's own selection checkbox, which is there with the publish
    // renderer deleted. One switch per record is the row's actual claim.
    const rows = page.locator(".MuiDataGrid-row")
    const toggles = page.locator(
      '.MuiDataGrid-cell[data-field="publish_status"] .MuiSwitch-root',
    )
    await expect(toggles.first()).toBeVisible()
    await expect(toggles).toHaveCount(await rows.count())
  })

  // The grid filters server-side via per-column menus (no global search
  // box): header menu → Filter → debounced value input
  const rowCount = (page: Page) =>
    page.locator('[role="grid"] [role="row"]').count()

  async function filterByColumn(page: Page, field: string, value: string) {
    await filterColumn(page, field, value)
    await expect(async () => {
      expect(await rowCount(page)).toBe(2) // header + 1 match
    }).toPass({ timeout: 15_000 })
    await page.keyboard.press("Escape")
  }

  test("DV-03 - Filter by ID via the column menu", async ({ page }) => {
    await filterByColumn(page, "uid", "tutorial1")
    await expect(
      page.locator('.MuiDataGrid-cell[data-field="uid"]').first(),
    ).toHaveText("tutorial1")
  })

  test("DV-13 - Filter by name via the column menu", async ({ page }) => {
    await filterByColumn(page, "name", "copy")
    await expect(
      page.locator('.MuiDataGrid-cell[data-field="name"]').first(),
    ).toHaveText("Tutorial1_copy")
  })

  // The Workspace column is `filterable: !workspaceId`, so the filter lives on
  // the all-workspaces view and is deliberately off at /dataview/{id}. Both
  // that view and the public one render DataviewRecords with no workspaceId, so
  // this is the same filter the public page offers.
  test("DV-16 - Filter by workspace narrows the table", async ({ page }) => {
    // The carve-out the row names: the single-workspace view keeps the column
    // menu but drops its Filter entry
    const scopedHeader = page.locator(
      '.MuiDataGrid-columnHeader[data-field="workspace_name"]',
    )
    await scopedHeader.hover()
    await scopedHeader.locator(".MuiDataGrid-menuIcon button").click()
    await expect(page.getByRole("menuitem").first()).toBeVisible({
      timeout: 10_000,
    })
    await expect(page.getByRole("menuitem", { name: /^filter$/i })).toHaveCount(
      0,
    )
    await page.keyboard.press("Escape")

    await page.goto("/dataview")
    await expect(page.locator('[role="grid"]')).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('[role="grid"] [role="row"]').nth(1)).toBeVisible(
      {
        timeout: 30_000,
      },
    )

    await filterWorkspace(page, DATA_WS)
    await expect(page.locator('[role="grid"] [role="row"]').nth(1)).toBeVisible(
      {
        timeout: 15_000,
      },
    )
    // Every surviving row belongs to the workspace that was filtered for.
    // Re-read until the grid has re-fetched: a single read can still sample
    // the pre-filter rows.
    await expect(async () => {
      const texts = await page
        .locator('.MuiDataGrid-cell[data-field="workspace_name"]')
        .allTextContents()
      expect(texts.length).toBeGreaterThan(0)
      for (const text of texts) {
        expect(text).toContain(DATA_WS)
      }
    }).toPass({ timeout: 15_000 })

    // A workspace that cannot match empties the table, which is what makes the
    // pass above a narrowing rather than a no-op
    await filterWorkspace(page, "e2e-no-such-workspace")
    await expect(async () => {
      expect(await page.locator(".MuiDataGrid-row").count()).toBe(0)
    }).toPass({ timeout: 15_000 })
  })

  test("DV-04 - Sort by column header inverts the row order", async ({
    page,
  }) => {
    // The sort arrow is no evidence: MUI renders it from its own local sort
    // model, so it appears with the server sort unwired. Sorting is on the
    // Name column rather than Timestamp because the copy inherits its
    // original's analyzed_at, and the API returns tied timestamps in the same
    // order for asc and desc.
    const names = () =>
      page.locator('.MuiDataGrid-cell[data-field="name"]').allTextContents()
    const header = page.locator('.MuiDataGrid-columnHeader[data-field="name"]')

    await header.click()
    // localeCompare, not the default sort: MySQL orders name under a
    // case-insensitive collation, so lowercase names are not sorted last
    const ascending = [...(await names())].sort((a, b) => a.localeCompare(b))
    await expect(async () => {
      expect(await names()).toEqual(ascending)
    }).toPass({ timeout: 15_000 })

    await header.click()
    await expect(async () => {
      expect(await names()).toEqual([...ascending].reverse())
    }).toPass({ timeout: 15_000 })
  })

  test("DV-05 - Change page size", async ({ page }) => {
    // Custom pagination: a native <select name="limit"> (10/50/100)
    const limitSelect = page.locator('select[name="limit"]')
    const refetch = page.waitForResponse(
      (r) => r.url().includes("/api/dataview") && r.url().includes("limit=10"),
    )
    await limitSelect.selectOption("10")
    const { items } = (await (await refetch).json()) as { items: unknown[] }
    await expect(limitSelect).toHaveValue("10")
    // The selector reading 10 only proves the control moved, so the page size
    // is asserted on the response: the DataGrid virtualizes, and a grid holding
    // 50 records can render fewer than 10 row elements.
    expect(items.length).toBeLessThanOrEqual(10)
    await expect(page.locator('[role="grid"] [role="row"]').nth(1)).toBeVisible(
      { timeout: 15_000 },
    )
  })

  test("DV-06 - Inputs dialog opens with the visualization grid, and closes", async ({
    page,
  }) => {
    // The cell's click target is the thumbnail (a spinner while loading)
    // or the fallback icon when no thumbnail exists
    const cellinput = page
      .locator(
        '.MuiDataGrid-cell[data-field="input_data"] img, .MuiDataGrid-cell[data-field="input_data"] [data-testid="ImageIcon"]',
      )
      .first()
    await expect(cellinput).toBeVisible({ timeout: 30_000 })
    await cellinput.click()
    // Row 707: THE inputs dialog with its content, not just any dialog - the
    // InputsView title and a really-rendered plot inside it
    const dialog = page.locator('[role="dialog"]')
    await expect(dialog).toBeVisible({ timeout: 10_000 })
    await expect(dialog.getByText("Workflow Inputs")).toBeVisible()
    await expect(dialog.locator(".js-plotly-plot").first()).toBeVisible({
      timeout: 60_000,
    })

    // And the row's second half: it closes
    await page.keyboard.press("Escape")
    await expect(dialog).toBeHidden({ timeout: 10_000 })
  })

  test("DV-07 - Outputs dialog opens", async ({ page }) => {
    // The cell's click target is the thumbnail (a spinner while loading)
    // or the fallback icon when no thumbnail exists
    const celloutput = page
      .locator(
        '.MuiDataGrid-cell[data-field="output_data"] img, .MuiDataGrid-cell[data-field="output_data"] [data-testid="ImageIcon"]',
      )
      .first()
    await expect(celloutput).toBeVisible({ timeout: 30_000 })
    await celloutput.click()
    await expect(page.locator('[role="dialog"]')).toBeVisible({
      timeout: 10_000,
    })
  })

  test("DV-08 - Details dialog opens and closes", async ({ page }) => {
    await page
      .locator(
        '.MuiDataGrid-cell[data-field="details"] button, .MuiDataGrid-cell[data-field="details"] svg',
      )
      .first()
      .click()
    const dialog = page.locator('[role="dialog"]')
    await expect(dialog).toBeVisible({ timeout: 10_000 })

    await page.keyboard.press("Escape")
    await expect(dialog).toBeHidden({ timeout: 5_000 })
    await expect(page.locator('[role="grid"]')).toBeVisible()
  })

  test("DV-12 - Records show image and ROI thumbnails", async ({ page }) => {
    // Two <img> per grid is also what two rows of input thumbnails alone
    // produce, so each thumbnail is asserted in its own cell by its own alt
    await expect(
      page
        .locator('.MuiDataGrid-cell[data-field="input_data"]')
        .locator('img[alt="Input thumbnail"]')
        .first(),
    ).toBeVisible({ timeout: 30_000 })
    await expect(
      page
        .locator('.MuiDataGrid-cell[data-field="output_data"]')
        .locator('img[alt="ROI thumbnail"]')
        .first(),
    ).toBeVisible({ timeout: 30_000 })
  })

  // Exact text match — "Tutorial1" is a substring of "Tutorial1_copy"
  const rowByName = (page: Page, name: string) =>
    page
      .locator('[role="row"]')
      .filter({ has: page.getByText(name, { exact: true }) })
  const publishSwitch = (page: Page, name: string) =>
    rowByName(page, name).locator(
      '[data-field="publish_status"] input[type="checkbox"]',
    )

  // No confirmation for single records; the switch state is the acknowledgment
  async function setPublish(page: Page, name: string, on: boolean) {
    await rowByName(page, name).locator('[data-field="publish_status"]').click()
    const state = expect(publishSwitch(page, name))
    await (on
      ? state.toBeChecked({ timeout: 15_000 })
      : state.not.toBeChecked({ timeout: 15_000 }))
  }

  // Entry state is established, not asserted: a retry re-runs the whole test,
  // so a failure after publishing would otherwise fail on its first line.
  async function ensurePublish(page: Page, name: string, on: boolean) {
    const checked = await publishSwitch(page, name).isChecked({
      timeout: 15_000,
    })
    if (checked !== on) await setPublish(page, name, on)
  }

  test("DV-14 - Publish lists the record publicly; unpublish removes it", async ({
    page,
  }) => {
    await ensurePublish(page, "Tutorial1", false)
    await setPublish(page, "Tutorial1", true)
    const record = await dataviewRecord(page, "Tutorial1")

    // Listed on the public dataview (S3 sync stays manual — the listing
    // gates on publish_status only)
    await page.goto("/public")
    expect(await filterPublicToRecord(page, record)).toHaveLength(1)
    await expect(publicRow(page, record)).toBeVisible({ timeout: 15_000 })

    // Unpublish removes it from the public page
    await page.goto(`/dataview/${dataviewId}`)
    await setPublish(page, "Tutorial1", false)
    await page.goto("/public")
    // Positive pin before the absence: the grid rendering its own columns is
    // what makes the count below an absence rather than an unrendered table
    await expect(
      page.locator('.MuiDataGrid-columnHeader[data-field="name"]'),
    ).toBeVisible({ timeout: 15_000 })
    expect(await filterPublicToRecord(page, record)).toHaveLength(0)
    await expect(publicRow(page, record)).toHaveCount(0)
  })

  test("DV-17 - Public dataview filters by workspace", async ({ page }) => {
    await ensurePublish(page, "Tutorial1", true)
    const record = await dataviewRecord(page, "Tutorial1")
    await page.goto("/public")
    expect(await filterPublicToRecord(page, record)).toHaveLength(1)
    await expect(publicRow(page, record)).toBeVisible({ timeout: 15_000 })

    // The workspace filter replaces the uid one - the grid holds a single
    // filter item
    await filterWorkspace(page, DATA_WS)
    await expect(publicRow(page, record)).toBeVisible({ timeout: 15_000 })
    // Re-read until the grid has re-fetched: the filter is applied
    // asynchronously, so a single read can still sample the pre-filter rows.
    // Iterating an empty list asserts nothing, so the rows are counted first
    await expect(async () => {
      const cells = await page
        .locator('.MuiDataGrid-cell[data-field="workspace_name"]')
        .allTextContents()
      expect(cells.length).toBeGreaterThan(0)
      for (const text of cells) {
        expect(text).toContain(DATA_WS)
      }
    }).toPass({ timeout: 15_000 })

    // A workspace that cannot match empties the table, which is what makes the
    // pass above a narrowing rather than a no-op
    await filterWorkspace(page, "e2e-no-such-workspace")
    await expect(async () => {
      expect(await page.locator(".MuiDataGrid-row").count()).toBe(0)
    }).toPass({ timeout: 15_000 })

    await page.goto(`/dataview/${dataviewId}`)
    await ensurePublish(page, "Tutorial1", false)
  })

  test("DV-18 - Concurrent public reads return the same payload", async ({
    page,
  }) => {
    await ensurePublish(page, "Tutorial1", true)

    const url = `${apiUrl()}/api/public/dataview?limit=50&offset=0`
    const responses = await Promise.all(
      [0, 1, 2].map(() => page.request.get(url)),
    )
    for (const response of responses) {
      expect(response.status()).toBe(200)
    }
    const bodies = await Promise.all(responses.map((r) => r.text()))
    // Non-vacuous: an empty listing would make three identical payloads
    // trivially true
    expect(bodies[0]).toContain("Tutorial1")
    expect(bodies[1]).toBe(bodies[0])
    expect(bodies[2]).toBe(bodies[0])

    await ensurePublish(page, "Tutorial1", false)
  })

  test("DV-15 - Bulk publish and unpublish with confirmation", async ({
    page,
  }) => {
    // Select all records via the header check-all
    await page
      .locator('.MuiDataGrid-columnHeader[data-field="checkbox"] input')
      .check()

    // Bulk publish: confirmation dialog, then every toggle flips on
    await page.locator('button:has([data-testid="PublicIcon"])').click()
    const confirm = page.locator('[role="dialog"]:has-text("Bulk Publish")')
    await expect(confirm).toBeVisible({ timeout: 10_000 })
    await confirm.getByRole("button", { name: "ok" }).click()
    await expect(publishSwitch(page, "Tutorial1")).toBeChecked({
      timeout: 15_000,
    })
    await expect(publishSwitch(page, "Tutorial1_copy")).toBeChecked()

    // Bulk unpublish the same selection
    await page
      .locator('.MuiDataGrid-columnHeader[data-field="checkbox"] input')
      .check()
    await page.locator('button:has([data-testid="PublicOffIcon"])').click()
    const unconfirm = page.locator('[role="dialog"]:has-text("Bulk UnPublish")')
    await expect(unconfirm).toBeVisible({ timeout: 10_000 })
    await unconfirm.getByRole("button", { name: "ok" }).click()
    await expect(publishSwitch(page, "Tutorial1")).not.toBeChecked({
      timeout: 15_000,
    })
    await expect(publishSwitch(page, "Tutorial1_copy")).not.toBeChecked()
  })

  test("DV-19 - Rapid publish toggles end on the last action", async ({
    page,
  }) => {
    await ensurePublish(page, "Tutorial1", false)

    // Three clicks with no waits between them. Which flags they send depends
    // on how fresh the renderer's row state was for each, so the invariant is
    // asserted against the requests actually observed: the final state must
    // match the LAST flag that went out, whatever the sequence was.
    const flags: string[] = []
    page.on("request", (request) => {
      const match = request
        .url()
        .match(/\/api\/dataview\/publish\/\d+\/(on|off)$/)
      if (match && request.method() === "POST") flags.push(match[1])
    })
    const cell = rowByName(page, "Tutorial1").locator(
      '[data-field="publish_status"]',
    )
    await cell.click()
    await cell.click()
    await cell.click()

    await expect.poll(() => flags.length).toBeGreaterThanOrEqual(3)
    const shouldBePublished = flags[flags.length - 1] === "on"

    const uiState = expect(publishSwitch(page, "Tutorial1"))
    await (shouldBePublished
      ? uiState.toBeChecked({ timeout: 15_000 })
      : uiState.not.toBeChecked({ timeout: 15_000 }))
    // The server agrees with the UI on the public listing
    await expect
      .poll(
        async () => {
          const listed = await page.request.get(
            `${apiUrl()}/api/public/dataview?limit=50&offset=0&workspace_id=${dataviewId}`,
          )
          const { items } = await listed.json()
          return (items as { name?: string }[]).some(
            (item) => item.name === "Tutorial1",
          )
        },
        { timeout: 15_000 },
      )
      .toBe(shouldBePublished)

    await ensurePublish(page, "Tutorial1", false)
  })

  test("DV-20 - Concurrent publishes move the version exactly once", async ({
    page,
  }) => {
    // The version column is the optimistic lock the row is about; reachable on
    // the docker DB and on the deployed RDS over SSM. Each SSM SQL round trip
    // costs tens of seconds, so the 60s default test budget cannot hold.
    test.setTimeout(10 * 60_000)
    const noSql = sqlSkipReason()
    test.skip(!!noSql, `row 719 reads experiment_records.version: ${noSql}`)

    await ensurePublish(page, "Tutorial1", false)
    const headers = await apiHeaders(page)
    const listed = await page.request.get(
      `${apiUrl()}/api/dataview?limit=100&offset=0&workspace_id=${dataviewId}`,
      { headers },
    )
    const { items } = await listed.json()
    const record = (items as { id: number; name?: string }[]).find(
      (item) => item.name === "Tutorial1",
    )
    expect(record).toBeTruthy()

    const versionOf = () =>
      Number(
        runSql(
          `SELECT version FROM experiment_records WHERE id = ${record!.id};`,
        ),
      )
    const before = versionOf()

    // Concurrent as issued; the single-worker dev backend serializes them, so
    // what this pins is the no-double-publish half of the row: the second
    // request must land in "already published, no change" rather than write
    // again. The read-overlap retry ladder itself stays with
    // test_dataview_publish.py::test_publish_concurrent_modification_retry.
    // Publish syncs and validates against S3 in-request on a deployed env,
    // so the config's 15s actionTimeout would abort it mid-flight
    const [first, second] = await Promise.all([
      page.request.post(`${apiUrl()}/api/dataview/publish/${record!.id}/on`, {
        headers,
        timeout: 120_000,
      }),
      page.request.post(`${apiUrl()}/api/dataview/publish/${record!.id}/on`, {
        headers,
        timeout: 120_000,
      }),
    ])
    expect(first.status()).toBe(200)
    expect(second.status()).toBe(200)

    expect(versionOf() - before).toBe(1)
    expect(
      runSql(
        `SELECT publish_status FROM experiment_records
           WHERE id = ${record!.id};`,
      ),
    ).toBe("1")

    // The grid never saw these API posts, so the switch is stale and the UI
    // helper would no-op; unpublish through the same endpoint instead
    const unpublished = await page.request.post(
      `${apiUrl()}/api/dataview/publish/${record!.id}/off`,
      { headers, timeout: 120_000 },
    )
    expect(unpublished.ok()).toBe(true)
  })
})

test.describe("Public Dataview", () => {
  test("DV-09 - Public dataview while logged in hides Publish", async ({
    page,
  }) => {
    skipWithoutCreds()
    await login(page)
    await page.goto("/public")

    await expect(
      page.locator("text=OptiNiSt Public Repository").first(),
    ).toBeVisible({ timeout: 15_000 })

    // Positive pin first: the public grid renders its own columns (Owner is
    // public-only), so the absence of Publish is an absence rather than a grid
    // that never rendered
    const headers = page.locator('[role="grid"] [role="columnheader"]')
    await expect(headers.filter({ hasText: "Name" }).first()).toBeVisible({
      timeout: 15_000,
    })
    await expect(headers.filter({ hasText: "Owner" }).first()).toBeVisible()
    await expect(headers.filter({ hasText: "Publish" })).toHaveCount(0)
  })

  test("DV-10 - Public dataview loads without authentication @slow", async ({
    page,
    browser,
  }) => {
    skipWithoutCreds()
    // The publish, the reload ladder and the unpublish in finally each carry
    // their own multi-minute timeout; the budget has to clear their sum, or a
    // slow publish times the test out and takes the cleanup with it
    test.setTimeout(15 * 60_000)
    // Row 813: the grid's thumbnails are served by /api/visualizations/*, which
    // only reaches the public tier through an ALB rule keyed on the
    // DATAVIEW_PUBLIC_REQUEST header the app sends. A broken rule leaves the
    // page loading fine with every image missing, so the statuses are the row.
    // Every publishing test here reverts its own state, so the row that the
    // thumbnails come from has to be this test's own. A new context inherits
    // neither the baseURL nor the session.
    const publisher = await browser.newContext({
      baseURL: process.env.BASE_URL || "http://localhost:3000",
      storageState: freeStorageState(),
    })
    const publisherPage = await publisher.newPage()

    let unpublishAfter = false
    // Inside the try: a publish whose response is lost still committed, so
    // setup has to reach the cleanup too
    try {
      ensurePublishableAccount()
      await gotoDashboard(publisherPage)
      // Read the prior state before mutating it - a publish that throws
      // half-way still committed, and the cleanup must leave a pre-existing
      // public record public
      const before = await findDataviewRecord(publisherPage, BASE_RECORD)
      unpublishAfter = before?.publish_status !== 1
      await ensurePublishedRecord(publisherPage, BASE_RECORD)

      // A record whose PNG generation failed falls back to its source TIFF,
      // which the grid renders through ImagePlotSimpleWithLoading and never
      // requests a thumbnail for. Without this the ladder below blames the ALB
      // rule for a bad fixture.
      const published = await findDataviewRecord(publisherPage, BASE_RECORD)
      expect(
        published?.thumbnails?.image_url ?? "",
        `${BASE_RECORD} has no _thumb.png thumbnail - its PNG generation ` +
          "failed at mint time, so the grid requests no thumbnails for it",
      ).toContain("_thumb.png")

      // One anonymous load of the public grid, resolving to every thumbnail
      // status it requested. Buffer and listener are per attempt, so a
      // response landing between two attempts is dropped rather than counted
      // against the next one.
      const loadPublicGrid = async () => {
        const thumbnails: number[] = []
        const collect = (r: Response) => {
          if (r.url().includes("/api/visualizations/thumbnail/")) {
            thumbnails.push(r.status())
          }
        }
        page.on("response", collect)
        try {
          const response = await page.goto("/public")
          expect(response?.status()).toBe(200)
          await expect(
            page.locator("text=OptiNiSt Public Repository").first(),
          ).toBeVisible({ timeout: 15_000 })
          await expect(page).not.toHaveURL(/\/login/)

          await expect
            .poll(() => thumbnails.length, {
              timeout: 30_000,
              message:
                `the public grid requested no thumbnails - ${BASE_RECORD} ` +
                "was published above, so an empty grid is the public " +
                "listing failing, not a missing fixture",
            })
            .toBeGreaterThan(0)
          // The poll returns on the FIRST response, so filtering here judged
          // one or two thumbnails and let a partial regression through. Wait
          // for the grid to stop requesting before reading the whole set.
          let settled = 0
          await expect
            .poll(
              () => {
                const stable = thumbnails.length === settled
                settled = thumbnails.length
                return stable
              },
              { timeout: 30_000, intervals: [2_000] },
            )
            .toBe(true)
          return thumbnails
        } finally {
          page.off("response", collect)
        }
      }

      // The record published above syncs from S3 on its first anonymous read
      // and its thumbnail 423s while that lock is held, which is what the
      // grid's own "Retry download" is for. A broken ALB rule never turns
      // into a 200, so reloading forgives the transient without the row.
      // toPass, not poll: poll calls its function outside its own try, so a
      // listing lag that throws inside loadPublicGrid would end the ladder on
      // the first attempt with a message blaming the public listing for a
      // condition it was given 30s of the ladder's 150s to settle.
      await expect(async () => {
        const seen = await loadPublicGrid()
        expect(
          seen.filter((status) => status !== 200),
          `thumbnail responses that were not 200 (of ${seen.length}) - a ` +
            "status that never clears means the ALB rule keyed on " +
            "DATAVIEW_PUBLIC_REQUEST is not routing them to the public tier",
        ).toEqual([])
      }).toPass({ timeout: 150_000, intervals: [5_000] })
    } finally {
      // A failed assertion must not leave the record published, and a record
      // that was already public before the row must stay that way
      if (unpublishAfter) {
        await setPublished(publisherPage, BASE_RECORD, false).catch(() => {})
      }
      await publisher.close()
    }
  })

  test("DV-11 - Public API is open, private API rejects a bad token", async ({
    page,
  }) => {
    const pub = await page.request.get(
      `${apiUrl()}/api/public/dataview?limit=5`,
    )
    expect(pub.status()).toBe(200)

    const priv = await page.request.get(`${apiUrl()}/api/dataview?limit=5`, {
      headers: { Authorization: "Bearer invalid-token" },
    })
    expect([401, 403]).toContain(priv.status())
  })
})
