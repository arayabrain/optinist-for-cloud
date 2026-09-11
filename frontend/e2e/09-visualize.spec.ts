import { test, expect, Page } from "@playwright/test"

import {
  apiHeaders,
  apiUrl,
  skipWithoutCreds,
  freeStorageState,
  gotoDashboard,
  openWorkspace,
  ensureTutorialRecords,
  reproduceTutorial,
  runTutorial,
  DATA_WS,
} from "./helpers"

// Visualize tab. VIS-01 asserts the sidebar info; VIS-02..05 drive the plot
// editor against Tutorial1's node outputs. `sample_data/tutorial/output` ships
// workflow YAML only, so those outputs come from a run earlier in the session,
// not from the import. VIS-06 commits an ROI edit for real, so it mints its
// own run first.

// MUI standard Select: the label's FormControl wraps the select div. The
// sidebar stacks one control group per plot box, so pick the box's group.
async function selectFromMui(
  page: Page,
  label: string,
  option: string,
  box: "first" | "last" = "first",
) {
  const select = page.locator(
    `div:has(> label:text-is("${label}")) .MuiSelect-select`,
  )
  await (box === "first" ? select.first() : select.last()).click()
  await page.getByRole("option", { name: option, exact: true }).click()
}

// Open Visualize, add a plot box, and select the sample TIFF into it
async function addImagePlot(page: Page) {
  await page.locator('button[role="tab"]:has-text("Visualize")').click()
  await page
    .locator('main main button:has([data-testid="AddIcon"])')
    .first()
    .click()
  await selectFromMui(page, "Select Item", "sample_mouse2p_image.tiff")
  await expect(page.locator(".js-plotly-plot").first()).toBeVisible({
    timeout: 60_000,
  })
}

// Read the ROI ids the rendered overlay actually contains. The roi trace's z is
// a pixel grid whose values are the global ROI index, so the distinct non-null
// values are exactly the ids the selected projection (cell_roi / non_cell_roi)
// holds.
async function roiIds(page: Page): Promise<number[]> {
  return page.evaluate(() => {
    const plot = document.querySelector(".js-plotly-plot") as unknown as {
      data?: { name?: string; z?: (number | null)[][] }[]
    }
    const z = plot?.data?.find((t) => t.name === "roi")?.z ?? []
    const ids = new Set<number>()
    for (const row of z) for (const v of row) if (v != null) ids.add(v)
    return [...ids].sort((a, b) => a - b)
  })
}

// Select an ROI by id. Plotly's own hit test needs the ROI's pixel coordinates,
// which the test would have to re-derive from the axis transform; emitting the
// event Plotly would emit drives the real ImagePlot handler (redux dispatch and
// the selection context) and only skips the hit test itself.
async function clickRoi(page: Page, id: number) {
  await page.evaluate((roiId) => {
    const gd = document.querySelector(".js-plotly-plot") as unknown as {
      emit: (name: string, payload: unknown) => void
    }
    gd.emit("plotly_click", { points: [{ curveNumber: 1, z: roiId }] })
  }, id)
  await expect(page.getByTestId("roi-selected-ids")).toContainText(String(id))
}

// Switch the projection and wait for its ids to land.
async function selectRoiProjection(page: Page, outputKey: string) {
  await selectFromMui(page, "Select Roi", outputKey)
  await expect.poll(() => roiIds(page), { timeout: 60_000 }).not.toHaveLength(0)
}

// Run one Edit ROI action: open the editor, pick the action, select the ROIs,
// OK. The "Edit ROI" link is only there when the editor is closed - after an OK
// the toolbar stays open on the pending edits, which is how two actions get
// staged into a single commit.
async function runRoiAction(
  page: Page,
  action: string,
  endpoint: RegExp,
  ids: number[],
  prepare?: () => Promise<void>,
) {
  const editLink = page.getByText("Edit ROI", { exact: true })
  if (await editLink.isVisible()) await editLink.click()
  const actionLink = page.getByText(action, { exact: true })
  await expect(actionLink).toBeVisible({ timeout: 30_000 })
  await actionLink.click()
  if (prepare) await prepare()
  for (const id of ids) await clickRoi(page, id)

  const posted = page.waitForResponse(
    (r) => r.request().method() === "POST" && endpoint.test(r.url()),
    { timeout: 60_000 },
  )
  await page.getByText("OK", { exact: true }).click()
  expect((await posted).status(), endpoint.source).toBe(200)
}

// Commit Edit renders only once statusRoi has entries after the getStatus
// round-trip, and runs the recompute in-request.
async function commitRoiEdit(page: Page) {
  const commitEdit = page.getByTestId("roi-commit-edit")
  await expect(commitEdit).toBeVisible({ timeout: 60_000 })
  const committed = page.waitForResponse(
    (r) => r.request().method() === "POST" && /commit_edit/.test(r.url()),
    { timeout: 600_000 },
  )
  await commitEdit.click()
  expect((await committed).status(), "commit_edit").toBe(200)
  await expect(
    page.getByText("Successfully committed to Edit ROI."),
  ).toBeVisible({ timeout: 120_000 })
}

async function editRoiAndCommit(
  page: Page,
  action: string,
  endpoint: RegExp,
  ids: number[],
  prepare?: () => Promise<void>,
) {
  await runRoiAction(page, action, endpoint, ids, prepare)
  await commitRoiEdit(page)
}

test.describe("Visualize", () => {
  test.use({ storageState: freeStorageState() })

  test.beforeEach(async ({ page }) => {
    test.setTimeout(240_000)
    skipWithoutCreds()
    await gotoDashboard(page)
    await openWorkspace(page, DATA_WS)
    await ensureTutorialRecords(page, DATA_WS)
    // Load a known workflow into the store so the sidebar has data
    await reproduceTutorial(page, "Tutorial1")
  })

  test("VIS-01 - Visualize tab shows workspace and workflow info", async ({
    page,
  }) => {
    await page.locator('button[role="tab"]:has-text("Visualize")').click()
    await expect(
      page.locator('button[role="tab"]:has-text("Visualize")'),
    ).toHaveAttribute("aria-selected", "true", { timeout: 10_000 })

    // CurrentPipelineInfo sidebar, exactly (rows BT-402 / 512): the loose
    // substring this used to match would also pass on a stale select option
    // elsewhere on the page. The workflow ID must be the reproduced record's
    // own uid, read from the API rather than trusted from the sidebar.
    await expect(page.locator("text=NAME").first()).toBeVisible({
      timeout: 15_000,
    })
    await expect(page.getByText(DATA_WS, { exact: true }).first()).toBeVisible()
    await expect(
      page.getByText("Tutorial1", { exact: true }).first(),
    ).toBeVisible()

    const wsId = page.url().match(/workspaces\/(\d+)/)?.[1] ?? ""
    expect(wsId, "the workspace id is in the URL").not.toBe("")
    await expect(page.getByText(wsId, { exact: true }).first()).toBeVisible()

    const res = await page.request.get(`${apiUrl()}/experiments/${wsId}`, {
      headers: await apiHeaders(page),
    })
    expect(res.ok(), await res.text()).toBe(true)
    const experiments = (await res.json()) as Record<string, { name: string }>
    const uid = Object.keys(experiments).find(
      (key) => experiments[key].name === "Tutorial1",
    )
    expect(
      uid,
      "no Tutorial1 record to compare the sidebar against",
    ).toBeTruthy()
    await expect(page.getByText(uid!, { exact: true }).first()).toBeVisible()
  })

  test("VIS-02 - Add Cell ROI plot renders image with ROI overlay @slow", async ({
    page,
  }) => {
    test.setTimeout(60 * 60_000)
    // Mints its own run: `cell_roi` is a suite2p_roi node output and the ROI
    // route answers 503 without one. Nothing orders a run-minting test ahead of
    // this one, so relying on "a run earlier in the session" is a coin flip.
    await runTutorial(page, "Tutorial1", "RUN ALL")
    await addImagePlot(page)
    // @slow because `cell_roi` is a suite2p_roi node output, not shipped input:
    // without a completed run the ROI route answers 503. The other VIS tests
    // plot the sample TIFF, which the import does ship.

    // `.js-plotly-plot` and `text=cell_roi` are both already true before the ROI
    // loads: addImagePlot awaited the plot, and `cell_roi` is the select's own
    // displayed value. ImagePlot gates rendering on the *image* error only, so a
    // failed getRoiData leaves the plot visible and both of those assertions
    // passing.
    //
    // Trace *count* is no good either: ImagePlot always builds a fixed two-trace
    // array ("images" then "roi"), with the roi trace's z starting as []. So the
    // ROI-specific artefact is that trace's z gaining rows.
    const roiRowCount = () =>
      page.evaluate(() => {
        const plot = document.querySelector(".js-plotly-plot") as unknown as {
          data?: { name?: string; z?: unknown[] }[]
        }
        return plot?.data?.find((t) => t.name === "roi")?.z?.length ?? 0
      })
    expect(await roiRowCount()).toBe(0)

    // getRoiData GETs /api/visualizations/image/<path>/cell_roi.json while
    // getStatus POSTs .../cell_roi.json/status on the same prefix, so matching
    // the prefix alone resolves on whichever lands first. Pin the ROI data GET.
    const roiResponse = page.waitForResponse(
      (r) =>
        r.request().method() === "GET" &&
        /\/api\/visualizations\/image\/.*_roi\.json(\?|$)/i.test(r.url()),
      { timeout: 60_000 },
    )
    await selectFromMui(page, "Select Roi", "cell_roi")
    expect((await roiResponse).status()).toBe(200)

    await expect.poll(roiRowCount, { timeout: 60_000 }).toBeGreaterThan(0)

    // Both selectors keep their values after the plot re-renders
    await expect(
      page.locator("text=sample_mouse2p_image.tiff").first(),
    ).toBeVisible()
    await expect(page.locator("text=cell_roi").first()).toBeVisible()
  })

  test("VIS-03 - Play advances image frames; Pause stops", async ({ page }) => {
    await addImagePlot(page)
    const frame = page.locator('input[type="range"]').first()
    const start = Number(await frame.inputValue())
    await page.getByRole("button", { name: "Play" }).click()
    // 500ms/frame default — the index must advance
    await expect
      .poll(async () => Number(await frame.inputValue()), { timeout: 15_000 })
      .toBeGreaterThan(start)
    await page.getByRole("button", { name: "Pause" }).click()
    const paused = Number(await frame.inputValue())
    await page.waitForTimeout(1_500)
    expect(Number(await frame.inputValue())).toBe(paused)
  })

  test("VIS-04 - Add a second plot of a different type", async ({ page }) => {
    await addImagePlot(page)
    // The next empty plot box gets a timeseries item
    await page
      .locator('main main button:has([data-testid="AddIcon"])')
      .first()
      .click()
    await selectFromMui(page, "Select Item", "fluorescence", "last")
    await expect(page.locator(".js-plotly-plot")).toHaveCount(2, {
      timeout: 60_000,
    })

    // Two plots is also what two image plots give. The image plot draws a
    // plotly heatmap and the timeseries one does not, so exactly one of the two
    // being a heatmap is where "a different type" is observable. (The
    // timeseries traces stay out of _fullData until a curve is selected, so
    // their own type is not assertable here.)
    const heatmapPlots = () =>
      page.evaluate(
        () =>
          Array.from(document.querySelectorAll(".js-plotly-plot")).filter(
            (el) =>
              (
                (el as unknown as { _fullData?: { type?: string }[] })
                  ._fullData ?? []
              ).some((trace) => trace.type === "heatmap"),
          ).length,
      )
    await expect.poll(heatmapPlots, { timeout: 60_000 }).toBe(1)
  })

  test("VIS-05 - Edit ROI opens the ROI editor; Cancel exits", async ({
    page,
  }) => {
    await addImagePlot(page)
    await selectFromMui(page, "Select Roi", "cell_roi")
    await page.getByText("Edit ROI", { exact: true }).click()

    for (const action of ["Add ROI", "Delete ROI", "Merge ROI"]) {
      await expect(page.getByText(action, { exact: true })).toBeVisible({
        timeout: 15_000,
      })
    }
    // Committing (OK) mutates the ROI data and starts a processing run —
    // that half is VIS-06; Cancel must leave the editor cleanly
    await page.getByText("Cancel", { exact: true }).first().click()
    await expect(page.getByText("Add ROI", { exact: true })).toBeHidden()
  })

  // Row BT-407's commit half: Add ROI, OK, then Commit Edit really re-runs
  // the ROI processing server-side and reports success. Mints its own run:
  // the commit recomputes off a real suite2p output, and the workspace is
  // wiped at every suite start.
  test("VIS-06 - Edit ROI commit really recomputes and succeeds @slow", async ({
    page,
  }) => {
    test.setTimeout(30 * 60_000)
    await runTutorial(page, "Tutorial1", "RUN ALL")
    await addImagePlot(page)
    await selectFromMui(page, "Select Roi", "cell_roi")
    await page.getByText("Edit ROI", { exact: true }).click()
    await page.getByText("Add ROI", { exact: true }).click()
    // The pending-ROI overlay is what registers the rectangle OK will post;
    // clicking OK before it mounts posts nothing (observed failure mode).
    await expect(page.getByTestId("roi-add-overlay")).toBeVisible({
      timeout: 15_000,
    })

    // OK posts the pending ROI (a default rectangle when nothing is dragged)
    const added = page.waitForResponse(
      (r) => r.request().method() === "POST" && /add_roi/.test(r.url()),
      { timeout: 60_000 },
    )
    await page.getByText("OK", { exact: true }).click()
    expect((await added).status(), "add_roi").toBe(200)

    // Commit Edit renders only once statusRoi has entries after the
    // getStatus round-trip, which can outlast the default action timeout.
    const commitEdit = page.getByTestId("roi-commit-edit")
    await expect(commitEdit).toBeVisible({ timeout: 60_000 })

    // Commit Edit runs the EDIT_ROI recompute in-request; the snackbar is
    // the row's own "Success Edit ROI"
    const committed = page.waitForResponse(
      (r) => r.request().method() === "POST" && /commit_edit/.test(r.url()),
      { timeout: 600_000 },
    )
    await commitEdit.click()
    expect((await committed).status(), "commit_edit").toBe(200)
    await expect(
      page.getByText("Successfully committed to Edit ROI."),
    ).toBeVisible({ timeout: 120_000 })
  })
  // Issues #472 / #486: a cell ROI demoted by Delete has to be reachable and
  // promotable again. Round trip: add a cell ROI, delete it (which is a demote —
  // the index and its fluorescence row survive), find it in non_cell_roi, and
  // promote it back. The non_cell_roi assertion is also the regression test for
  // that projection going stale: before this change only cell_roi.json was
  // regenerated on commit, so the demoted ROI never appeared there.
  test("VIS-07 - a deleted ROI reappears in non_cell_roi and can be promoted back @slow", async ({
    page,
  }) => {
    test.setTimeout(60 * 60_000)
    await runTutorial(page, "Tutorial1", "RUN ALL")
    await addImagePlot(page)
    await selectRoiProjection(page, "cell_roi")
    const before = await roiIds(page)

    await editRoiAndCommit(page, "Add ROI", /add_roi/, [], async () => {
      await expect(page.getByTestId("roi-add-overlay")).toBeVisible({
        timeout: 15_000,
      })
    })

    const added = (await roiIds(page)).filter((id) => !before.includes(id))
    expect(added, "Add ROI produced exactly one new cell ROI").toHaveLength(1)
    const roi = added[0]

    await editRoiAndCommit(page, "Delete ROI", /delete_roi/, [roi])
    await expect
      .poll(() => roiIds(page), { timeout: 60_000 })
      .not.toContain(roi)

    await selectRoiProjection(page, "non_cell_roi")
    expect(
      await roiIds(page),
      "the deleted ROI is now a non-cell ROI",
    ).toContain(roi)

    await editRoiAndCommit(page, "Set as Cell ROI", /promote_roi/, [roi])
    await expect
      .poll(() => roiIds(page), { timeout: 60_000 })
      .not.toContain(roi)

    await selectRoiProjection(page, "cell_roi")
    expect(
      await roiIds(page),
      "the promoted ROI is a cell ROI again",
    ).toContain(roi)
  })

  // Issue #486's un-merge half. Merge keeps its sources: they are demoted to
  // non-cell with their own indices and fluorescence rows intact, so promoting
  // them back is the un-merge, and the merged ROI is removed with Delete.
  test("VIS-08 - merged ROIs can be un-merged by promoting the sources @slow", async ({
    page,
  }) => {
    test.setTimeout(60 * 60_000)
    await runTutorial(page, "Tutorial1", "RUN ALL")
    await addImagePlot(page)
    await selectRoiProjection(page, "cell_roi")
    const before = await roiIds(page)
    expect(before.length, "need two cell ROIs to merge").toBeGreaterThan(1)
    const [first, second] = before

    await editRoiAndCommit(page, "Merge ROI", /merge_roi/, [first, second])
    const afterMerge = await roiIds(page)
    expect(afterMerge, "the merge sources left cell_roi").not.toContain(first)
    expect(afterMerge).not.toContain(second)
    const merged = afterMerge.filter((id) => !before.includes(id))
    expect(merged, "the merge produced one new ROI").toHaveLength(1)

    await selectRoiProjection(page, "non_cell_roi")
    const nonCell = await roiIds(page)
    expect(nonCell, "merge sources are recoverable as non-cell ROIs").toContain(
      first,
    )
    expect(nonCell).toContain(second)

    await editRoiAndCommit(page, "Set as Cell ROI", /promote_roi/, [
      first,
      second,
    ])

    // Promoting alone is not enough to *see* them: every projection is a
    // max-index flatten of the ROI stack, and the merged ROI covers the union
    // of its sources' pixels with a higher index, so it hides them in cell_roi.
    // Un-merging is promote the sources plus delete the merged ROI.
    await selectRoiProjection(page, "cell_roi")
    expect(
      await roiIds(page),
      "the merged ROI still covers its sources",
    ).not.toContain(first)

    await editRoiAndCommit(page, "Delete ROI", /delete_roi/, merged)
    const unmerged = await roiIds(page)
    expect(unmerged, "both sources are cell ROIs again").toContain(first)
    expect(unmerged).toContain(second)
    expect(unmerged, "the merged ROI is gone").not.toContain(merged[0])
  })
  // The un-merge half that has to happen before a commit: merge two ROIs, then
  // delete the merged ROI while it is still pending. Its temp_merge_roi entry
  // used to force it back to a cell at commit, so the delete was silently
  // ignored; now it stays demoted and its sources are promotable.
  test("VIS-09 - deleting a pending merge before commit really removes it @slow", async ({
    page,
  }) => {
    test.setTimeout(60 * 60_000)
    await runTutorial(page, "Tutorial1", "RUN ALL")
    await addImagePlot(page)
    await selectRoiProjection(page, "cell_roi")
    const before = await roiIds(page)
    expect(before.length, "need two cell ROIs to merge").toBeGreaterThan(1)
    const [first, second] = before

    await runRoiAction(page, "Merge ROI", /merge_roi/, [first, second])
    // The merge writes cell_roi.json and refetches it, and that refresh lands
    // after the POST resolves - reading the plot straight away races it.
    const newRoiCount = async () =>
      (await roiIds(page)).filter((id) => !before.includes(id)).length
    await expect.poll(newRoiCount, { timeout: 60_000 }).toBe(1)
    const merged = (await roiIds(page)).filter((id) => !before.includes(id))

    // Same editor session, no commit in between: the merge is still pending.
    await runRoiAction(page, "Delete ROI", /delete_roi/, merged)
    await commitRoiEdit(page)

    // Undoing a pending merge puts its sources straight back: leaving them
    // demoted under the merged ROI would hide them in non_cell_roi too, since
    // every projection is a max-index flatten.
    const afterCommit = await roiIds(page)
    expect(afterCommit, "the deleted merge is not a cell ROI").not.toContain(
      merged[0],
    )
    expect(afterCommit, "the merge sources are cell ROIs again").toContain(
      first,
    )
    expect(afterCommit).toContain(second)

    await selectRoiProjection(page, "non_cell_roi")
    expect(await roiIds(page), "the undone merge is a non-cell ROI").toContain(
      merged[0],
    )
  })
})
