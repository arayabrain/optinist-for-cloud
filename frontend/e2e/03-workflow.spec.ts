import * as path from "path"

import { test, expect, Page } from "@playwright/test"

import {
  skipWithoutCreds,
  freeStorageState,
  gotoDashboard,
  openWorkspace,
  importSampleData,
  ensureTutorialRecords,
  reproduceTutorial,
  runTutorial,
  RUN_TEST_TIMEOUT_MS,
  DATA_WS,
} from "./helpers"

// Committed fixture, the same one the upload spec sends
const IMAGE_FIXTURE = path.join(
  __dirname,
  "..",
  "..",
  "sample_data",
  "dev_mouse2p_short_image.tiff",
)

// Workflow page: sample data import, reproduce, runs, validation, tabs.
// WF-04..06 are tagged @slow (RUN_SLOW=1 yarn test:e2e): a fresh workspace
// holds no node outputs, so all three recompute their pipeline.

// The sidebar's Workspace section: label cell plus value cell per row
const workspaceInfo = (page: Page) =>
  page
    .locator(".MuiGrid-container")
    .filter({ has: page.getByText("ID", { exact: true }) })
    .first()

test.describe("Workflow Execution", () => {
  test.use({ storageState: freeStorageState() })

  test.beforeEach(async ({ page }) => {
    skipWithoutCreds()
    await gotoDashboard(page)
  })

  test("WF-01 - Workflow page loads with workspace info", async ({ page }) => {
    const id = await openWorkspace(page, DATA_WS)

    // `text=Workspace` / `text=ID` are case-insensitive substring matches, so
    // they pass on the sidebar's labels alone; the row's claim is that the id
    // and the name are the opened workspace's. The id is matched exactly: as a
    // substring of the whole section it also passes on a wrong id that merely
    // contains the right one, and on the name cell alone.
    const info = workspaceInfo(page)
    await expect(info.getByText(String(id), { exact: true })).toBeVisible()
    await expect(info).toContainText(DATA_WS)
    await expect(page.locator("text=Nodes").first()).toBeVisible()
  })

  test("WF-02 - Import sample data creates tutorial records", async ({
    page,
  }) => {
    test.setTimeout(180_000)
    await openWorkspace(page, DATA_WS)

    await importSampleData(page, DATA_WS)

    for (const name of ["Tutorial1", "Tutorial2", "Tutorial3", "Tutorial4"]) {
      await expect(page.locator(`tr:has-text("${name}")`).first()).toBeVisible({
        timeout: 30_000,
      })
    }
  })

  test("WF-03 - Reproduce workflow from record", async ({ page }) => {
    test.setTimeout(180_000)
    await openWorkspace(page, DATA_WS)
    await ensureTutorialRecords(page, DATA_WS)

    await reproduceTutorial(page, "Tutorial1")
    // Flowchart should contain nodes from the reproduced workflow
    await expect(page.locator(".react-flow__node").first()).toBeVisible({
      timeout: 15_000,
    })
  })

  // Both run-button paths get real coverage: RUN ALL executes the pipeline
  // under a fresh uid, RUN re-executes it under the reproduced one
  const RUNS: Array<[string, string, "RUN" | "RUN ALL"]> = [
    ["WF-04", "Tutorial1", "RUN"],
    ["WF-05", "Tutorial2", "RUN ALL"],
    ["WF-06", "Tutorial3", "RUN ALL"],
  ]
  for (const [id, tutorial, mode] of RUNS) {
    test(`${id} - Run ${tutorial} workflow via ${mode} @slow`, async ({
      page,
    }) => {
      test.setTimeout(RUN_TEST_TIMEOUT_MS)
      await openWorkspace(page, DATA_WS)
      await ensureTutorialRecords(page, DATA_WS)

      await runTutorial(page, tutorial, mode)
    })
  }

  const NO_ALGORITHM_NODES = "please add some algorithm nodes to the flowchart"
  const NO_INPUT_FILE = "please select input file"
  const snackbar = (page: Page, message: string) =>
    page.getByText(message, { exact: true })

  test("WF-07 - Run without algorithm nodes shows that error", async ({
    page,
  }) => {
    test.setTimeout(180_000)
    const id = await openWorkspace(page, "e2e-empty")
    // The uploader reads the current workspace out of the store and throws
    // "workspaceId is undefined" if the node is used before that fetch lands
    await expect(workspaceInfo(page)).toContainText(String(id), {
      timeout: 30_000,
    })

    // Both validations assign the same variable and the input-file one is
    // assigned last, so on a fresh workspace it always wins and this branch
    // went unexercised. Uploading into the default image node sets the node's
    // path on fulfillment, which is what clears the input-file branch.
    const node = page.locator(".react-flow__node-ImageFileNode")
    const uploaded = page.waitForResponse(
      (r) => /\/files\/\d+\/upload\//.test(r.url()),
      { timeout: 120_000 },
    )
    await node.locator('input[type="file"]').setInputFiles(IMAGE_FIXTURE)
    expect((await uploaded).ok()).toBeTruthy()
    await expect(node.locator(".selectFilePath")).toHaveText(
      path.basename(IMAGE_FIXTURE),
      { timeout: 30_000 },
    )

    await page.locator('button:has-text("RUN ALL")').first().click()
    await expect(snackbar(page, NO_ALGORITHM_NODES)).toBeVisible({
      timeout: 15_000,
    })
    await expect(snackbar(page, NO_INPUT_FILE)).toHaveCount(0)
  })

  test("WF-08 - Rapid clicks produce one input-file error, not duplicates", async ({
    page,
  }) => {
    await openWorkspace(page, "e2e-cooldown")

    // A fresh workspace's default input node holds no file, so this is the
    // input-file branch. Deduplication is the SnackbarProvider's
    // preventDuplicate (verified by toggling it off), NOT the run-request
    // debounce: the validation path returns before reaching it.
    const runButton = page.locator('button:has-text("RUN ALL")').first()
    await runButton.click()
    await runButton.click({ force: true })
    await runButton.click({ force: true })

    await expect(snackbar(page, NO_INPUT_FILE)).toHaveCount(1, {
      timeout: 15_000,
    })
    await expect(snackbar(page, NO_ALGORITHM_NODES)).toHaveCount(0)
  })

  test("WF-09 - Tab navigation between Workflow/Visualize/Record", async ({
    page,
  }) => {
    test.setTimeout(180_000)
    await openWorkspace(page, DATA_WS)
    await ensureTutorialRecords(page, DATA_WS)

    // aria-selected on the clicked tab is also true for a panel that rendered
    // nothing, so each tab is judged on the content it owns
    const panels: [string, string][] = [
      ["Visualize", 'main main button:has([data-testid="AddIcon"])'],
      ["Record", 'tr:has([data-testid="reproduce-button"])'],
      ["Workflow", ".react-flow__node"],
    ]
    for (const [tab, content] of panels) {
      await page.locator(`button[role="tab"]:has-text("${tab}")`).click()
      await expect(
        page.locator(`button[role="tab"]:has-text("${tab}")`),
      ).toHaveAttribute("aria-selected", "true", { timeout: 10_000 })
      await expect(page.locator(content).first()).toBeVisible({
        timeout: 30_000,
      })
    }
  })
})
