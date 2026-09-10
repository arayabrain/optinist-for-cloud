import * as fs from "fs"
import * as path from "path"

import type {
  FullConfig,
  FullResult,
  Reporter,
  Suite,
  TestCase,
  TestResult,
} from "@playwright/test/reporter"

/**
 * Reports which mapped test IDs did not actually run.
 *
 * On a release sign-off sheet a skipped test is indistinguishable from a passing
 * one: the summary line says "N passed" and the row gets ticked. That is not
 * hypothetical here — around twenty specs are guarded by
 * `test.skip(!(await hasDataRows(page)))` or `skipWithoutCreds()`, so a
 * regression that empties the dataview silently converts eight mapped rows from
 * FAIL to SKIP.
 *
 * Two failure modes this deliberately covers, because both make a bad run read
 * as a clean one:
 *
 * - **Filtered-out tests never reach `onTestEnd`.** `grepInvert: /@slow/` removes
 *   the tutorial runs from the suite entirely, so they are absent rather than
 *   skipped. Counting only skips would report "no tests were skipped" for a run
 *   that never executed them.
 * - **A run where nothing executed at all** (a `globalSetup` failure) also
 *   produces zero skips. The counts printed below make that visible.
 *
 * Set `E2E_FAIL_ON_SKIP=1` to make any skip fail the run, which is what a
 * sign-off run should do once the environmental skips are resolved.
 */
export default class SkipSummaryReporter implements Reporter {
  private skipped: { id: string; title: string; reason: string }[] = []
  private executed = 0
  private outputDir = path.join(__dirname, "..", "test-results")
  private declaredIds = new Set<string>()
  private seenIds = new Set<string>()

  onConfigure(config: FullConfig) {
    // Honour a configured outputDir; the workflow reads the file from wherever
    // Playwright would put its other artifacts.
    const configured = config.projects?.[0]?.outputDir
    if (configured) this.outputDir = configured
  }

  onBegin(_config: FullConfig, suite: Suite) {
    // Everything Playwright will actually run, after grep/grepInvert filtering.
    for (const test of suite.allTests()) {
      const id = mappedId(test.title)
      if (id) this.declaredIds.add(id)
    }
  }

  onTestEnd(test: TestCase, result: TestResult) {
    const id = mappedId(test.title) ?? "(unmapped)"
    if (id !== "(unmapped)") this.seenIds.add(id)

    if (result.status !== "skipped") {
      this.executed += 1
      return
    }

    // `test.fixme(cond, reason)` annotates as "fixme", not "skip".
    const annotation = test.annotations.find(
      (a) => a.type === "skip" || a.type === "fixme",
    )
    this.skipped.push({
      id,
      title: test.title,
      // Pipes would break the markdown table the workflow renders, and a
      // multi-line message would emit a tabless row.
      reason: sanitise(
        annotation?.description ?? result.error?.message ?? "no reason given",
      ),
    })
  }

  async onEnd(result: FullResult) {
    const lines = this.skipped.map(
      ({ id, title, reason }) => `${id}\t${title}\t${reason}`,
    )

    console.log(
      `\nSkip summary: ${this.executed} executed, ${this.skipped.length} skipped, ` +
        `${this.declaredIds.size} mapped ID(s) in the filtered suite.`,
    )

    if (this.skipped.length) {
      console.log(
        "These rows are unverified, not passing, on a sign-off sheet:",
      )
      for (const line of lines) console.log(`  ${line}`)
    }

    if (this.executed === 0) {
      console.error(
        "\nNo test executed. A run with nothing to report is not a clean run.",
      )
    }

    // Enforcement first: the write below can throw (read-only volume, full
    // disk), and Playwright swallows reporter exceptions, which would let a
    // skip pass silently — the exact thing this reporter exists to stop.
    const shouldFail =
      process.env.E2E_FAIL_ON_SKIP === "1" &&
      (this.skipped.length > 0 || this.executed === 0)

    try {
      fs.mkdirSync(this.outputDir, { recursive: true })
      fs.writeFileSync(
        path.join(this.outputDir, "skipped-tests.txt"),
        lines.length ? `${lines.join("\n")}\n` : "",
      )
    } catch (error) {
      console.error(`\nFailed to write the skip summary: ${error}`)
    }

    if (shouldFail) {
      console.error(
        "\nE2E_FAIL_ON_SKIP=1 with skipped or zero executed tests: failing the run.",
      )
      // Returning the status is the documented contract; mutating `result`
      // happens to work today only because the multiplexer passes the same
      // object through.
      result.status = "failed"
      return { status: "failed" as const }
    }
    // Playwright reads a missing status as "no override", which is the
    // non-failing outcome.
    return undefined
  }
}

function mappedId(title: string): string | undefined {
  return title.match(/^([A-Z][A-Z0-9]*-\d+)/)?.[1]
}

function sanitise(reason: string): string {
  return reason.replace(/\s+/g, " ").replace(/\|/g, "\\|").trim()
}
