import { test, expect } from "@playwright/test"

import { isLocalBaseUrl, runCompose } from "./helpers"

// System row 127: on boot the interval jobs fire once shortly after startup
// rather than a full interval later, and that first cleanup run skips the
// orphaned-data sweep so a rolling deploy's instance handoff is not swept away.
//
// Its own file because it restarts the backend container, and a restart
// mid-suite breaks every other local spec sharing the stack. Opt in with
// RUN_RESTART=1.
//
// The assertions read APScheduler's own scheduling fields rather than wall
// clock: "scheduled at" is the fire time the scheduler computed and "next run
// at" the one after it, so the delay and the interval are both exact and a
// slow host cannot make them flake.

const CONTAINER = "studio-dev-be"
const SCHEDULER_STARTED = "Background job scheduler started"
const CLEANUP_STARTED = "Starting data cleanup job"
const WARMUP_SKIP =
  "Skipping orphaned-data sweep on first run (startup warm-up)"
// studio/app/common/core/subscription/constants.py
const INITIAL_RUN_DELAY_SECONDS = 10
const CLEANUP_INTERVAL_MINUTES = 60

// "2026-08-22 01:35:30.969683+00:00" / "2026-08-22 01:35:30" -> epoch ms
function parseUtc(stamp: string): number {
  const [date, time] = stamp.replace(",", ".").split(" ")
  const [hms, frac = "0"] = time.split(".")
  return Date.parse(`${date}T${hms}.${frac.slice(0, 3).padEnd(3, "0")}Z`)
}

function appTs(line: string): number {
  const m = line.match(/(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})/)
  if (!m) throw new Error(`no application timestamp in: ${line}`)
  return parseUtc(m[1])
}

// The container's most recent boot: the lines it logged from the scheduler
// starting onwards, stamped with when that happened. A restart leaves the
// previous boot in the log too, so callers compare stamps to be sure they are
// reading the boot they caused and not the one before it.
function lastBoot(): { at: number; lines: string[] } | null {
  const lines = runCompose(
    `logs --no-log-prefix --tail 4000 ${CONTAINER}`,
  ).split("\n")
  const boot = lines.map((l) => l.includes(SCHEDULER_STARTED)).lastIndexOf(true)
  return boot < 0 ? null : { at: appTs(lines[boot]), lines: lines.slice(boot) }
}

test.describe("Background scheduler boot", () => {
  test.skip(
    !isLocalBaseUrl(),
    "restarts the local backend container; BASE_URL is not local",
  )
  test.skip(
    !process.env.RUN_RESTART,
    "restarts the backend, breaking any concurrent spec; opt in with RUN_RESTART=1",
  )

  test("BOOT-01 - The cleanup job's first run fires seconds after boot and skips the orphan sweep", async () => {
    test.setTimeout(360_000)
    const previous = lastBoot()
    runCompose(`restart ${CONTAINER}`, 180_000)

    let lines: string[] = []
    const deadline = Date.now() + 240_000
    for (;;) {
      const boot = lastBoot()
      // A boot newer than the one before the restart, carrying the line the
      // assertions are about: anything less and this would read a stale boot.
      if (
        boot &&
        (!previous || boot.at > previous.at) &&
        boot.lines.some((l) => l.includes(WARMUP_SKIP))
      ) {
        lines = boot.lines
        break
      }
      if (Date.now() > deadline) {
        throw new Error(
          `${CONTAINER} logged no new boot with a "${WARMUP_SKIP}" line ` +
            `within 240s of being restarted`,
        )
      }
      await new Promise((r) => setTimeout(r, 3000))
    }

    const started = appTs(lines[0])
    const fire = lines.find(
      (l) => l.includes("DataCleanupJob.run") && l.includes("scheduled at"),
    )!
    const scheduledAt = parseUtc(
      fire.match(/scheduled at ([\d-]+ [\d:.]+)\+00:00/)![1],
    )
    const nextRun = parseUtc(fire.match(/next run at: ([\d-]+ [\d:]+) UTC/)![1])

    // Seconds after startup, not the full interval the trigger repeats on.
    expect(
      (scheduledAt - started) / 1000,
      "delay from scheduler start to the first cleanup fire",
    ).toBeCloseTo(INITIAL_RUN_DELAY_SECONDS, 0)
    // "next run at" is logged to whole seconds, so both fires are compared at
    // that granularity and the interval between them is exact.
    expect(
      (nextRun / 1000 - Math.floor(scheduledAt / 1000)) / 60,
      "the fire after it is one whole interval later",
    ).toBe(CLEANUP_INTERVAL_MINUTES)

    // That first run reaches the job body and takes the warm-up branch, once.
    const skips = lines.filter((l) => l.includes(WARMUP_SKIP))
    expect(
      skips,
      "the warm-up sweep is skipped exactly once per boot",
    ).toHaveLength(1)
    const run = lines.findIndex((l) => l.includes(CLEANUP_STARTED))
    expect(
      run,
      `no "${CLEANUP_STARTED}" line after boot`,
    ).toBeGreaterThanOrEqual(0)
    expect(
      lines.indexOf(skips[0]),
      "the skip belongs to the first cleanup run",
    ).toBeGreaterThan(run)
    expect(
      appTs(skips[0]),
      "the skip is logged at the scheduled fire",
    ).toBeGreaterThanOrEqual(scheduledAt)
  })
})
