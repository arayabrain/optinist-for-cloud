import { test, expect } from "@playwright/test"

import {
  skipWithoutCreds,
  freeStorageState,
  gotoDashboard,
  goToWorkspaces,
  ensureWorkspaceId,
  createWorkspace,
  DATA_WS,
} from "./helpers"

// Workspace list: create, display, navigation, storage reload, delete

const WS_NAME = `e2e-ws-${Date.now()}`

test.describe("Workspace", () => {
  test.use({ storageState: freeStorageState() })

  test.beforeEach(async ({ page }) => {
    skipWithoutCreds()
    await gotoDashboard(page)
  })

  test("WS-01 - Create new workspace", async ({ page }) => {
    await createWorkspace(page, WS_NAME)

    // A workspace created this second holds no data
    await expect(
      page
        .locator(`.MuiDataGrid-row:has-text("${WS_NAME}")`)
        .first()
        .locator('[data-field="data_usage"]'),
    ).toHaveText("0 Bytes")
  })

  test("WS-02 - Workspace list displays with columns", async ({ page }) => {
    // Own data row: global-setup deletes every e2e-* workspace at run start, so
    // depending on WS-01 makes this red whenever it runs alone or first fails
    await ensureWorkspaceId(page, DATA_WS)
    const listed = page.waitForResponse(
      (r) => /\/workspaces\?/.test(r.url()) && r.request().method() === "GET",
      { timeout: 60_000 },
    )
    await goToWorkspaces(page)
    const { items } = await (await listed).json()
    expect(items.length).toBeGreaterThan(0)

    for (const column of [
      "ID",
      "Workspace Name",
      "Owner",
      "Created",
      "Data size",
    ]) {
      await expect(
        page.getByRole("columnheader", { name: column, exact: true }),
      ).toBeVisible({ timeout: 30_000 })
    }

    // `text=ID` is a case-insensitive substring match on the label; the row's
    // claim is that the listed workspace's own id and name are displayed
    const first = items[0]
    const row = page
      .locator(`.MuiDataGrid-row:has-text("${first.name}")`)
      .first()
    await expect(row.locator('[data-field="display_number"]')).toHaveText(
      String(first.display_number ?? first.id),
    )
    await expect(row.locator('[data-field="name"]')).toContainText(first.name)
  })

  test("WS-03 - Workflow button navigates to workflow page", async ({
    page,
  }) => {
    // Its own row, asserted rather than probed: an empty grid used to skip here,
    // and a skipped row reads as a pass on the sheet it is signed off against
    const id = await ensureWorkspaceId(page, DATA_WS)
    await goToWorkspaces(page)
    const row = page.locator(`.MuiDataGrid-row:has-text("${DATA_WS}")`).first()
    await expect(row).toBeVisible({ timeout: 30_000 })

    await row.locator('button:has-text("Workflow")').click()
    await expect(page).toHaveURL(new RegExp(`/workspaces/${id}$`), {
      timeout: 15_000,
    })
    await expect(
      page.locator('button[role="tab"]:has-text("Workflow")'),
    ).toBeVisible()
  })

  test("WS-04 - Storage reload button refreshes storage", async ({ page }) => {
    await goToWorkspaces(page)
    const reloadButton = page.locator('button:has-text("Reload")')
    await expect(reloadButton).toBeVisible()

    const refreshed = page.waitForResponse(
      (r) =>
        r.url().includes("/workspaces/refresh-storage") &&
        r.request().method() === "POST",
      { timeout: 60_000 },
    )
    await reloadButton.click()
    const { refreshed_workspaces } = await (await refreshed).json()

    // The count is part of the copy, and it comes from the response rather than
    // from the test: `/refreshed/i` also matched the storage panel's caption
    await expect(
      page.getByText(
        `Storage refreshed for ${refreshed_workspaces} workspaces!`,
        { exact: true },
      ),
    ).toBeVisible({ timeout: 60_000 })
  })

  test("WS-07 - Storage refresh fires once per session", async ({ page }) => {
    // The gate is a sessionStorage flag the surrounding hook already set, so
    // clear it and let this test own the session's first refresh
    await page.evaluate(() =>
      sessionStorage.removeItem("storage-refreshed-on-login"),
    )
    const refreshes: string[] = []
    page.on("request", (r) => {
      if (
        r.method() === "POST" &&
        r.url().includes("/workspaces/refresh-storage")
      ) {
        refreshes.push(r.url())
      }
    })

    for (const route of ["/dashboard", "/workspaces", "/account"]) {
      // Each load decides whether to refresh only after its own /users/me
      // resolves; navigating away before that cancels the decision, and the
      // count then reads 1 whether the gate is there or not
      const meSeen = page.waitForResponse(
        (r) => r.url().endsWith("/users/me") && r.request().method() === "GET",
        { timeout: 30_000 },
      )
      await page.goto(route)
      await expect(page).toHaveURL(new RegExp(`${route}$`), { timeout: 30_000 })
      await meSeen
      await page.waitForTimeout(2_000)
    }
    expect(refreshes).toHaveLength(1)
  })

  test("WS-05 - Dataview button navigates to dataview page", async ({
    page,
  }) => {
    const id = await ensureWorkspaceId(page, DATA_WS)
    await goToWorkspaces(page)
    const row = page.locator(`.MuiDataGrid-row:has-text("${DATA_WS}")`).first()
    await expect(row).toBeVisible({ timeout: 30_000 })

    await row.locator('button:has-text("Dataview")').click()
    await expect(page).toHaveURL(new RegExp(`/dataview/${id}$`), {
      timeout: 15_000,
    })
  })

  test("WS-06 - Delete workspace", async ({ page }) => {
    await goToWorkspaces(page)
    // Wait for the grid to load before checking for the row, otherwise the
    // fallback creates a duplicate workspace
    await page
      .locator(".MuiDataGrid-row")
      .first()
      .waitFor({ timeout: 30_000 })
      .catch(() => {})

    // Delete the workspace created in WS-01; create one if it isn't there
    let row = page.locator(`.MuiDataGrid-row:has-text("${WS_NAME}")`)
    if (!(await row.count())) {
      await createWorkspace(page, WS_NAME)
      row = page.locator(`.MuiDataGrid-row:has-text("${WS_NAME}")`)
    }

    await row.locator('[data-testid="DeleteIcon"]').click()
    await page.locator('[role="dialog"] [placeholder="DELETE"]').fill("DELETE")
    await page.locator('button:has-text("Delete Workspace")').click()

    await expect(
      page.locator(`.MuiDataGrid-row:has-text("${WS_NAME}")`),
    ).toHaveCount(0, { timeout: 15_000 })
  })
})
