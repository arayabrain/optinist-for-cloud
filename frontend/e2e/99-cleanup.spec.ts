import { test, expect } from "@playwright/test"

import { apiLogin, deleteE2eWorkspaces, skipWithoutCreds } from "./helpers"

// On-demand data cleanup, for when the run's data has been inspected and can
// go. Opt in with RUN_CLEANUP=1 so a normal run never destroys data mid-check;
// global-setup does the same deletion at the start of the next run regardless.
test.describe("Cleanup @cleanup", () => {
  test.skip(!process.env.RUN_CLEANUP, "RUN_CLEANUP not set")

  test("CLEAN-01 - deletes the test account's e2e-* workspaces", async () => {
    skipWithoutCreds()
    const { api, headers } = await apiLogin()
    try {
      const deleted = await deleteE2eWorkspaces(api, headers)
      console.log(`Deleted ${deleted.length}: ${deleted.join(", ") || "none"}`)
      // A second pass lists again: anything left means a DELETE failed
      expect(await deleteE2eWorkspaces(api, headers)).toEqual([])
    } finally {
      await api.dispose()
    }
  })
})
