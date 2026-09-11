import * as fs from "fs"
import * as path from "path"

import { defineConfig } from "@playwright/test"

// Load e2e/.env (KEY=VALUE lines) so credentials never live in the repo.
// E2E_TARGET=prod reads e2e/.env.prod instead, and its values win over anything
// already in the environment: an environment is every variable at once, and a
// leftover BASE_URL or account in the shell is precisely the half-swap that
// points the browser at one environment and the API or the account at another.
// Without a target the file stays the fallback it has always been.
// A target file stands for a whole environment, so an incomplete one is the same
// half-swap by another route: without HEALTH_ENV the log groups, the Stripe
// secret and the scan's --check all resolve to development while BASE_URL says
// otherwise, and each of those surfaces as a timeout rather than as a mistake.
const REQUIRED_ON_TARGET = [
  "BASE_URL",
  "API_URL",
  "HEALTH_ENV",
  "TEST_USER_EMAIL",
  "TEST_USER_PASSWORD",
]

const target = process.env.E2E_TARGET
if (target && !/^[A-Za-z0-9_-]+$/.test(target)) {
  throw new Error(`E2E_TARGET=${target} is not a plain target name`)
}
const envFile = path.join(__dirname, "e2e", target ? `.env.${target}` : ".env")
if (target && !fs.existsSync(envFile)) {
  throw new Error(`E2E_TARGET=${target} but ${envFile} does not exist`)
}
if (fs.existsSync(envFile)) {
  const overridden: string[] = []
  for (const line of fs.readFileSync(envFile, "utf-8").split(/\r?\n/)) {
    const match = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$/)
    if (!match) continue
    if (
      target &&
      match[1] in process.env &&
      process.env[match[1]] !== match[2]
    ) {
      overridden.push(match[1])
    }
    if (target || !(match[1] in process.env)) {
      process.env[match[1]] = match[2]
    }
  }
  // Winning over the shell is the surprising half of the rule, so say which keys
  // it happened to rather than leaving a one-off override silently ignored.
  if (overridden.length) {
    console.log(
      `${path.basename(envFile)} overrode ${overridden.sort().join(", ")}`,
    )
  }
  const missing = target
    ? REQUIRED_ON_TARGET.filter((key) => !process.env[key])
    : []
  if (missing.length) {
    throw new Error(`${envFile} is missing ${missing.join(", ")}`)
  }
}

// One regex from the tags that are switched off, or undefined when none are:
// grepInvert matches every test when handed an empty pattern.
function excluded(tags: [string, boolean][]): RegExp | undefined {
  const off = tags.filter(([, on]) => !on).map(([tag]) => tag)
  return off.length ? new RegExp(off.join("|")) : undefined
}

export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  timeout: 60_000,
  expect: {
    timeout: 10_000,
  },
  fullyParallel: false,
  // Specs share one account, the e2e-data workspace, and one dev server, so
  // they must run serially — parallel workers race and fail spuriously
  workers: 1,
  // CRA dev-server hydration makes early clicks occasionally no-op; one
  // retry absorbs it without hiding persistent failures
  // A @disruptive test mutates the environment, so a retry would take the
  // tier down a second time rather than re-observe it.
  retries: process.env.RUN_DISRUPTIVE ? 0 : process.env.CI ? 2 : 1,
  reporter: [
    ["html", { open: "never" }],
    ["list"],
    // A skipped test reads as a pass in the summary line the sheets are signed
    // off against; this names the rows that did not run.
    ["./e2e/skip-summary-reporter.ts"],
  ],
  // Two opt-in tags, each filtered out unless its variable is set:
  //   @slow        real workflow runs, 5-10 minutes each   RUN_SLOW=1
  //   @disruptive  degrades the shared environment while it runs, so it may
  //                only run when nobody else is using it    RUN_DISRUPTIVE=1
  grepInvert: excluded([
    ["@slow", !!process.env.RUN_SLOW],
    ["@disruptive", !!process.env.RUN_DISRUPTIVE],
  ]),
  // End the run ourselves rather than letting the CI job's timeout kill it: a
  // runner-level kill skips onEnd, so the skip summary and artifacts are lost
  // precisely on the runs where they matter most. Kept 15 minutes under the
  // job's own timeout-minutes so the summary and log-dump steps still run.
  globalTimeout: process.env.CI ? 165 * 60_000 : undefined,
  use: {
    // Without this, an intercepted click retries until the test timeout
    // (Playwright's default action timeout is unlimited)
    actionTimeout: 15_000,
    baseURL: process.env.BASE_URL || "http://localhost:3000",
    // Dates render through toLocaleDateString, so a runner in a negative
    // offset would read a fixture timestamp back a day early
    timezoneId: "UTC",
    screenshot: "only-on-failure",
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
})
