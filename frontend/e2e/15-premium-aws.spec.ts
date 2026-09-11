import { execSync } from "child_process"

import { test, expect, APIResponse, Page } from "@playwright/test"

import {
  CLOUDWATCH_POLL,
  FREE_LOG_GROUP,
  FREE_USER,
  PREMIUM_LOG_GROUP,
  PREMIUM_USER,
  PUBLIC_LOG_GROUP,
  RUN_TEST_TIMEOUT_MS,
  RUN_TIMEOUT_MS,
  apiHeaders,
  apiLogin,
  apiUrl,
  awaitRunFinished,
  awsJson,
  cloudwatchHas,
  expectPremiumTargetGroupGone,
  importSampleData,
  instanceState,
  invokeMonitoringSweep,
  isLocalBaseUrl,
  login,
  openWorkspace,
  progressLog,
  premiumInstances,
  premiumTargetHealth,
  premiumTaskOn,
  premiumTaskStatusOn,
  reproduceTutorial,
  routedApiHeaders,
  runningPremiumInstanceIds,
  runShellOverSsm,
  runSql,
  runSqlWriteOnDev,
  runTutorial,
  skipUnlessPremiumTargetHealthy,
  s3ObjectCount,
  skipWithoutCreds,
  sqlSkipReason,
  stageSecondRunningInstance,
  startRun,
  windowStart,
} from "./helpers"

// Real-AWS premium assignment (sheet 06-2). Unlike the mocked STO-02/03/09,
// every test here performs a genuine assignment against the deployed dev
// environment: the backend picks a real tier, the premium ECS service really
// scales, and the assertions read the API, the cluster, and the UI state.
// Opt in explicitly - a run costs money and mutates shared dev infra:
//
//   RUN_SLOW=1 RUN_PREMIUM_AWS=1 npx playwright test e2e/15-premium-aws.spec.ts --retries 0
//
// --retries 0 matters: a real-AWS test that "passes on retry" hides real
// flakiness from the sign-off sheet.

const RUN_PREMIUM_AWS = process.env.RUN_PREMIUM_AWS === "1"

// The cascade's transient warming tier, reported by /premium/assign but
// deliberately 404'd by /premium/status.
const AUTOSCALING_POOL = "autoscaling-pool"

const CLUSTER = "development-optinist-cloud-cluster"
const PREMIUM_SERVICE = "development-premium-optinist-cloud-service"
const PREMIUM_MANAGER_LOG_GROUP = "/aws/lambda/development-premium-manager"
const PREMIUM_CLEANUP_LOG_GROUP = "/aws/lambda/development-premium-cleanup"
const REGION = "ap-northeast-1"

// A cold assign starts EC2 capacity + an ECS task: minutes, not seconds
const ASSIGN_TIMEOUT_MS = 15 * 60_000
const TEST_TIMEOUT_MS = ASSIGN_TIMEOUT_MS + 10 * 60_000
// The premium endpoints do real AWS work in-request (ALB rules, scale-up,
// teardown), so the config's 15s actionTimeout aborts them mid-flight -
// every assign/release times out client-side while completing server-side.
// Each call names its own budget instead.
const ASSIGN_REQUEST_TIMEOUT_MS = 300_000
// The assign call blocks for the whole cold standby start, and the assignment
// row becomes visible to /premium/status DURING it - so the row can be asserted
// minutes before the call returns and logs its line. These polls need the
// assign budget, not the generic CloudWatch one.
const ASSIGN_LOG_POLL = { timeout: 8 * 60_000, intervals: [15_000] }
const RELEASE_REQUEST_TIMEOUT_MS = 120_000
const STATUS_REQUEST_TIMEOUT_MS = 30_000

function ecs(query: string): string {
  return execSync(
    `aws ecs describe-services --cluster ${CLUSTER} --region ${REGION} ` +
      `--services ${PREMIUM_SERVICE} --query '${query}' --output text`,
    { timeout: 30_000 },
  )
    .toString()
    .trim()
}

// A dedicated assignment creates a per-user ALB target group with a
// deterministic name; its lifecycle is the real ALB truth the coverage doc
// long marked manual. NotFound (deleted) and any other CLI failure both
// return false, so assert true before ever asserting false.
function tgExists(userId: number): boolean {
  try {
    execSync(
      `aws elbv2 describe-target-groups --names premium-${userId}-tg ` +
        `--region ${REGION}`,
      { timeout: 30_000, stdio: ["pipe", "pipe", "pipe"] },
    )
    return true
  } catch {
    return false
  }
}

// Premium instances not yet at rest: an assign racing a still-stopping
// instance can land on it, so cooling waits for none of these.
function transitioningPremiumInstanceCount(): number {
  return Number(
    execSync(
      `aws ec2 describe-instances --region ${REGION} ` +
        `--filters "Name=tag:Name,Values=development-premium-*" ` +
        `"Name=instance-state-name,Values=pending,running,stopping,shutting-down" ` +
        `--query 'length(Reservations[].Instances[])' --output text`,
      { timeout: 30_000 },
    )
      .toString()
      .trim(),
  )
}

// The cascade grants the shared tier instantly while a warm shared-marked
// instance runs, which starves the dedicated-tier rows whenever an earlier
// test warmed the pool. A shared grant with no DB owner of premium capacity
// is pool temperature, not a capacity limit: release it, park the pool cold
// (desired 0, instances stopped), and let one re-assign take the cold-start
// migrate path to a dedicated grant. A shared grant that survives the cooled
// re-assign, or capacity someone else owns, still skips with its reason.
async function dedicatedAssignmentCoolingIfNeeded(
  page: Page,
  rows: string,
): Promise<NonNullable<PremiumStatus["assignment"]>> {
  let assignment = (await waitForAssignment(page, rows)).assignment!
  if (isDedicated(assignment)) return assignment

  const release = await page.request.delete(
    `${apiUrl()}/users/me/premium/assign`,
    { headers: await apiHeaders(page), timeout: RELEASE_REQUEST_TIMEOUT_MS },
  )
  expect(release.ok(), await release.text()).toBe(true)

  const sqlReason = sqlSkipReason()
  const owners = sqlReason
    ? `unknown (${sqlReason})`
    : runSql(
        "SELECT COUNT(*) FROM premium_user_assignments WHERE is_standby = 0" +
          " AND status IN ('active', 'migrating', 'terminating');",
      ).trim()
  test.skip(
    owners !== "0",
    `rows ${rows}: the cascade granted ${JSON.stringify(assignment)} and the ` +
      `pool cannot be cooled: premium capacity owners=${owners}`,
  )

  const running = runningPremiumInstanceIds()
  if (running.length) {
    execSync(
      `aws ecs update-service --cluster ${CLUSTER} ` +
        `--service ${PREMIUM_SERVICE} --desired-count 0 --region ${REGION}`,
      { timeout: 30_000 },
    )
    execSync(
      `aws ec2 stop-instances --instance-ids ${running.join(" ")} ` +
        `--region ${REGION}`,
      { timeout: 30_000 },
    )
  }
  await expect
    .poll(() => transitioningPremiumInstanceCount(), {
      timeout: 5 * 60_000,
      intervals: [15_000],
      message: `premium instances never finished stopping while cooling for rows ${rows}`,
    })
    .toBe(0)

  // A mount with no assignment re-runs the provider's assign flow
  await page.reload()
  assignment = (await waitForAssignment(page, rows)).assignment!
  test.skip(
    !isDedicated(assignment),
    `rows ${rows}: the cascade did not grant a dedicated instance even from ` +
      `a cold pool (${JSON.stringify(assignment)})`,
  )
  return assignment
}

// A window that provably excludes older matches of the same pattern: when a
// previous test's teardown logged the line within the slack, tighten to now.
function freshWindow(logGroup: string, pattern: string): number {
  const t = windowStart()
  return cloudwatchHas(logGroup, pattern, t) ? Date.now() - 1_000 : t
}

// The autoscaling pool tier reports a sentinel instead of a real EC2 id, so
// "a machine is really ours" is pinned on the id shape, not on is_shared alone
function isDedicated(a: { is_shared?: boolean; instance_id?: string }) {
  return !a.is_shared && !!a.instance_id?.startsWith("i-")
}

// Second premium account, required only by the two-user scale-down test
const PREMIUM2_USER = {
  email: process.env.TEST_PREMIUM2_EMAIL || "",
  password: process.env.TEST_PREMIUM2_PASSWORD || "",
}

function ecsContainerEc2Ids(): string[] {
  const arns = execSync(
    `aws ecs list-container-instances --cluster ${CLUSTER} ` +
      `--region ${REGION} --query 'containerInstanceArns' --output text`,
    { timeout: 30_000 },
  )
    .toString()
    .trim()
  if (!arns) return []
  const out = execSync(
    `aws ecs describe-container-instances --cluster ${CLUSTER} ` +
      `--region ${REGION} --container-instances ${arns.split(/\s+/).join(" ")} ` +
      `--query 'containerInstances[].ec2InstanceId' --output text`,
    { timeout: 30_000 },
  )
    .toString()
    .trim()
  return out ? out.split(/\s+/) : []
}

// The latest matching line since sinceMs, from the manager Lambda's own log
// group; empty string until log propagation delivers it.
function latestManagerLine(pattern: string, sinceMs: number): string {
  const out = execSync(
    `aws logs filter-log-events --log-group-name ${PREMIUM_MANAGER_LOG_GROUP} ` +
      `--start-time ${sinceMs} --filter-pattern '"${pattern}"' ` +
      `--query 'events[-1].message' --output text --region ${REGION}`,
    { timeout: 30_000, stdio: ["pipe", "pipe", "pipe"] },
  )
    .toString()
    .trim()
  return out === "None" ? "" : out
}

function skipUnlessOptedIn(
  rows: string,
  user = PREMIUM_USER,
  name = "TEST_PREMIUM_EMAIL/TEST_PREMIUM_PASSWORD",
) {
  skipWithoutCreds(user, name)
  test.skip(
    !RUN_PREMIUM_AWS,
    `rows ${rows}: set RUN_PREMIUM_AWS=1 - assigns a real premium instance and scales real ECS`,
  )
  test.skip(
    isLocalBaseUrl(),
    `rows ${rows}: needs the deployed dev environment; BASE_URL is local`,
  )
  // The AWS side (ECS, Lambda, EC2 stops in the cooling path) is hardcoded to
  // development, so a non-development BASE_URL would mutate one environment's
  // accounts while cooling another's infrastructure.
  expect(
    process.env.BASE_URL || "",
    "this lane only runs against the development environment",
  ).toContain("development-optinist")
  // A pass on retry hides real-AWS flakiness from the sign-off sheet
  expect(test.info().project.retries, "run this lane with --retries 0").toBe(0)
}

type Assignment = {
  instance_id?: string
  instance_id_hash?: string
  is_shared?: boolean
  assigned_at?: string
} | null

type PremiumStatus = {
  subscription_type: string
  is_premium: boolean
  assignment: Assignment
}

async function statusViaPage(page: Page): Promise<PremiumStatus> {
  const headers = await apiHeaders(page)
  const res = await page.request.get(`${apiUrl()}/users/me/premium/status`, {
    headers,
    timeout: STATUS_REQUEST_TIMEOUT_MS,
  })
  expect(res.ok(), await res.text()).toBe(true)
  return res.json()
}

// The app rewrites premium_shared from its own /premium/status poll, whose
// interval is 30s, and the cascade can migrate a pool assignment to dedicated
// mid-test - so compare the stored tier against a live status read rather than
// an earlier snapshot, over a window wider than that poll.
async function expectStoredTierMatchesStatus(page: Page) {
  await expect
    .poll(
      async () => {
        const shared = (await statusViaPage(page)).assignment?.is_shared
        if (typeof shared !== "boolean") return "no assignment on /status"
        const stored = await page.evaluate(() =>
          localStorage.getItem("premium_shared"),
        )
        return stored === String(shared)
          ? "match"
          : `premium_shared=${stored}, status is_shared=${shared}`
      },
      { timeout: 90_000, intervals: [5_000] },
    )
    .toBe("match")
}

// The sheet's standby/autoscaling rows call a cluster with no free capacity a
// legitimate outcome ("insufficient CPU units available"). It leaves the rows
// unverified, not failed - skip with a reason the skip-summary reporter can
// put on the sign-off sheet.
function skipForNoCapacity(rows: string, detail: unknown): never {
  test.skip(
    true,
    `rows ${rows}: the cluster could not place premium capacity within ` +
      `${ASSIGN_TIMEOUT_MS / 60_000} min (${JSON.stringify(detail)}) - ` +
      `rerun when the dev cluster has free CPU`,
  )
  throw new Error("unreachable")
}

// Poll /status until the provider's own assign flow lands an assignment.
// If it never does, one direct assign probe tells apart "still scaling with
// no capacity" (skip) from a dead assign flow (fail).
async function waitForAssignment(
  page: Page,
  rows: string,
): Promise<PremiumStatus> {
  const deadline = Date.now() + ASSIGN_TIMEOUT_MS
  for (;;) {
    const status = await statusViaPage(page)
    if (status.assignment) return status
    if (Date.now() > deadline) {
      const headers = await apiHeaders(page)
      const probe = await page.request.post(
        `${apiUrl()}/users/me/premium/assign`,
        { headers, timeout: ASSIGN_REQUEST_TIMEOUT_MS },
      )
      const body = await probe.json().catch(() => ({}))
      if (body?.scaling_in_progress) skipForNoCapacity(rows, body)
      throw new Error(
        `no assignment appeared and the backend is not scaling: ` +
          `${probe.status()} ${JSON.stringify(body)}`,
      )
    }
    await new Promise((r) => setTimeout(r, 15_000))
  }
}

// The account trap this suite has been bitten by before: an account can say
// "Premium" on /users/me while /premium/status says free (billing grace).
// Every test verifies the slot against /premium/status before relying on it.
function expectGenuinelyPremium(status: PremiumStatus) {
  expect(
    status.is_premium,
    `${PREMIUM_USER.email} is not premium on /premium/status - ` +
      `verify the TEST_PREMIUM_* account against /premium/status, not /users/me`,
  ).toBe(true)
}

// The assign endpoint answers scaling_in_progress while capacity starts and
// expects the caller to retry. Retry every 30s rather than the advertised
// 180s so a warm assign settles in one round and a cold one within minutes.
async function assignUntilSettled(
  post: () => Promise<APIResponse>,
  rows: string,
) {
  const deadline = Date.now() + ASSIGN_TIMEOUT_MS
  for (;;) {
    const res = await post()
    expect(res.status(), await res.text()).toBeLessThan(500)
    const body = await res.json()
    if (body.assigned && body.instance_id !== AUTOSCALING_POOL) {
      return body as {
        assigned: true
        is_shared: boolean
        instance_id?: string
      }
    }
    // Either still scaling, or parked on the pool tier - which /premium/status
    // deliberately 404s so the client keeps assigning until a real instance is
    // free. Settling here would assert against a tier status never reports.
    expect(
      body.scaling_in_progress || body.instance_id === AUTOSCALING_POOL,
      JSON.stringify(body),
    ).toBeTruthy()
    if (Date.now() > deadline) skipForNoCapacity(rows, body)
    await new Promise((r) => setTimeout(r, 30_000))
  }
}

// Teardown here is deliberately partial, so a "leave no trace" instinct does
// not fight the pool: desiredCount is pool state, which afterAll reports rather
// than forces back, and is_shared lives on the assignment row that afterEach
// hard-releases. Writes that touch premium_user_assignments must carry
// `user_id IS NULL`: the rows this lane may remove are unowned pool
// bookkeeping, never a real user's assignment.

// The premium service normally idles at desired=0; a non-zero baseline means
// another user already holds capacity, and the scale-back-to-zero assertion
// in afterAll would then blame this run for their assignment.
let baselineDesired = -1
// Only a granted non-shared assignment obliges release to scale back down.
// An assign stuck on placement failure also bumps desiredCount, but nothing
// owns that capacity and only the reconciliation sweep (row 6226) clears it.
let heldDedicated = false

test.beforeAll(() => {
  if (!RUN_PREMIUM_AWS || !PREMIUM_USER.email || isLocalBaseUrl()) return
  baselineDesired = Number(ecs("services[0].desiredCount"))
})

function premiumAccounts() {
  return [PREMIUM_USER, PREMIUM2_USER].filter((u) => u.email && u.password)
}

// Hard-release after every test, pass or fail: a stuck assignment degrades
// the shared dev environment and keeps billing for the capacity.
test.afterEach(async () => {
  if (!RUN_PREMIUM_AWS || !PREMIUM_USER.email || isLocalBaseUrl()) return
  for (const user of premiumAccounts()) {
    const { api, headers } = await apiLogin(user.email, user.password)
    try {
      const res = await api.delete("/users/me/premium/assign", {
        headers,
        timeout: RELEASE_REQUEST_TIMEOUT_MS,
      })
      expect(res.ok(), await res.text()).toBe(true)
    } finally {
      await api.dispose()
    }
  }
})

// Asserted, not assumed: after the lane nothing may still be held BY US - no
// assignment row and no per-user ALB resources. The ECS desiredCount is NOT
// part of that invariant: the monitoring Lambda re-targets it to match the
// running standby-pool instances our assigns warmed up (desired returns to 2
// shortly after every release), and idle-pool scale-down is that Lambda's own
// 6221/6222 logic on its own schedule.
test.afterAll(async () => {
  if (!RUN_PREMIUM_AWS || !PREMIUM_USER.email || isLocalBaseUrl()) return
  test.setTimeout(5 * 60_000)
  for (const user of premiumAccounts()) {
    const { api, headers } = await apiLogin(user.email, user.password)
    try {
      const res = await api.get("/users/me/premium/status", {
        headers,
        timeout: STATUS_REQUEST_TIMEOUT_MS,
      })
      expect(res.ok(), await res.text()).toBe(true)
      const { assignment } = await res.json()
      expect(
        assignment ?? null,
        `the lane finished with ${user.email} still assigned`,
      ).toBeNull()
      // The ALB half of the invariant: a hard release can delete the row and
      // rule but fail the TG deletion, stranding a rule-less TG no sweep can
      // find (issue #814) - so only a genuine NotFound counts as absence.
      const me = await api.get("/users/me", {
        headers,
        timeout: STATUS_REQUEST_TIMEOUT_MS,
      })
      const userId: number = (await me.json()).id
      expectPremiumTargetGroupGone(userId, "the lane finished with")
    } finally {
      await api.dispose()
    }
  }
  console.log(
    `[15-premium-aws] premium service after the lane: baseline desired=` +
      `${baselineDesired}, heldDedicated=${heldDedicated}, now ` +
      `desired/running=${ecs("services[0].[desiredCount,runningCount]")}`,
  )
})

test("PREM-01 - Premium login assigns a real tier and dedicated capacity really runs @slow", async ({
  page,
}) => {
  const rows = "6205 / 6206 / 6207 / 415 / BT-1109"
  skipUnlessOptedIn(rows)
  test.setTimeout(TEST_TIMEOUT_MS)

  // No mocks: the login triggers the provider's real assign flow, and the
  // backend's cascade picks whichever tier the live cluster can offer.
  const t0 = windowStart()
  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)
  expectGenuinelyPremium(await statusViaPage(page))
  const status = await waitForAssignment(page, rows)

  // API truth: the assignment row exists and names its tier
  const assignment = status.assignment!
  expect(typeof assignment.is_shared).toBe("boolean")
  expect(assignment.assigned_at).toBeTruthy()

  // AWS truth: a non-shared tier is backed by real capacity in the cluster
  if (!assignment.is_shared) {
    heldDedicated = true
    expect(assignment.instance_id).toBeTruthy()
    expect(Number(ecs("services[0].runningCount"))).toBeGreaterThan(0)
  }

  // UI truth: the frontend recorded the tier the backend really returned
  await expectStoredTierMatchesStatus(page)

  // CloudWatch truth: the assignment left its lines in the public tier's log
  // group (the assign endpoint always answers pre-routing), and the login-time
  // limit-warning calculation logged on the tier that served the login
  const me = await page.request.get(`${apiUrl()}/users/me`, {
    headers: await apiHeaders(page),
    timeout: STATUS_REQUEST_TIMEOUT_MS,
  })
  const userId: number = (await me.json()).id
  // Two racers can complete a cold-start assignment: a client /assign call
  // that returns assigned=True logs in the public group, but when the manager
  // Lambda's sweep migrates the user off the warming pool before any client
  // call completes, no service-side line is ever emitted and the only
  // CloudWatch evidence is the Lambda's own migration line (seen both ways).
  const sweepMigrated = () =>
    cloudwatchHas(
      PREMIUM_MANAGER_LOG_GROUP,
      `Migrated user ${userId} from autoscaling-pool`,
      t0,
    )
  await expect
    .poll(
      () =>
        cloudwatchHas(
          PUBLIC_LOG_GROUP,
          `[premium-assign] user=${userId} assigned=True`,
          t0,
        ) || sweepMigrated(),
      {
        ...ASSIGN_LOG_POLL,
        message: `no [premium-assign] success line for user ${userId} in ${PUBLIC_LOG_GROUP} and no migration line in ${PREMIUM_MANAGER_LOG_GROUP}`,
      },
    )
    .toBe(true)
  await expect
    .poll(
      () =>
        cloudwatchHas(
          PUBLIC_LOG_GROUP,
          `Successfully assigned premium user ${userId}`,
          t0,
        ) || sweepMigrated(),
      {
        ...ASSIGN_LOG_POLL,
        message: `no service-side assign line for user ${userId} in ${PUBLIC_LOG_GROUP} and no migration line in ${PREMIUM_MANAGER_LOG_GROUP}`,
      },
    )
    .toBe(true)
  // /auth/login forwards to the public tier (ALB rule p305), so the login's
  // calculate_limit_warning lines land in the public group
  await expect
    .poll(
      () =>
        cloudwatchHas(
          PUBLIC_LOG_GROUP,
          `Calculating limit warning for user ${userId}`,
          t0,
        ),
      {
        ...CLOUDWATCH_POLL,
        message: `no limit-warning lines for premium user ${userId} in ${PUBLIC_LOG_GROUP}`,
      },
    )
    .toBe(true)
})

test("PREM-02 - Assign, release, and reassign round-trip the real backend @slow", async () => {
  skipUnlessOptedIn("6201 / 6203 / 6231 / BT-614 / BT-1109")
  test.setTimeout(TEST_TIMEOUT_MS * 2)

  // API-driven on purpose: PREM-01 already exercises the UI login half, and
  // a live provider in a page would race these explicit release/assign calls
  // with its own polling and auto-reassign.
  const { api, headers } = await apiLogin(
    PREMIUM_USER.email,
    PREMIUM_USER.password,
  )
  try {
    const rows = "6201 / 6203 / 6231 / BT-614 / BT-1109"
    const post = () =>
      api.post("/users/me/premium/assign", {
        headers,
        timeout: ASSIGN_REQUEST_TIMEOUT_MS,
      })
    const release = () =>
      api.delete("/users/me/premium/assign", {
        headers,
        timeout: RELEASE_REQUEST_TIMEOUT_MS,
      })
    const getStatus = async (): Promise<PremiumStatus> => {
      const res = await api.get("/users/me/premium/status", {
        headers,
        timeout: STATUS_REQUEST_TIMEOUT_MS,
      })
      expect(res.ok(), await res.text()).toBe(true)
      return res.json()
    }

    const first = await assignUntilSettled(post, rows)
    if (!first.is_shared) heldDedicated = true
    let status = await getStatus()
    expectGenuinelyPremium(status)
    expect(status.assignment).toBeTruthy()
    expect(status.assignment!.is_shared).toBe(first.is_shared)
    const firstAssignedAt = status.assignment!.assigned_at

    const me = await api.get("/users/me", {
      headers,
      timeout: STATUS_REQUEST_TIMEOUT_MS,
    })
    const userId: number = (await me.json()).id
    const dedicated = isDedicated(first)
    // ALB truth: a dedicated assignment is backed by its per-user target group
    if (dedicated) {
      expect(
        tgExists(userId),
        `premium-${userId}-tg missing while dedicated-assigned`,
      ).toBe(true)
    }

    // Row 1203: the grant is backed by a premium ECS task really RUNNING on the
    // instance it named, within the sheet's 8 minutes. A warm pool means the
    // task predates the assignment rather than being created by it, which is
    // why the assertion is on the task's state and not on its age.
    const grantedInstance = status.assignment!.instance_id
    if (grantedInstance?.startsWith("i-")) {
      await expect
        .poll(() => premiumTaskStatusOn(grantedInstance), {
          timeout: 480_000,
          intervals: [15_000],
          message:
            `no RUNNING premium task on ${grantedInstance} within 8 minutes ` +
            `of the assignment`,
        })
        .toBe("RUNNING")
    }

    // Hard release (the logout path): the row must be gone immediately,
    // not soft-released into the 120s grace. freshWindow, not windowStart:
    // the previous test's afterEach hard-released this same user, and its
    // line must not satisfy this assert.
    const tRelease = freshWindow(
      PUBLIC_LOG_GROUP,
      `Released premium user ${userId}`,
    )
    const tReleasing = freshWindow(
      PUBLIC_LOG_GROUP,
      `Releasing (hard) premium user ${userId}`,
    )
    const released = await release()
    expect(released.ok(), await released.text()).toBe(true)
    expect((await released.json()).released).toBe(true)
    status = await getStatus()
    expect(status.assignment ?? null).toBeNull()
    // CloudWatch truth (BT-614's log half): the release left its line.
    // The source line is "Released premium user {id} from instance ..." -
    // the sheet's "Releasing premium user" wording never appears verbatim
    // (the actual prefix is "Releasing (hard) premium user").
    await expect
      .poll(
        () =>
          cloudwatchHas(
            PUBLIC_LOG_GROUP,
            `Released premium user ${userId}`,
            tRelease,
          ),
        {
          ...CLOUDWATCH_POLL,
          message: `no release line for user ${userId} in ${PUBLIC_LOG_GROUP}`,
        },
      )
      .toBe(true)
    // Row 1207 wants both halves: the request going out as well as the instance
    // coming back. This is the "Releasing (hard) premium user {id} (uid: ...)
    // from assigned instance" line that precedes it.
    await expect
      .poll(
        () =>
          cloudwatchHas(
            PUBLIC_LOG_GROUP,
            `Releasing (hard) premium user ${userId}`,
            tReleasing,
          ),
        {
          ...CLOUDWATCH_POLL,
          message: `no "Releasing (hard)" line for user ${userId} in ${PUBLIC_LOG_GROUP}`,
        },
      )
      .toBe(true)
    // ... and the ALB teardown must be real, not just the DB row
    if (dedicated) {
      await expect
        .poll(() => tgExists(userId), {
          timeout: 60_000,
          message: `premium-${userId}-tg survived the hard release`,
        })
        .toBe(false)
    }

    // Reassign lands a fresh assignment rather than resurrecting the old row
    const second = await assignUntilSettled(post, rows)
    if (!second.is_shared) heldDedicated = true
    expect(typeof second.is_shared).toBe("boolean")
    status = await getStatus()
    expect(status.assignment).toBeTruthy()
    expect(
      status.assignment!.assigned_at,
      "the reassign resurrected the released row instead of creating a fresh one",
    ).not.toBe(firstAssignedAt)

    const releasedAgain = await release()
    expect(releasedAgain.ok(), await releasedAgain.text()).toBe(true)
    status = await getStatus()
    expect(status.assignment ?? null).toBeNull()
  } finally {
    await api.dispose()
  }
})

test("PREM-03 - A page refresh adopts the real assignment without re-assigning @slow", async ({
  page,
}) => {
  skipUnlessOptedIn("6202 / 6212 / 6213")
  test.setTimeout(TEST_TIMEOUT_MS)

  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)
  expectGenuinelyPremium(await statusViaPage(page))
  const status = await waitForAssignment(page, "6202 / 6212 / 6213")
  const before = status.assignment!
  if (!before.is_shared) heldDedicated = true

  // Any write to /premium/assign after the reload is a re-assign or a
  // release; adoption must issue neither
  const assignWrites: string[] = []
  page.on("request", (req) => {
    if (req.url().includes("/premium/assign") && req.method() !== "GET") {
      assignWrites.push(`${req.method()} ${req.url()}`)
    }
  })

  const statusPolled = page.waitForResponse((r) =>
    r.url().includes("/premium/status"),
  )
  await page.reload()
  await statusPolled
  // Give a wrongly re-triggered assign the chance to fire before ruling it out
  await page.waitForTimeout(10_000)
  expect(assignWrites, assignWrites.join(", ")).toHaveLength(0)

  // DB truth: the very same row survived the reload - identity, tier and
  // timestamp unchanged, whichever tier the cluster really assigned
  const after = (await statusViaPage(page)).assignment!
  expect(after).toBeTruthy()
  expect(after.instance_id).toBe(before.instance_id)
  expect(after.assigned_at).toBe(before.assigned_at)
  expect(after.is_shared).toBe(before.is_shared)

  await expectStoredTierMatchesStatus(page)
})

test("PREM-04 - A browser-close beacon soft-releases; reopening inside the grace restores the same row @slow", async () => {
  const rows = "6208 / BT-615 / 603"
  skipUnlessOptedIn(rows)
  test.setTimeout(TEST_TIMEOUT_MS)

  const { api, headers } = await apiLogin(
    PREMIUM_USER.email,
    PREMIUM_USER.password,
  )
  try {
    const post = () =>
      api.post("/users/me/premium/assign", {
        headers,
        timeout: ASSIGN_REQUEST_TIMEOUT_MS,
      })
    const first = await assignUntilSettled(post, rows)
    if (!first.is_shared) heldDedicated = true

    const me = await api.get("/users/me", {
      headers,
      timeout: STATUS_REQUEST_TIMEOUT_MS,
    })
    const userId: number = (await me.json()).id
    const statusRes = await api.get("/users/me/premium/status", {
      headers,
      timeout: STATUS_REQUEST_TIMEOUT_MS,
    })
    const before = (await statusRes.json()).assignment
    expect(before).toBeTruthy()
    const dedicated = isDedicated(before)

    // The sheet's middleware line, asserted causally and BEFORE the beacon:
    // the update is throttled to once per user per minute in a per-worker
    // cache, and the beacon's logged-out mark suppresses it entirely, so wait
    // the throttle out and then let one request produce the line.
    await new Promise((r) => setTimeout(r, 61_000))
    const tActivity = Date.now() - 5_000
    const probe = await api.get("/users/me", {
      headers,
      timeout: STATUS_REQUEST_TIMEOUT_MS,
    })
    expect(probe.ok(), await probe.text()).toBe(true)
    await expect
      .poll(
        () =>
          cloudwatchHas(
            PUBLIC_LOG_GROUP,
            `Updated premium activity for user ${userId}`,
            tActivity,
          ),
        {
          ...CLOUDWATCH_POLL,
          message: `no middleware premium-activity line for user ${userId} in ${PUBLIC_LOG_GROUP} after an unthrottled request`,
        },
      )
      .toBe(true)

    // The beforeunload path: token minted while authed, then the beacon body
    // is the only credential (sendBeacon cannot carry Authorization headers)
    const t0 = windowStart()
    const tokenRes = await api.get("/users/me/premium/beacon-token", {
      headers,
      timeout: STATUS_REQUEST_TIMEOUT_MS,
    })
    const { token } = await tokenRes.json()
    expect(token).toBeTruthy()
    const beacon = await api.post("/users/me/premium/release-beacon", {
      data: { token },
      timeout: RELEASE_REQUEST_TIMEOUT_MS,
    })
    expect(beacon.ok(), await beacon.text()).toBe(true)
    expect((await beacon.json()).success, await beacon.text()).toBe(true)

    // Soft, not hard: the per-user ALB resources must survive into the grace
    if (dedicated) {
      expect(
        tgExists(userId),
        `premium-${userId}-tg was torn down by a SOFT release`,
      ).toBe(true)
    }

    // Reopening inside the 120s grace: the next status check restores the
    // very same row - identical assigned_at and instance, never a re-assign
    const afterRes = await api.get("/users/me/premium/status", {
      headers,
      timeout: STATUS_REQUEST_TIMEOUT_MS,
    })
    const after = (await afterRes.json()).assignment
    expect(after, "the grace restore returned no assignment").toBeTruthy()
    expect(after.assigned_at).toBe(before.assigned_at)
    expect(after.instance_id).toBe(before.instance_id)
    expect(after.is_shared).toBe(before.is_shared)

    // CloudWatch truth (BT-615's log half): the beacon always lands on the
    // public tier - sendBeacon cannot carry routing headers
    await expect
      .poll(
        () =>
          cloudwatchHas(
            PUBLIC_LOG_GROUP,
            `[premium-trace] beacon-released user=${userId}`,
            t0,
          ),
        {
          ...CLOUDWATCH_POLL,
          message: `no beacon-released trace for user ${userId} in ${PUBLIC_LOG_GROUP}`,
        },
      )
      .toBe(true)

    // Row 603's CloudWatch half. The beacon marked the user logged out and
    // the activity middleware ignores that user for 10s, so wait it out;
    // then prove the heartbeat itself worked - its own response plus the
    // service line, in a window that excludes everything earlier
    await new Promise((r) => setTimeout(r, 11_000))
    const tHeartbeat = Date.now() - 5_000
    const heartbeat = await api.post("/users/me/premium/heartbeat", {
      headers,
      timeout: STATUS_REQUEST_TIMEOUT_MS,
    })
    expect(heartbeat.ok(), await heartbeat.text()).toBe(true)
    expect(
      (await heartbeat.json()).updated,
      "the heartbeat did not update activity",
    ).toBe(true)
    await expect
      .poll(
        () =>
          cloudwatchHas(
            PUBLIC_LOG_GROUP,
            `Successfully updated activity for premium user ${userId}`,
            tHeartbeat,
          ),
        {
          ...CLOUDWATCH_POLL,
          message: `no activity-update line for user ${userId} in ${PUBLIC_LOG_GROUP} after the heartbeat`,
        },
      )
      .toBe(true)
  } finally {
    await api.dispose()
  }
})

test("PREM-05 - Free-tier requests carry no premium routing headers @slow", async ({
  page,
}) => {
  skipUnlessOptedIn(
    "6238 / 402",
    FREE_USER,
    "TEST_USER_EMAIL/TEST_USER_PASSWORD",
  )
  test.setTimeout(10 * 60_000)

  // The real bundle's interceptor, not the jsdom one: capture every request
  // the app itself sends to the API and assert the omission on the wire
  const apiRequests: { url: string; headers: Record<string, string> }[] = []
  page.on("request", (req) => {
    if (req.url().startsWith(apiUrl())) {
      apiRequests.push({ url: req.url(), headers: req.headers() })
    }
  })

  const t0 = windowStart()
  await login(page, FREE_USER.email, FREE_USER.password)
  // On a deployed env apiUrl() can equal the page origin, so static assets
  // land in the capture too; the vacuity guard must count real API calls
  const isApiCall = (url: string) =>
    /\/(users\/me|workspaces|auth\/|storage-limit-alerts)/.test(url)
  await expect
    .poll(() => apiRequests.filter((r) => isApiCall(r.url)).length, {
      timeout: 60_000,
      message: "the app issued no API requests after a free login",
    })
    .toBeGreaterThanOrEqual(3)

  const offenders = apiRequests.filter(
    (r) => "x-routing-id" in r.headers || "x-user-tier" in r.headers,
  )
  expect(
    offenders.map((r) => r.url),
    "free-tier requests carried premium routing headers",
  ).toHaveLength(0)

  // Row 402's CloudWatch half: the login-time limit-warning calculation
  // logged its free-plan branch, and the line is logger.debug - it appearing
  // at all also proves DEBUG-level lines reach CloudWatch. /auth/login
  // forwards to the public tier (ALB rule p305), so the line is public-group.
  const me = await page.request.get(`${apiUrl()}/users/me`, {
    headers: await apiHeaders(page),
    timeout: STATUS_REQUEST_TIMEOUT_MS,
  })
  const userId: number = (await me.json()).id
  await expect
    .poll(
      () =>
        cloudwatchHas(
          PUBLIC_LOG_GROUP,
          `User ${userId}: No warning needed (free plan, within limits)`,
          t0,
        ),
      {
        ...CLOUDWATCH_POLL,
        message: `no free-plan limit-warning debug line for user ${userId} in ${PUBLIC_LOG_GROUP}`,
      },
    )
    .toBe(true)
})

test("PREM-07 - Premium workflow runs end-to-end on the real dedicated instance @slow", async ({
  page,
}) => {
  const rows = "604 / BT-607 / BT-608 / BT-609"
  skipUnlessOptedIn(rows)
  test.setTimeout(TEST_TIMEOUT_MS + RUN_TEST_TIMEOUT_MS)

  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)
  expectGenuinelyPremium(await statusViaPage(page))
  // The flagship 604 row is about the dedicated instance specifically: cool
  // the pool for a dedicated grant when a warm shared one stands in the way
  const assignment = await dedicatedAssignmentCoolingIfNeeded(page, rows)
  heldDedicated = true

  // Routing truth: every workflow-run POST must go out through the premium
  // routing the assignment established
  const apiRequests: {
    method: string
    url: string
    headers: Record<string, string>
  }[] = []
  page.on("request", (req) => {
    if (req.url().startsWith(apiUrl())) {
      apiRequests.push({
        method: req.method(),
        url: req.url(),
        headers: req.headers(),
      })
    }
  })

  // 604's premise is a dedicated instance that really serves: gate on its
  // target group being healthy before driving any workflow through the ALB.
  const idRes = await page.request.get(`${apiUrl()}/users/me`, {
    headers: await apiHeaders(page),
    timeout: STATUS_REQUEST_TIMEOUT_MS,
  })
  expect(idRes.ok(), await idRes.text()).toBe(true)
  const premiumUserId = (await idRes.json()).id
  await skipUnlessPremiumTargetHealthy(rows, premiumUserId)

  // Row 540: the fresh assignment's own baseline in the PREMIUM table
  const countSql =
    `SELECT active_workflow_count FROM premium_user_assignments ` +
    `WHERE user_id = ${premiumUserId};`
  expect(runSql(countSql), "row 540: fresh-assignment baseline").toBe("0")

  const wsId = await openWorkspace(page, "e2e-prem")
  try {
    await importSampleData(page, "e2e-prem")

    // RUN ALL, not RUN: a by-uid rerun of imported tutorials is a snakemake
    // no-op, so only a fresh uid proves the dedicated instance really
    // computed and wrote the outputs this test asserts on
    const t0 = windowStart()
    await reproduceTutorial(page, "Tutorial1")
    const { workspaceId: runWs, uid } = await startRun(page, "RUN ALL")

    // Rows 542/543's live half: the run really holds a slot in the premium
    // table while it executes, and releases it when it completes
    await expect
      .poll(() => runSql(countSql), {
        timeout: 180_000,
        intervals: [10_000],
        message: "active_workflow_count never reached 1 during the run",
      })
      .toBe("1")
    await awaitRunFinished(page, "Tutorial1", runWs, uid)
    await expect
      .poll(() => runSql(countSql), {
        timeout: 120_000,
        intervals: [10_000],
        message: "active_workflow_count did not return to 0 after the run",
      })
      .toBe("0")

    const runPosts = apiRequests.filter(
      (r) => r.method === "POST" && r.url.includes(`/run/${wsId}`),
    )
    expect(runPosts.length, "no run POST was captured").toBeGreaterThan(0)
    for (const r of runPosts) {
      expect(
        r.headers["x-routing-id"],
        `${r.url} missing x-routing-id`,
      ).toBeTruthy()
      expect(
        r.headers["x-user-tier"],
        `${r.url} did not carry x-user-tier: premium`,
      ).toBe("premium")
    }

    // CloudWatch truth (604's Expected #7): the run's WORKFLOW START logged
    // in the premium task's group and NOT in the free tier's
    await expect
      .poll(() => cloudwatchHas(PREMIUM_LOG_GROUP, `(ID: ${uid},`, t0), {
        ...CLOUDWATCH_POLL,
        message: `no WORKFLOW START for run ${uid} in ${PREMIUM_LOG_GROUP}`,
      })
      .toBe(true)
    // The free-group negative is only meaningful once free-group delivery is
    // proven caught up (its awslogs non-blocking buffer is independent of the
    // premium group's): fire one request that lands on the free tier and wait
    // for its own line first
    const plainHeaders = await apiHeaders(page)
    const meRes = await page.request.get(`${apiUrl()}/users/me`, {
      headers: plainHeaders,
      timeout: STATUS_REQUEST_TIMEOUT_MS,
    })
    expect(meRes.ok(), await meRes.text()).toBe(true)
    const meBody = await meRes.json()
    const probe = await page.request.get(
      `${apiUrl()}/storage-limit-alerts/limit-warning`,
      { headers: plainHeaders, timeout: STATUS_REQUEST_TIMEOUT_MS },
    )
    expect(probe.ok(), await probe.text()).toBe(true)
    await expect
      .poll(
        () =>
          cloudwatchHas(
            FREE_LOG_GROUP,
            `Calculating limit warning for user ${meBody.id}`,
            t0,
          ),
        {
          ...CLOUDWATCH_POLL,
          message: `the free-group delivery probe never appeared in ${FREE_LOG_GROUP}`,
        },
      )
      .toBe(true)
    expect(
      cloudwatchHas(FREE_LOG_GROUP, `Workspace: ${wsId})`, t0),
      `workflow lines for workspace ${wsId} leaked into ${FREE_LOG_GROUP}`,
    ).toBe(false)

    // S3 truth (604's Expected #6): the outputs landed in the user's own
    // bucket. A null attribute must fail loudly - the backend silently falls
    // back to the default bucket, which would make this assert vacuous.
    const bucket = meBody.attributes?.remote_bucket_name
    expect(
      bucket,
      "premium user has no remote_bucket_name attribute",
    ).toBeTruthy()
    const outputPrefix = `app/studio_data/output/${wsId}/${uid}/`
    await expect
      .poll(() => s3ObjectCount(bucket, outputPrefix), {
        timeout: 120_000,
        intervals: [15_000],
        message: `no run outputs under s3://${bucket}/${outputPrefix}`,
      })
      .toBeGreaterThan(0)
  } finally {
    // Deleting the workspace drops its S3 input+output prefixes server-side;
    // the afterEach hard-release returns the instance as usual
    const res = await page.request.delete(`${apiUrl()}/workspace/${wsId}`, {
      headers: await apiHeaders(page),
      timeout: RELEASE_REQUEST_TIMEOUT_MS,
    })
    expect(res.ok(), await res.text()).toBe(true)
  }
})

test("PREM-08 - Concurrent workflows all complete on one dedicated instance @slow", async ({
  page,
}) => {
  const rows = "605 / BT-610"
  skipUnlessOptedIn(rows)
  test.setTimeout(TEST_TIMEOUT_MS + RUN_TEST_TIMEOUT_MS + 10 * 60_000)

  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)
  expectGenuinelyPremium(await statusViaPage(page))
  // 605 needs the dedicated tier: cool the pool when a warm shared grant
  // stands in the way
  const assignment = await dedicatedAssignmentCoolingIfNeeded(page, rows)
  heldDedicated = true

  // 605's premise is the same dedicated instance serving three runs: gate on its
  // target group being healthy before driving any workflow through the ALB.
  const idRes = await page.request.get(`${apiUrl()}/users/me`, {
    headers: await apiHeaders(page),
    timeout: STATUS_REQUEST_TIMEOUT_MS,
  })
  expect(idRes.ok(), await idRes.text()).toBe(true)
  await skipUnlessPremiumTargetHealthy(rows, (await idRes.json()).id)

  const names = ["e2e-conc-a", "e2e-conc-b", "e2e-conc-c"]
  const wsIds: number[] = []
  for (const name of names) {
    wsIds.push(await openWorkspace(page, name))
    await importSampleData(page, name)
  }

  const pages = [page]
  try {
    while (pages.length < names.length) {
      pages.push(await page.context().newPage())
    }
    for (const [i, p] of pages.entries()) {
      await p.goto(`/workspaces/${wsIds[i]}`)
      await expect(
        p.locator('button[role="tab"]:has-text("Workflow")'),
      ).toBeVisible({ timeout: 15_000 })
      await reproduceTutorial(p, "Tutorial1")
    }

    // The row's point: fire every run as close to simultaneously as possible,
    // then every one must complete on the single dedicated instance - no
    // queuing, no timeout, no hung page
    const t0 = windowStart()
    const runs = await Promise.all(pages.map((p) => startRun(p, "RUN ALL")))

    // UI truth: each page still renders and answers the locator round-trip
    // while all runs are in flight - a hung renderer fails the deadline
    for (const p of pages) {
      await expect(
        p.locator('button[role="tab"]:has-text("Workflow")'),
        "a page stopped responding while the concurrent runs were in flight",
      ).toBeVisible({ timeout: 10_000 })
    }

    // CloudWatch truth: each run's WORKFLOW START logged in the premium group
    for (const run of runs) {
      await expect
        .poll(() => cloudwatchHas(PREMIUM_LOG_GROUP, `(ID: ${run.uid},`, t0), {
          ...CLOUDWATCH_POLL,
          message: `no WORKFLOW START for concurrent run ${run.uid} in ${PREMIUM_LOG_GROUP}`,
        })
        .toBe(true)
    }

    const recordedStatus = async (i: number) => {
      // routedApiHeaders, not apiHeaders: without the routing headers the
      // poll lands on the free tier, which never saw these premium runs.
      // A slow answer is not a verdict: three runs computing on one t3.large
      // can outlast the request budget, so let the poll retry instead of
      // failing the row - the poll's own deadline still bounds it.
      let res
      try {
        res = await page.request.get(
          `${apiUrl()}/experiments/${runs[i].workspaceId}`,
          {
            headers: await routedApiHeaders(page),
            timeout: STATUS_REQUEST_TIMEOUT_MS,
          },
        )
      } catch (e) {
        return `GET /experiments threw: ${(e as Error).message.split("\n")[0]}`
      }
      if (!res.ok()) return `GET /experiments -> ${res.status()}`
      const experiments = (await res.json()) as Record<
        string,
        { success?: string }
      >
      return experiments?.[runs[i].uid]?.success ?? "absent"
    }
    await Promise.all(
      runs.map(async (run, i) => {
        let recorded = ""
        await expect
          .poll(async () => (recorded = await recordedStatus(i)), {
            timeout: RUN_TIMEOUT_MS + 300_000,
            intervals: [15_000],
            message: `concurrent run ${run.uid} never settled`,
          })
          .toMatch(/^(success|error)$/)
        expect(
          recorded,
          `concurrent run ${run.uid} recorded "${recorded}", not success`,
        ).toBe("success")
      }),
    )
  } finally {
    for (const p of pages.slice(1)) {
      await p.close().catch(() => {})
    }
    const headers = await apiHeaders(page)
    for (const wsId of wsIds) {
      await page.request
        .delete(`${apiUrl()}/workspace/${wsId}`, {
          headers,
          timeout: RELEASE_REQUEST_TIMEOUT_MS,
        })
        .catch(() => {})
    }
  }
})

test("PREM-06 - The sweep stops a released idle instance and keeps the last one warm @slow", async () => {
  const rows = "6221 / 6222"
  skipUnlessOptedIn(rows)
  test.skip(
    !PREMIUM2_USER.email || !PREMIUM2_USER.password,
    `rows ${rows}: TEST_PREMIUM2_EMAIL/TEST_PREMIUM2_PASSWORD not set - needs two concurrent premium users`,
  )
  test.setTimeout(TEST_TIMEOUT_MS * 2)

  const s1 = await apiLogin(PREMIUM_USER.email, PREMIUM_USER.password)
  const s2 = await apiLogin(PREMIUM2_USER.email, PREMIUM2_USER.password)
  try {
    const post = (s: typeof s1) => () =>
      s.api.post("/users/me/premium/assign", {
        headers: s.headers,
        timeout: ASSIGN_REQUEST_TIMEOUT_MS,
      })
    const release = async (s: typeof s1) => {
      const res = await s.api.delete("/users/me/premium/assign", {
        headers: s.headers,
        timeout: RELEASE_REQUEST_TIMEOUT_MS,
      })
      expect(res.ok(), await res.text()).toBe(true)
    }

    let a1: { is_shared?: boolean; instance_id?: string } =
      await assignUntilSettled(post(s1), rows)
    let a2: { is_shared?: boolean; instance_id?: string } =
      await assignUntilSettled(post(s2), rows)
    const distinct = () =>
      isDedicated(a1) && isDedicated(a2) && a1.instance_id !== a2.instance_id

    // The immediate cascade grants at most one dedicated - the second user
    // lands shared. A shared user alone on their own instance is flipped to
    // dedicated in place by the sweep's fix_incorrect_is_shared_flags, so
    // when the users hold distinct instances, run that reconciliation and
    // re-read instead of skipping a state that is one sweep away.
    if (!distinct() && a1.instance_id !== a2.instance_id) {
      invokeMonitoringSweep()
      const statusAssignment = async (s: typeof s1) => {
        const res = await s.api.get("/users/me/premium/status", {
          headers: s.headers,
          timeout: STATUS_REQUEST_TIMEOUT_MS,
        })
        return res.ok() ? ((await res.json()).assignment ?? null) : null
      }
      const deadline = Date.now() + 2 * 60_000
      for (;;) {
        a1 = (await statusAssignment(s1)) ?? a1
        a2 = (await statusAssignment(s2)) ?? a2
        if (distinct() || Date.now() > deadline) break
        await new Promise((r) => setTimeout(r, 15_000))
      }
    }
    if (!a1.is_shared || !a2.is_shared) heldDedicated = true
    const distinctDedicated = distinct()

    // Sampled while the users still hold them. A hard release runs
    // scale_down_if_possible() inline and then, with no premium user left,
    // converts every remaining idle instance to standby - so both releases
    // return with the pool already cold and an after-the-fact sample reads 0.
    const runningBefore = runningPremiumInstanceIds()

    const tRelease = Date.now() - 5_000
    await release(s1)
    await release(s2)

    // The release's own scale-down prints its analysis, so this verifies every
    // run - including one where the cascade could not grant two distinct
    // instances and the outcome half below has to skip.
    let analysisLine = ""
    await expect
      .poll(
        () =>
          (analysisLine = latestManagerLine("Scale-down analysis:", tRelease)),
        {
          ...CLOUDWATCH_POLL,
          message: `no Scale-down analysis line in ${PREMIUM_MANAGER_LOG_GROUP} after the releases`,
        },
      )
      .toContain("Scale-down analysis:")
    expect(
      analysisLine.match(
        /Scale-down analysis: \d+ total, (\d+) occupied, (\d+) idle, (\d+) active users/,
      ),
      analysisLine,
    ).toBeTruthy()

    test.skip(
      !distinctDedicated,
      `rows ${rows}: no two distinct dedicated instances this run, even after ` +
        `the solo-shared reconciliation sweep (${JSON.stringify([a1, a2])}) - ` +
        `sweep analysis verified; pre-stage a second instance to verify manually`,
    )
    expect(runningBefore.length).toBeGreaterThanOrEqual(2)

    // 6221: the decision named real instances, and they were the ones the two
    // users had been holding - not some unrelated capacity.
    let stopLine = ""
    await expect
      .poll(() => (stopLine = latestManagerLine("idle instances:", tRelease)), {
        ...CLOUDWATCH_POLL,
        message:
          `no scale-down stop decision in ${PREMIUM_MANAGER_LOG_GROUP} after ` +
          `releasing both dedicated instances; analysis was: ${analysisLine}`,
      })
      .toContain("idle instances:")
    const stoppedIds = [...stopLine.matchAll(/i-[0-9a-f]+/g)].map((m) => m[0])
    expect(stoppedIds.length, stopLine).toBeGreaterThan(0)
    expect(
      runningBefore,
      `${stopLine} stopped an instance the released users never held`,
    ).toEqual(expect.arrayContaining(stoppedIds))

    // 6222: the same decision spared one. The pool still ends cold, because a
    // hard release with no premium users left additionally converts the
    // remainder to standby - but that is the logout path, not scale-down
    // stripping every idle instance in a single decision.
    expect(
      stoppedIds.length,
      `scale-down stopped every idle instance at once: ${stopLine}`,
    ).toBeLessThan(runningBefore.length)

    // The decision is not the outcome: the named instances really left the
    // running state, and left the ECS cluster with them rather than lingering
    // as ghost registrations.
    await expect
      .poll(
        () =>
          runningPremiumInstanceIds().filter((id) => stoppedIds.includes(id)),
        {
          timeout: 5 * 60_000,
          intervals: [15_000],
          message: `scale-down named ${stoppedIds} but they are still running`,
        },
      )
      .toHaveLength(0)
    await expect
      .poll(
        () => ecsContainerEc2Ids().filter((id) => stoppedIds.includes(id)),
        {
          timeout: 3 * 60_000,
          intervals: [15_000],
          message: `stopped instance(s) ${stoppedIds} still registered in ECS`,
        },
      )
      .toHaveLength(0)
  } finally {
    await s1.api.dispose()
    await s2.api.dispose()
  }
})

test("PREM-09 - The premium subscription row is real in the deployed RDS @slow", async () => {
  const rows = "BT-606"
  skipUnlessOptedIn(rows)
  const sqlReason = sqlSkipReason()
  test.skip(!!sqlReason, `rows ${rows}: ${sqlReason}`)
  test.setTimeout(10 * 60_000)

  const { api, headers } = await apiLogin(
    PREMIUM_USER.email,
    PREMIUM_USER.password,
  )
  try {
    const statusRes = await api.get("/users/me/premium/status", {
      headers,
      timeout: STATUS_REQUEST_TIMEOUT_MS,
    })
    expect(statusRes.ok(), await statusRes.text()).toBe(true)
    expectGenuinelyPremium(await statusRes.json())
    const me = await api.get("/users/me", {
      headers,
      timeout: STATUS_REQUEST_TIMEOUT_MS,
    })
    const userId: number = (await me.json()).id

    // BT-606's own query: plan_id = 2, a future expiration, no scheduled
    // downgrade - read over SSM from the real RDS, not through the API
    const row = runSql(
      `SELECT plan_id, expiration > NOW(), scheduled_downgrade
         FROM subscription_users WHERE user_id = ${userId};`,
    )
    expect(row, `subscription_users row for user ${userId}: "${row}"`).toMatch(
      /^2\s+1\s+0$/,
    )
  } finally {
    await api.dispose()
  }
})

// Row 6226: the database's picture of the premium fleet has to follow AWS.
// TestReconcileInstanceStates drives the reconcile transaction directly, so
// what stays unproven is everything around it - that terminating a real EC2
// reaches the Cleanup Lambda through the EventBridge rule at all, and that the
// row really disappears. The standby is the only premium instance owned by
// nobody, so it is the only one this may destroy; the sweep at the end buys
// back the one it consumed.
test("PREM-10 - Terminating a premium instance reconciles its database row away @slow", async () => {
  const rows = "6226"
  skipUnlessOptedIn(rows)
  const sqlReason = sqlSkipReason()
  test.skip(!!sqlReason, `rows ${rows}: ${sqlReason}`)
  test.setTimeout(20 * 60_000)

  const instanceId = runSql(
    `SELECT instance_id FROM premium_user_assignments
       WHERE user_id IS NULL AND is_standby = 1 AND status = 'active'
       ORDER BY id DESC LIMIT 1;`,
  )
  test.skip(
    !/^i-[0-9a-f]+$/.test(instanceId),
    `rows ${rows}: no unowned standby row to reconcile ("${instanceId}") - ` +
      `terminating an instance a user holds is not this test's to do`,
  )
  const rowsFor = () =>
    runSql(
      `SELECT COUNT(*) FROM premium_user_assignments
         WHERE instance_id = '${instanceId}';`,
    )
  expect(
    rowsFor(),
    `the database must start out holding a row for standby ${instanceId}, ` +
      `or its disappearance proves nothing`,
  ).toBe("1")

  const since = Date.now() - 5_000
  awsJson(`ec2 terminate-instances --instance-ids ${instanceId}`)
  try {
    // The event-driven path, not the hourly walk: this line only comes from
    // the reconcile_instance action the EventBridge rule delivers.
    await expect
      .poll(
        () =>
          cloudwatchHas(
            PREMIUM_CLEANUP_LOG_GROUP,
            `Targeted instance reconciliation for ${instanceId}`,
            since,
          ),
        {
          timeout: 10 * 60_000,
          intervals: [15_000],
          message:
            `no targeted reconciliation for ${instanceId} in ` +
            `${PREMIUM_CLEANUP_LOG_GROUP} after terminating it`,
        },
      )
      .toBe(true)
    await expect
      .poll(() => rowsFor(), {
        timeout: 5 * 60_000,
        intervals: [15_000],
        message: `the row for terminated ${instanceId} was never reconciled away`,
      })
      .toBe("0")
  } finally {
    invokeMonitoringSweep()
  }
})

// Row 6204: two premium users assigning at the same moment must not corrupt the
// pool. TestConcurrentAssignLock and the GET_LOCK integration lane pin the lock
// itself; what is unproven is the end state on the real cascade, where every
// contended assign now falls through to a tier the row's literal "same instance,
// one shared" outcome never describes (the sheet's own [FLAG: codebase]). So
// this asserts the invariant instead of the wording: one row per user, and no
// two users holding the same instance as dedicated.
test("PREM-11 - Two users assigning at once never double-assign the pool @slow", async () => {
  const rows = "6204"
  skipUnlessOptedIn(rows)
  test.skip(
    !PREMIUM2_USER.email || !PREMIUM2_USER.password,
    `rows ${rows}: TEST_PREMIUM2_EMAIL/TEST_PREMIUM2_PASSWORD not set - a race needs two users`,
  )
  const sqlReason = sqlSkipReason()
  test.skip(!!sqlReason, `rows ${rows}: ${sqlReason}`)
  test.setTimeout(TEST_TIMEOUT_MS)

  const realRows = (where: string) =>
    runSql(
      `SELECT COUNT(*) FROM premium_user_assignments
         WHERE is_standby = 0 AND status IN ('active', 'pending_release')
           AND ${where};`,
    )
  // Someone else's assignment would make every count below ambiguous.
  expect(
    realRows("1 = 1"),
    "the pool must start with no real user assignments - another tester or a " +
      "lane that did not clean up still holds premium capacity",
  ).toBe("0")

  const s1 = await apiLogin(PREMIUM_USER.email, PREMIUM_USER.password)
  const s2 = await apiLogin(PREMIUM2_USER.email, PREMIUM2_USER.password)
  try {
    const userId = async (s: typeof s1) => {
      const me = await s.api.get("/users/me", {
        headers: s.headers,
        timeout: STATUS_REQUEST_TIMEOUT_MS,
      })
      expect(me.ok(), await me.text()).toBe(true)
      return (await me.json()).id as number
    }
    const [u1, u2] = [await userId(s1), await userId(s2)]
    expect(u1, "the two premium accounts must be different users").not.toBe(u2)

    const post = (s: typeof s1) => () =>
      s.api.post("/users/me/premium/assign", {
        headers: s.headers,
        timeout: ASSIGN_REQUEST_TIMEOUT_MS,
      })

    // The race itself: both in flight before either has returned. Anything
    // sequential here would exercise the uncontended path and prove nothing.
    const [first, second] = await Promise.all([post(s1)(), post(s2)()])
    for (const res of [first, second]) {
      expect(res.status(), await res.text()).toBeLessThan(500)
    }

    // The row's own step 2: let both invocations settle, then read the
    // database. Re-assigning to settle would only meet the endpoint's
    // 20-second per-user throttle ("Assignment request too frequent"), and a
    // fresh uncontended assign proves nothing about the race anyway.
    await expect
      .poll(() => realRows(`user_id IN (${u1}, ${u2})`), {
        timeout: ASSIGN_TIMEOUT_MS,
        intervals: [15_000],
        message: `the two racing users never settled into assignments`,
      })
      .toBe("2")
    if (
      runSql(
        `SELECT COUNT(*) FROM premium_user_assignments
           WHERE user_id IN (${u1}, ${u2}) AND is_standby = 0 AND is_shared = 0
             AND status IN ('active', 'pending_release')
             AND instance_id <> '${AUTOSCALING_POOL}';`,
      ) !== "0"
    ) {
      heldDedicated = true
    }

    // Both users really hold capacity, one row each: a race that dropped one
    // user, or gave one of them two rows, lands here.
    expect(realRows(`user_id = ${u1}`), `user ${u1} holds one assignment`).toBe(
      "1",
    )
    expect(realRows(`user_id = ${u2}`), `user ${u2} holds one assignment`).toBe(
      "1",
    )
    expect(
      realRows(`user_id IS NULL`),
      "the race left an ownerless real assignment row behind",
    ).toBe("0")

    // The corruption the lock exists to prevent: two users each told an
    // instance is theirs alone.
    expect(
      runSql(
        `SELECT COUNT(*) FROM (
           SELECT instance_id FROM premium_user_assignments
             WHERE is_standby = 0 AND is_shared = 0
               AND status IN ('active', 'pending_release')
               AND instance_id <> '${AUTOSCALING_POOL}'
             GROUP BY instance_id HAVING COUNT(*) > 1
         ) AS doubled;`,
      ),
      `instances held as dedicated by more than one user after the race ` +
        `(${runSql(
          `SELECT user_id, instance_id, is_shared FROM premium_user_assignments
             WHERE user_id IN (${u1}, ${u2}) AND is_standby = 0;`,
        ).replace(/\s+/g, " ")})`,
    ).toBe("0")
  } finally {
    for (const s of [s1, s2]) {
      await s.api
        .delete("/users/me/premium/assign", {
          headers: s.headers,
          timeout: RELEASE_REQUEST_TIMEOUT_MS,
        })
        .catch(() => {})
      await s.api.dispose()
    }
  }
})

// Row 6216: the premium tier's own recovery. PremiumRetriggerAssign.test.tsx
// covers the frontend half against mocks - a 502 flips the routing state to
// DEGRADED and a later 200 from the same instance heals it without a re-assign.
// What stayed manual is the ECS half: stopping the task the user is really
// routed to and waiting for the replacement to serve. It sits in the @prem lane
// rather than the disruptive one because it only ever touches the premium test
// user's own dedicated instance, and skips outright if the grant is shared.
test("PREM-12 - Stopping a user's premium task brings a replacement back to healthy @slow", async () => {
  const rows = "6216"
  skipUnlessOptedIn(rows)
  test.setTimeout(TEST_TIMEOUT_MS)

  const { api, headers } = await apiLogin(
    PREMIUM_USER.email,
    PREMIUM_USER.password,
  )
  try {
    const me = await api.get("/users/me", {
      headers,
      timeout: STATUS_REQUEST_TIMEOUT_MS,
    })
    expect(me.ok(), await me.text()).toBe(true)
    const userId: number = (await me.json()).id

    const assignment = await assignUntilSettled(
      () =>
        api.post("/users/me/premium/assign", {
          headers,
          timeout: ASSIGN_REQUEST_TIMEOUT_MS,
        }),
      rows,
    )
    test.skip(
      assignment.is_shared || !assignment.instance_id,
      `rows ${rows}: the cascade granted ${JSON.stringify(assignment)} - ` +
        `stopping the task on a shared instance would take its other tenants ` +
        `down with it`,
    )
    heldDedicated = true
    const instanceId = assignment.instance_id!
    await skipUnlessPremiumTargetHealthy(rows, userId)

    const before = premiumTaskOn(instanceId)
    expect(
      before?.taskArn,
      `no premium task on ${instanceId} to stop`,
    ).toBeTruthy()

    awsJson(
      `ecs stop-task --cluster ${CLUSTER} --task ${before!.taskArn} ` +
        `--reason e2e-row-6216`,
    )

    // ECS replaces it with no re-assign from the user: recovery is the
    // scheduler's job, not the client's. The target's health is sampled all
    // the way through rather than asserted to dip - the per-user group health
    // checks every 30s and needs three consecutive failures, so a replacement
    // that lands inside a minute is invisible to the ALB by construction
    // (a replacement has been seen RUNNING within ~30s of the stop).
    // Whether a window opens at all is placement latency, so it is reported,
    // not required; the 502-to-DEGRADED half of the row belongs to
    // PremiumRetriggerAssign.test.tsx, which owns what the client does if one
    // does slip through.
    const health: string[] = []
    let after: ReturnType<typeof premiumTaskOn>
    await expect
      .poll(
        () => {
          health.push(premiumTargetHealth(userId).join("+") || "none")
          after = premiumTaskOn(instanceId)
          return (
            !!after &&
            after.taskArn !== before!.taskArn &&
            after.lastStatus === "RUNNING"
          )
        },
        {
          timeout: 15 * 60_000,
          intervals: [10_000],
          message:
            `ECS never placed a running replacement on ${instanceId} after ` +
            `${before!.taskArn} was stopped (target health seen: ` +
            `${health.join(", ")})`,
        },
      )
      .toBe(true)
    console.log(
      `[15-premium-aws] PREM-12 premium-${userId}-tg across the replacement: ` +
        health.join(", "),
    )

    // The replacement is only meaningful if the original really went away by
    // this test's hand, rather than the poll having watched an unrelated
    // redeployment roll past.
    expect(
      awsJson<{ tasks: { lastStatus: string; stoppedReason?: string }[] }>(
        `ecs describe-tasks --cluster ${CLUSTER} --tasks ${before!.taskArn}`,
      ).tasks[0],
      "the task this test stopped really stopped, carrying its own reason",
    ).toMatchObject({ lastStatus: "STOPPED", stoppedReason: "e2e-row-6216" })

    // And the user is served again at the end, without having re-assigned.
    await expect
      .poll(() => premiumTargetHealth(userId).includes("healthy"), {
        timeout: 5 * 60_000,
        intervals: [15_000],
        message: `premium-${userId}-tg never came back healthy after the replacement`,
      })
      .toBe(true)
  } finally {
    await api
      .delete("/users/me/premium/assign", {
        headers,
        timeout: RELEASE_REQUEST_TIMEOUT_MS,
      })
      .catch(() => {})
    await api.dispose()
  }
})

// ---------------------------------------------------------------------------
// Row 6237: a dedicated-instance outage detected by one tab must reach the
// second tab over BroadcastChannel, and the detection event must be logged
// exactly once across both tabs (echo prevention). The outage is real: an
// iptables REJECT on the instance's own container port, applied over SSM and
// removed in a finally, with a 15-minute on-host safety net in case the test
// process dies holding the rule.
// ---------------------------------------------------------------------------

function setPremiumPortBlock(instanceId: string, on: boolean): void {
  const rule = "-p tcp -d $CIP --dport 8000 -j REJECT --reject-with tcp-reset"
  runShellOverSsm(
    instanceId,
    [
      "set -e",
      "CID=$(sudo docker ps -q --filter name=ecs- --filter publish=8000 | head -1)",
      '[ -n "$CID" ]',
      "CIP=$(sudo docker inspect $CID --format {{.NetworkSettings.IPAddress}})",
      on
        ? `sudo iptables -I DOCKER-USER ${rule}`
        : `sudo iptables -D DOCKER-USER ${rule} || true`,
      ...(on
        ? [
            `nohup bash -c "sleep 900 && sudo iptables -D DOCKER-USER ${rule}" >/dev/null 2>&1 &`,
          ]
        : []),
    ],
    "premium port block",
  )
}

// Count of one UI-event kind for one user since a moment. Which tier logs the
// line depends on the routing state at the moment it is shipped: the outage
// events go out with premium routing torn down (free or public, whichever ALB
// rule catches /users/me), while the recovery event is shipped just after
// routing is restored and lands in the PREMIUM group - measured, and the
// reason an earlier free+public-only count read zero for a recovery that had
// demonstrably happened. All three are counted. The trailing space in the
// event term keeps "event=instance_unreachable " from also matching
// instance_unreachable_popup_shown.
function uiEventCount(userId: number, event: string, sinceMs: number): number {
  let total = 0
  for (const group of [FREE_LOG_GROUP, PUBLIC_LOG_GROUP, PREMIUM_LOG_GROUP]) {
    total += awsJson<unknown[]>(
      `logs filter-log-events --log-group-name ${group} ` +
        `--start-time ${sinceMs} ` +
        `--filter-pattern '"Premium UI event: user=${userId} " "event=${event} "' ` +
        `--query 'events[]'`,
    ).length
  }
  return total
}

// Sorted CloudWatch timestamps of one UI-event kind for one user since a
// moment, merged across the three tier groups (which tier logs each event
// depends on the routing state at the moment it ships - see uiEventCount).
function uiEventTimestamps(
  userId: number,
  event: string,
  sinceMs: number,
): number[] {
  const stamps: number[] = []
  for (const group of [FREE_LOG_GROUP, PUBLIC_LOG_GROUP, PREMIUM_LOG_GROUP]) {
    stamps.push(
      ...awsJson<number[]>(
        `logs filter-log-events --log-group-name ${group} ` +
          `--start-time ${sinceMs} ` +
          `--filter-pattern '"Premium UI event: user=${userId} " "event=${event} "' ` +
          `--query 'events[].timestamp'`,
      ),
    )
  }
  return stamps.sort((a, b) => a - b)
}

test("PREM-13 - A dedicated outage broadcasts to the second tab and logs one detection event @slow", async ({
  page,
}) => {
  const rows = "6237"
  skipUnlessOptedIn(rows)
  test.setTimeout(TEST_TIMEOUT_MS + 15 * 60_000)

  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)
  expectGenuinelyPremium(await statusViaPage(page))
  const status = await waitForAssignment(page, rows)
  const assignment = status.assignment!
  // The unreachable snackbar renders only for a dedicated holder, and blocking
  // a shared instance would take other users down with it.
  test.skip(
    !!assignment.is_shared,
    "row 6237 needs a dedicated instance; the cascade granted shared",
  )
  heldDedicated = true
  const instanceId = assignment.instance_id!
  expect(instanceId).toMatch(/^i-[0-9a-f]+$/)

  const me = await page.request.get(`${apiUrl()}/users/me`, {
    headers: await apiHeaders(page),
    timeout: STATUS_REQUEST_TIMEOUT_MS,
  })
  const userId: number = (await me.json()).id
  await expect
    .poll(() => premiumTargetHealth(userId).includes("healthy"), {
      timeout: 8 * 60_000,
      intervals: [15_000],
      message: `premium-${userId}-tg never became healthy before the outage`,
    })
    .toBe(true)

  // A workspace to navigate into: the content requests it fires are what carry
  // premium routing headers (the dashboard list itself is free-tier-routed).
  const wsId = await openWorkspace(page, "e2e-prem-tabs")
  await expectStoredTierMatchesStatus(page)

  // The second tab. Same context = same origin storage and BroadcastChannel,
  // which is the variable this row tests.
  const tabB = await page.context().newPage()
  await tabB.goto("/workspaces")
  await expect
    .poll(
      () =>
        page.evaluate(
          () =>
            JSON.parse(localStorage.getItem("premium_poll_leader") || "null")
              ?.tabId ?? null,
        ),
      { timeout: 60_000, message: "no premium poll leader was elected" },
    )
    .not.toBeNull()
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem("premium_assigned")), {
      timeout: 60_000,
      message: "premium_assigned never reached true",
    })
    .toBe("true")
  expect(
    await page.evaluate(() =>
      localStorage.getItem("premium_unreachable_snapshot"),
    ),
    "an unreachable snapshot is already set before the outage",
  ).toBeNull()

  const snackbarIn = (p: Page) =>
    p.getByText(
      /dedicated premium instance is (temporarily unreachable|unresponsive)/,
    )
  // The interceptor logs this warn on every premium-routed 5xx before it
  // retries on the free tier - it separates "the block never bit" from "the
  // detection was suppressed" when the snackbar assert below fails.
  let premiumFallbacks = 0
  page.on("console", (msg) => {
    if (msg.text().includes("Using free tier while premium instance")) {
      premiumFallbacks += 1
    }
  })
  const t0 = windowStart()
  let blocked = false
  try {
    setPremiumPortBlock(instanceId, true)
    blocked = true

    // Tab A only - the sheet's own step 4. The trigger must fire a FRESH
    // premium-routed request each time: the Record tab's own getExperiments
    // runs once on mount and caches, so tab toggling never re-hits the
    // blocked instance (proven against a live assignment - zero /experiments
    // requests across a dozen toggles). The Record tab's "Reload" button
    // dispatches getExperiments on every click, which is a premium-routed
    // GET /experiments, so clicking it in a loop drives the 502 that tears
    // routing down and paints the snackbar. It also outlasts the 16s warm-up
    // grace a page load would re-arm.
    await page.locator('button[role="tab"]:has-text("Record")').click()
    const reload = page.getByRole("button", { name: "Reload" })
    await expect(reload).toBeVisible({ timeout: 30_000 })
    await expect
      .poll(
        async () => {
          if (
            await snackbarIn(page)
              .isVisible()
              .catch(() => false)
          )
            return true
          await reload.click().catch(() => {})
          await page.waitForTimeout(2_000)
          return snackbarIn(page)
            .isVisible()
            .catch(() => false)
        },
        {
          timeout: 180_000,
          intervals: [1_000],
          message:
            "the unreachable snackbar never appeared in tab A after the " +
            `block (premium 502s observed: ${premiumFallbacks})`,
        },
      )
      .toBe(true)
    // A premium-routed request really did 502 (not a free-tier 200 that would
    // make the snackbar meaningless) - triage evidence, asserted after the fact.
    expect(
      premiumFallbacks,
      "the snackbar appeared but no premium-routed request 502'd",
    ).toBeGreaterThan(0)

    // Tab B renders the same snackbar without any interaction of its own:
    // this visibility IS the broadcast assertion.
    await expect(snackbarIn(tabB)).toBeVisible({ timeout: 60_000 })
    expect(
      await page.evaluate(() =>
        localStorage.getItem("premium_unreachable_snapshot"),
      ),
      "the unreachable snapshot was never persisted",
    ).toBeTruthy()

    // Exactly one detection event across both tabs. The per-render
    // popup_shown line is the delivery control: once at least one of those
    // reached CloudWatch, telemetry is flowing, and the settle window below
    // gives a would-be echo from tab B time to land before the exact count.
    await expect
      .poll(
        () => uiEventCount(userId, "instance_unreachable_popup_shown", t0),
        {
          ...CLOUDWATCH_POLL,
          message: "no instance_unreachable_popup_shown telemetry arrived",
        },
      )
      .toBeGreaterThan(0)
    await expect
      .poll(() => uiEventCount(userId, "instance_unreachable", t0), {
        ...CLOUDWATCH_POLL,
        message: "no instance_unreachable detection event arrived",
      })
      .toBe(1)
    await new Promise((r) => setTimeout(r, 90_000))
    expect(
      uiEventCount(userId, "instance_unreachable", t0),
      "a second tab re-logged the detection event - echo prevention failed",
    ).toBe(1)

    setPremiumPortBlock(instanceId, false)
    blocked = false

    // Recovery is the machine's own half-open re-arm, and it must happen in
    // THIS tab WITHOUT a reload. Two measured facts shape this:
    //   - the re-arm timer only runs on the poll leader, and a reload mints a
    //     new tabId that no longer matches the stored premium_poll_leader, so
    //     a reloaded tab never re-arms and stays unreachable forever;
    //   - Retry is not offered here at all - the snackbar only goes terminal
    //     after MAX_FAILED_PROBES, which this short outage does not reach.
    // Left alone, the leader re-armed ~30s after the block lifted; the next
    // premium-routed request (the Reload button) then answers a verified 200,
    // which emits instance_reachable and clears the machine for real.
    await expect
      .poll(
        async () => {
          await reload.click().catch(() => {})
          await page.waitForTimeout(3_000)
          return uiEventCount(userId, "instance_reachable", t0)
        },
        {
          timeout: 6 * 60_000,
          intervals: [10_000],
          message: "tab A never emitted instance_reachable after recovery",
        },
      )
      .toBe(1)
    // The snackbar clears for real once the machine recovers.
    await expect(snackbarIn(page)).toBeHidden({ timeout: 60_000 })
    // Tab B dismisses off the PREMIUM_INSTANCE_REACHABLE broadcast alone.
    await expect(snackbarIn(tabB)).toBeHidden({ timeout: 60_000 })
    await expect
      .poll(
        () =>
          page.evaluate(() =>
            localStorage.getItem("premium_unreachable_snapshot"),
          ),
        { timeout: 60_000, message: "the unreachable snapshot never cleared" },
      )
      .toBeNull()

    await expect
      .poll(() => uiEventCount(userId, "instance_reachable", t0), {
        ...CLOUDWATCH_POLL,
        message: "no instance_reachable recovery event arrived",
      })
      .toBe(1)
    await new Promise((r) => setTimeout(r, 90_000))
    expect(
      uiEventCount(userId, "instance_reachable", t0),
      "a second tab re-logged the recovery event - echo prevention failed",
    ).toBe(1)
  } finally {
    if (blocked) setPremiumPortBlock(instanceId, false)
    await tabB.close().catch(() => {})
    await page.request
      .delete(`${apiUrl()}/workspace/${wsId}`, {
        headers: await apiHeaders(page),
        timeout: RELEASE_REQUEST_TIMEOUT_MS,
      })
      .catch(() => {})
  }
})

// ---------------------------------------------------------------------------
// Row 608: user data survives a migration to a different dedicated instance,
// because S3 is the source of truth and the new instance lazily fetches it.
// The migration is the premium manager's own migrate_shared_users path with
// all its safety checks (readiness, active_workflow_count); the only staging
// this test does is flip the assignment's is_shared flag so the optimizer
// considers the user's instance shared - the state a second user's login
// would create, minus the second user.
// ---------------------------------------------------------------------------

test("PREM-14 - User data stays accessible after migration to a different dedicated instance @slow", async ({
  page,
}) => {
  const rows = "608"
  skipUnlessOptedIn(rows)
  test.setTimeout(TEST_TIMEOUT_MS + RUN_TEST_TIMEOUT_MS + 20 * 60_000)

  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)
  expectGenuinelyPremium(await statusViaPage(page))
  const p = progressLog("15-premium-aws", "PREM-14")
  const assignment = await dedicatedAssignmentCoolingIfNeeded(page, rows)
  heldDedicated = true
  const instanceA = assignment.instance_id!
  expect(instanceA).toMatch(/^i-[0-9a-f]+$/)
  p.step(`dedicated assignment held on ${instanceA}`)

  const idRes = await page.request.get(`${apiUrl()}/users/me`, {
    headers: await apiHeaders(page),
    timeout: STATUS_REQUEST_TIMEOUT_MS,
  })
  expect(idRes.ok(), await idRes.text()).toBe(true)
  const meBody = await idRes.json()
  const userId: number = meBody.id
  await skipUnlessPremiumTargetHealthy(rows, userId)

  const wsName = "e2e-prem-migrate"
  const wsId = await openWorkspace(page, wsName)
  try {
    // The row's own precondition: a completed run, so both EBS-local and
    // S3-uploaded artefacts exist before anything moves.
    await importSampleData(page, wsName)
    const { uid } = await runTutorial(page, "Tutorial1", "RUN ALL")
    p.step(`precondition run ${uid} complete`)
    const bucket = meBody.attributes?.remote_bucket_name
    expect(
      bucket,
      "premium user has no remote_bucket_name attribute",
    ).toBeTruthy()
    await expect
      .poll(
        () => {
          p.tick("waiting for the run's outputs to land in S3")
          return s3ObjectCount(bucket, `app/studio_data/output/${wsId}/${uid}/`)
        },
        {
          timeout: 120_000,
          intervals: [15_000],
          message: "the run's outputs never landed in the user's own bucket",
        },
      )
      .toBeGreaterThan(0)

    // The sheet's own SQL check, before.
    const rowSql =
      `SELECT instance_id FROM premium_user_assignments ` +
      `WHERE user_id = ${userId};`
    expect(runSql(rowSql), "pre-migration assignment row").toBe(instanceA)

    const candidate = await stageSecondRunningInstance(instanceA)
    p.step(`migration target staged: ${candidate}`)

    // Now mark the assignment shared and let the manager's own migration loop
    // do the real work: pick the available instance, move the user, re-point
    // the ALB.
    runSqlWriteOnDev(
      `UPDATE premium_user_assignments SET is_shared = 1 ` +
        `WHERE user_id = ${userId} AND instance_id = '${instanceA}'`,
    )
    expect(
      runSql(
        `SELECT is_shared FROM premium_user_assignments ` +
          `WHERE user_id = ${userId};`,
      ),
      "the is_shared nudge did not land",
    ).toBe("1")

    const t0 = windowStart()
    const invokeMigration = () =>
      awsJson(
        `lambda invoke --function-name development-premium-manager ` +
          `--invocation-type Event --cli-binary-format raw-in-base64-out ` +
          `--payload '{"action":"migrate_shared_users","max_wait_seconds":300,` +
          `"retry_interval":15}' /dev/null`,
      )
    invokeMigration()
    p.step(
      `invoked migrate_shared_users; waiting for the row to leave ${instanceA}`,
    )

    // The migration's DB truth: the row moves to a different real instance.
    // Re-invoked as we poll, because one invocation gives up after its own
    // max_wait_seconds and a later pass may find the pool in a better state;
    // the handler takes a distributed lock, so an overlapping invoke is a
    // logged no-op rather than a second migration.
    let instanceB = ""
    let passes = 0
    await expect
      .poll(
        () => {
          instanceB = runSql(rowSql)
          if (/^i-[0-9a-f]+$/.test(instanceB) && instanceB !== instanceA) {
            return true
          }
          if (++passes % 15 === 0) invokeMigration()
          p.tick(`assignment still on ${instanceB} after ${passes} passes`)
          return false
        },
        {
          timeout: 14 * 60_000,
          intervals: [20_000],
          message:
            `user ${userId} never migrated off ${instanceA} onto ` +
            `${candidate} - read the premium-manager log for the optimizer's ` +
            `own verdict ("marked for migration" then either a target or ` +
            `"no running instances available")`,
        },
      )
      .toBe(true)

    p.step(`migrated onto ${instanceB}`)

    // The manager's own account of what happened.
    await expect
      .poll(
        () =>
          cloudwatchHas(
            PREMIUM_MANAGER_LOG_GROUP,
            `Migrated user ${userId}`,
            t0,
          ),
        {
          ...CLOUDWATCH_POLL,
          message: `no "Migrated user ${userId}" line in ${PREMIUM_MANAGER_LOG_GROUP}`,
        },
      )
      .toBe(true)

    // The row's claim is that S3 is the source of truth, so the proof is the
    // experiment's own file arriving on an instance that did not have it.
    // Read the filesystem rather than the log: the app's download line is a
    // listing summary that legitimately prints "workspaces: []", so its
    // absence does not mean no data moved (observed both ways).
    const onInstanceB = (cmd: string) =>
      runShellOverSsm(
        instanceB,
        [
          "set -e",
          "CID=$(sudo docker ps -q --filter name=ecs- --filter publish=8000 | head -1)",
          '[ -n "$CID" ]',
          `sudo docker exec "$CID" sh -c "${cmd}"`,
        ],
        "premium instance exec",
      )
    const tMigrated = windowStart()
    const experimentYaml = `/app/studio_data/output/${wsId}/${uid}/experiment.yaml`
    const yamlOnB = () =>
      onInstanceB(`test -f ${experimentYaml} && echo present || echo absent`)
    // Taken before anything re-opens the app, so the assertion below cannot be
    // satisfied by a file that was already sitting there.
    expect(
      yamlOnB(),
      `${instanceB} already holds ${experimentYaml}, so fetching it later ` +
        `would prove nothing`,
    ).toBe("absent")

    // The ALB really serves the user from the new instance before the UI half.
    await expect
      .poll(
        () => {
          p.tick(
            `waiting for premium-${userId}-tg to go healthy on ${instanceB}`,
          )
          return premiumTargetHealth(userId).includes("healthy")
        },
        {
          timeout: 8 * 60_000,
          intervals: [15_000],
          message: `premium-${userId}-tg never became healthy on ${instanceB}`,
        },
      )
      .toBe(true)
    p.step(`premium-${userId}-tg healthy on ${instanceB}`)
    // The app adopts the migrated assignment on its own assign-on-mount after
    // a reload (PREM-03's adoption fact). The hash comes from the same read
    // that reports the instance, so the two cannot describe different states.
    await page.reload()
    let adopted: string | undefined
    await expect
      .poll(
        async () => {
          const assignment = (await statusViaPage(page)).assignment
          adopted = assignment?.instance_id_hash
          return assignment?.instance_id
        },
        {
          timeout: 120_000,
          intervals: [10_000],
          message: "the server never reported the migrated assignment",
        },
      )
      .toBe(instanceB)
    expect(adopted, "status reported no instance_id_hash to adopt").toBeTruthy()

    // Wait for the ALB path to really converge on instance B before the app
    // fires its first premium-routed burst: right after the TG reports
    // healthy, a request can still ride the old target or 502 into the
    // interceptor's free-tier fallback, which then serves the whole session
    // (the reproduce can answer 200 from the free group and the served-by
    // assert below then fails on exactly that). The probe uses
    // explicit routing headers, so it proves the ALB itself, not client state.
    await expect
      .poll(
        async () => {
          p.tick(`waiting for routed requests to converge on ${instanceB}`)
          const probe = await page.request.get(`${apiUrl()}/users/me`, {
            headers: await routedApiHeaders(page),
            timeout: STATUS_REQUEST_TIMEOUT_MS,
          })
          return probe.headers()["x-served-by-instance"] ?? "unidentified"
        },
        {
          timeout: 5 * 60_000,
          intervals: [10_000],
          message:
            `premium-routed requests never converged onto the migrated ` +
            `instance ${instanceB} after its TG reported healthy`,
        },
      )
      .toBe(adopted)

    // Step 4: open the previously-created experiment. Instance B never ran it,
    // so serving this is only possible by lazily fetching the user's files
    // from S3, and the fetch is the row's whole point: S3 is the source of
    // truth, so an instance change costs the user nothing.
    await page.goto(`/workspaces/${wsId}`)
    // The app itself must have premium routing armed and pinned to B before
    // the reproduce goes out: the interceptor tears routing down on any
    // post-warm-up 200 served by a hash that differs from its pin, and its
    // recovery re-assign can complete seconds AFTER a reproduce has already
    // gone out headerless to the free tier (the failure shows up as the
    // assign-success toast racing the reproduce).
    await expect
      .poll(
        () =>
          page.evaluate(() => ({
            assigned: localStorage.getItem("premium_assigned"),
            pin: localStorage.getItem("premium_instance_id"),
          })),
        {
          timeout: 3 * 60_000,
          intervals: [5_000],
          message:
            `the app never re-armed premium routing pinned to ${instanceB} ` +
            `after the migration`,
        },
      )
      .toEqual({ assigned: "true", pin: adopted })
    const reproduced = page.waitForResponse(
      (r) => /\/workflow\/reproduce\//.test(r.url()),
      { timeout: RELEASE_REQUEST_TIMEOUT_MS },
    )
    await reproduceTutorial(page, "e2e-runall")

    // Which instance actually answered, read from the response rather than
    // from client state: a request whose premium headers are missing - or one
    // the interceptor retried after a 5xx - goes to the free tier, which
    // answers 200 without instance B ever seeing it. The middleware hashes the
    // serving instance into every authenticated response with the same
    // function that produced `adopted`, so this compares like with like.
    const servedBy = (await reproduced).headers()["x-served-by-instance"]
    expect(
      servedBy,
      `the reproduce was served by ${servedBy ?? "an unidentified tier"} ` +
        `rather than the migrated instance ${instanceB}: a routing failure ` +
        `after migration, not a storage one`,
    ).toBe(adopted)

    // The row's Expected #2, read from the instance rather than from a log
    // sentence: the experiment's own config, absent from this instance a
    // moment ago, is now on its disk. S3 is the only place it could have come
    // from - instance B never ran the workflow.
    await expect
      .poll(yamlOnB, {
        timeout: 6 * 60_000,
        intervals: [15_000],
        message:
          `${instanceB} never fetched ${experimentYaml} after the migration, ` +
          `so the experiment is not recoverable from S3 on the new instance`,
      })
      .toBe("present")
    // Reported, not asserted: the app's own download line names the bucket it
    // pulled from, but it summarises a listing and can read "workspaces: []"
    // even on a run that moved data.
    console.log(
      `[15-premium-aws] PREM-14: lazy-fetch log lines naming ${bucket} and ` +
        `workspace ${wsId}: ` +
        awsJson<unknown[]>(
          `logs filter-log-events --log-group-name ${PREMIUM_LOG_GROUP} ` +
            `--start-time ${tMigrated} --filter-pattern ` +
            `'"Download all metadata from remote storage" "${bucket}" ` +
            `"output/${wsId}/"' --query 'events[]'`,
        ).length,
    )
  } finally {
    await page.request
      .delete(`${apiUrl()}/workspace/${wsId}`, {
        headers: await apiHeaders(page),
        timeout: RELEASE_REQUEST_TIMEOUT_MS,
      })
      .catch(() => {})
  }
})

// ---------------------------------------------------------------------------
// Row 6217: the migration sweep must not move a user whose workflow is running.
// TestMigrationWorkflowGuard pins the guard function against mocks; what stayed
// manual is the guard holding on the real sweep against a real running
// snakemake workflow - and, the same test's own positive control, the sweep
// moving the user once that workflow completes. The guard reads
// active_workflow_count on the assignment row, which only a real run through
// the real premium routing increments (PREM-07's 0 -> 1 -> 0 fact).
// ---------------------------------------------------------------------------

test("PREM-22 - The migration sweep refuses a user mid-workflow, then migrates after completion @slow", async ({
  page,
}) => {
  const rows = "6217"
  skipUnlessOptedIn(rows)
  const sqlReason = sqlSkipReason()
  test.skip(!!sqlReason, `rows ${rows}: ${sqlReason}`)
  test.setTimeout(TEST_TIMEOUT_MS + RUN_TEST_TIMEOUT_MS + 30 * 60_000)

  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)
  expectGenuinelyPremium(await statusViaPage(page))
  // The guard is tier-agnostic but the staging is not: the sweep only
  // considers a user whose instance it deems shared, and the is_shared nudge
  // below pins the row to the instance it was staged on.
  const assignment = await dedicatedAssignmentCoolingIfNeeded(page, rows)
  heldDedicated = true
  const instanceA = assignment.instance_id!
  expect(instanceA).toMatch(/^i-[0-9a-f]+$/)

  const idRes = await page.request.get(`${apiUrl()}/users/me`, {
    headers: await apiHeaders(page),
    timeout: STATUS_REQUEST_TIMEOUT_MS,
  })
  expect(idRes.ok(), await idRes.text()).toBe(true)
  const userId: number = (await idRes.json()).id
  await skipUnlessPremiumTargetHealthy(rows, userId)

  const rowSql =
    `SELECT instance_id FROM premium_user_assignments ` +
    `WHERE user_id = ${userId};`
  const countSql =
    `SELECT active_workflow_count FROM premium_user_assignments ` +
    `WHERE user_id = ${userId};`
  // The is_shared nudge (PREM-14's lever) makes the optimizer consider the
  // user's instance shared. Re-applied inside the polls below: the 15-min
  // cron's fix_incorrect_is_shared_flags strips the flag from a user alone on
  // an instance, so a single nudge can silently vanish mid-window.
  const nudge = () =>
    runSqlWriteOnDev(
      `UPDATE premium_user_assignments SET is_shared = 1 ` +
        `WHERE user_id = ${userId} AND instance_id = '${instanceA}'`,
    )
  const invokeMigration = (maxWaitSeconds: number) =>
    awsJson(
      `lambda invoke --function-name development-premium-manager ` +
        `--invocation-type Event --cli-binary-format raw-in-base64-out ` +
        `--payload '{"action":"migrate_shared_users",` +
        `"max_wait_seconds":${maxWaitSeconds},"retry_interval":15}' /dev/null`,
    )

  const wsName = "e2e-prem-guard"
  const wsId = await openWorkspace(page, wsName)
  try {
    await importSampleData(page, wsName)
    // The sweep only emits the guard's refusal when it has a candidate to
    // offer: with no available instance it logs "no running instances
    // available" and never reaches the per-user guard at all.
    const candidate = await stageSecondRunningInstance(instanceA)

    // A real workflow, started and NOT awaited: the guard's premise is a run
    // in flight, proven by the row's own slot count rather than the UI.
    await reproduceTutorial(page, "Tutorial1")
    await startRun(page, "RUN ALL")
    await expect
      .poll(() => runSql(countSql), {
        timeout: 180_000,
        intervals: [10_000],
        message: "active_workflow_count never reached 1 after the run started",
      })
      .toBe("1")

    // Phase 1: the sweep sees a shared-flagged user with a ready candidate,
    // and must refuse. The guard line is can_migrate_user's own verdict.
    const t1 = windowStart()
    nudge()
    invokeMigration(90)
    let guardPasses = 0
    await expect
      .poll(
        () => {
          if (
            cloudwatchHas(
              PREMIUM_MANAGER_LOG_GROUP,
              `Cannot migrate user ${userId}`,
              t1,
            )
          ) {
            return true
          }
          if (++guardPasses % 8 === 0) {
            nudge()
            invokeMigration(90)
          }
          return false
        },
        {
          timeout: 8 * 60_000,
          intervals: [15_000],
          message:
            `no migration-guard refusal for user ${userId} in ` +
            `${PREMIUM_MANAGER_LOG_GROUP} - either the sweep never considered ` +
            `the user (is_shared nudge lost?) or it had no candidate ` +
            `(${candidate} gone?)`,
        },
      )
      .toBe(true)
    // The refusal is only the guard's if the run was still holding its slot.
    expect(
      runSql(countSql),
      "the workflow finished before the guard was exercised - phase 1 proves nothing",
    ).toBe("1")
    // The row's own Expected: the user did not move.
    expect(runSql(rowSql), "the sweep migrated a user mid-workflow").toBe(
      instanceA,
    )
    // Same window, same user id: the positive guard line above proves log
    // delivery caught up, so this absence is meaningful (the PREM-13 rule).
    expect(
      cloudwatchHas(PREMIUM_MANAGER_LOG_GROUP, `Migrated user ${userId}`, t1),
      `a "Migrated user ${userId}" line appeared while the workflow ran`,
    ).toBe(false)
    // Opened here, not after the completion wait: with is_shared still
    // nudged, the 15-min cron may migrate the user the moment the workflow
    // completes, and that migration must land inside phase 2's window.
    const t2 = windowStart()

    // Phase 2, the built-in positive control: completion must unblock the
    // very migration phase 1 refused - if staging were broken this also
    // fails, so phase 1 cannot pass vacuously. Completion is read off the
    // slot count the guard itself reads, not the UI snackbar: phase 1 took
    // minutes, and a "Workflow finished" snackbar can appear and auto-hide
    // while the CloudWatch polls above are still running.
    await expect
      .poll(() => runSql(countSql), {
        timeout: RUN_TIMEOUT_MS + 120_000,
        intervals: [15_000],
        message: "active_workflow_count did not return to 0 after the run",
      })
      .toBe("0")
    // Heal the candidate: the run took minutes, in which the pool manager
    // re-adds standby rows and the idle sweep may have stopped or even
    // terminated it - so the healed candidate may be a different instance.
    const candidate2 = await stageSecondRunningInstance(instanceA)

    nudge()
    invokeMigration(300)
    let instanceB = ""
    let movePasses = 0
    await expect
      .poll(
        () => {
          instanceB = runSql(rowSql)
          if (/^i-[0-9a-f]+$/.test(instanceB) && instanceB !== instanceA) {
            return true
          }
          if (++movePasses % 15 === 0) {
            runSqlWriteOnDev(
              `DELETE FROM premium_user_assignments ` +
                `WHERE instance_id = '${candidate2}' AND user_id IS NULL`,
            )
            nudge()
            invokeMigration(300)
          }
          return false
        },
        {
          timeout: 14 * 60_000,
          intervals: [20_000],
          message:
            `user ${userId} was never migrated off ${instanceA} after the ` +
            `workflow completed - the guard refusal in phase 1 is unproven ` +
            `as the cause of the non-migration`,
        },
      )
      .toBe(true)
    await expect
      .poll(
        () =>
          cloudwatchHas(
            PREMIUM_MANAGER_LOG_GROUP,
            `Migrated user ${userId}`,
            t2,
          ),
        {
          ...CLOUDWATCH_POLL,
          message: `no "Migrated user ${userId}" line in ${PREMIUM_MANAGER_LOG_GROUP} after completion`,
        },
      )
      .toBe(true)
  } finally {
    await page.request
      .delete(`${apiUrl()}/workspace/${wsId}`, {
        headers: await apiHeaders(page),
        timeout: RELEASE_REQUEST_TIMEOUT_MS,
      })
      .catch(() => {})
  }
})

// ---------------------------------------------------------------------------
// Row 6233: a user stranded on the transient autoscaling-pool tier is migrated
// to a ready dedicated instance inline, by the client's own adoption flow, in
// a single assign call. TestInlineMigrationOnAdoption pins the manager half
// against mocks; this drives it end to end: /premium/status deliberately 404s
// the pool sentinel, the app's own recovery issues the assign write, and that
// one call returns the migrated dedicated assignment. The staging repoint
// writes the same sentinel the backend itself stores when no capacity is free
// (assignment_source=autoscaling_temp), not a fabricated instance id - so the
// code path is the adoption branch, not input validation.
// ---------------------------------------------------------------------------

test("PREM-23 - Adoption of a pool-stranded assignment migrates inline to a dedicated instance @slow", async ({
  page,
}) => {
  const rows = "6233"
  skipUnlessOptedIn(rows)
  const sqlReason = sqlSkipReason()
  test.skip(!!sqlReason, `rows ${rows}: ${sqlReason}`)
  // The dedicated-or-cool staging can add a stop/start cycle on top of a
  // cold assign, so this needs more than the plain assign budget.
  test.setTimeout(TEST_TIMEOUT_MS + 15 * 60_000)

  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)
  expectGenuinelyPremium(await statusViaPage(page))
  // Dedicated required: the repoint below must leave a running, empty, ready
  // instance behind as the inline candidate, and the row must carry its
  // per-user TG and rule so the migration reuses them.
  const assignment = await dedicatedAssignmentCoolingIfNeeded(page, rows)
  heldDedicated = true
  const instanceA = assignment.instance_id!
  expect(instanceA).toMatch(/^i-[0-9a-f]+$/)

  const me = await page.request.get(`${apiUrl()}/users/me`, {
    headers: await apiHeaders(page),
    timeout: STATUS_REQUEST_TIMEOUT_MS,
  })
  expect(me.ok(), await me.text()).toBe(true)
  const userId: number = (await me.json()).id
  await skipUnlessPremiumTargetHealthy(rows, userId)

  const rowSql =
    `SELECT instance_id FROM premium_user_assignments ` +
    `WHERE user_id = ${userId};`
  expect(runSql(rowSql), "pre-repoint assignment row").toBe(instanceA)

  // Capture the client's own recovery write, armed before the repoint: the
  // page's 30s status poll can see the stranded state before the reload does,
  // and its assign is the same adoption flow.
  const adoptionAssign = page.waitForResponse(
    (r) =>
      r.url().includes("/premium/assign") && r.request().method() === "POST",
    { timeout: 5 * 60_000 },
  )

  // freshWindow, not windowStart: a cold-start assignment in THIS test's own
  // staging can pass through the pool tier and be moved off it by the async
  // sweep, logging the same migration wording minutes before the repoint.
  const tInline = freshWindow(
    PREMIUM_MANAGER_LOG_GROUP,
    `Inline migration successful: user ${userId}`,
  )
  const tMigrated = freshWindow(
    PREMIUM_MANAGER_LOG_GROUP,
    `Migrated user ${userId} from autoscaling-pool`,
  )
  runSqlWriteOnDev(
    `UPDATE premium_user_assignments SET instance_id = 'autoscaling-pool' ` +
      `WHERE user_id = ${userId} AND instance_id = '${instanceA}'`,
  )
  expect(runSql(rowSql), "the pool repoint did not land").toBe(
    "autoscaling-pool",
  )

  // Adoption: the provider's mount reads /status, which answers no assignment
  // for the sentinel, and the app's own assign flow issues the recovery write.
  await page.reload()
  const res = await adoptionAssign
  expect(res.ok(), await res.text()).toBe(true)
  const body = await res.json()
  // The one call did the whole recovery: migrated, dedicated, real instance.
  expect(body.assignment_source, JSON.stringify(body)).toBe("inline_migration")
  expect(body.assigned, JSON.stringify(body)).toBe(true)
  expect(body.is_shared, JSON.stringify(body)).toBe(false)
  expect(body.instance_id).toMatch(/^i-[0-9a-f]+$/)

  // DB truth: the row left the sentinel for the instance the call named,
  // and that instance is really running.
  expect(runSql(rowSql)).toBe(body.instance_id)
  expect(runningPremiumInstanceIds()).toContain(body.instance_id)

  // The manager's own account of the inline path (not the async sweep's).
  await expect
    .poll(
      () =>
        cloudwatchHas(
          PREMIUM_MANAGER_LOG_GROUP,
          `Inline migration successful: user ${userId}`,
          tInline,
        ),
      {
        ...CLOUDWATCH_POLL,
        message: `no inline-migration success line for user ${userId} in ${PREMIUM_MANAGER_LOG_GROUP}`,
      },
    )
    .toBe(true)
  await expect
    .poll(
      () =>
        cloudwatchHas(
          PREMIUM_MANAGER_LOG_GROUP,
          `Migrated user ${userId} from autoscaling-pool`,
          tMigrated,
        ),
      {
        ...CLOUDWATCH_POLL,
        message: `no migration line naming autoscaling-pool for user ${userId}`,
      },
    )
    .toBe(true)

  // The user is really served again: per-user TG healthy, UI tier consistent.
  await expect
    .poll(() => premiumTargetHealth(userId).includes("healthy"), {
      timeout: 8 * 60_000,
      intervals: [15_000],
      message: `premium-${userId}-tg never became healthy after the inline migration`,
    })
    .toBe(true)
  await expectStoredTierMatchesStatus(page)
})

// ---------------------------------------------------------------------------
// Row 6236: the unreachable machine's probe ladder against a REAL outage.
// unreachableMachine.test.ts pins the ladder's shape (doubling, the 300s cap,
// the terminal probe count) but computes every expected delay from
// INITIAL_PROBE_DELAY_MS itself, so nothing pins the constants to wall-clock
// seconds. This measures the first doubling for real, off the machine's own
// telemetry timestamps: ~30s from detection to the first armed probe, ~60s
// from that probe's failure to the second arm.
//
// The ladder needs no help to advance. Arming re-enables premium routing and
// then logs instance_probe_armed through the shared axios instance, so that
// telemetry POST is itself the probe: it 502s off the blocked instance,
// consumes the probe immediately, and still reaches CloudWatch through the
// interceptor's free-tier retry. So a failure timestamp always sits right
// after its arm, and the delay between them is not what this measures.
//
// Deliberately partial (the sheet cell says so): failures 3..5 to the 300s
// cap are ~11 more minutes of real waiting inside the 15-minute iptables
// self-heal ceiling - too tight to be reliable - and a runtime-read override
// flag was rejected as shipping test hooks in the production bundle. The
// terminal state's Retry recovery IS covered: terminal is staged through the
// app's own persisted snapshot (the exact localStorage contract row 6237b's
// hydration pins), because the Retry button only renders on the terminal
// variant of the snackbar and reaching terminal organically is the wait this
// test refuses.
// ---------------------------------------------------------------------------

test("PREM-24 - The probe ladder's first doubling is real wall-clock; terminal Retry recovers @slow", async ({
  page,
}) => {
  const rows = "6236"
  skipUnlessOptedIn(rows)
  test.setTimeout(TEST_TIMEOUT_MS + 15 * 60_000)

  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)
  expectGenuinelyPremium(await statusViaPage(page))
  const status = await waitForAssignment(page, rows)
  const assignment = status.assignment!
  test.skip(
    !!assignment.is_shared,
    `rows ${rows}: the ladder only runs for a dedicated holder; the cascade granted shared`,
  )
  heldDedicated = true
  const instanceA = assignment.instance_id!
  expect(instanceA).toMatch(/^i-[0-9a-f]+$/)

  const me = await page.request.get(`${apiUrl()}/users/me`, {
    headers: await apiHeaders(page),
    timeout: STATUS_REQUEST_TIMEOUT_MS,
  })
  expect(me.ok(), await me.text()).toBe(true)
  const userId: number = (await me.json()).id
  await expect
    .poll(() => premiumTargetHealth(userId).includes("healthy"), {
      timeout: 8 * 60_000,
      intervals: [15_000],
      message: `premium-${userId}-tg never became healthy before the outage`,
    })
    .toBe(true)

  // The Record tab's Reload button is the premium-routed request generator
  // (PREM-13's trigger mechanics: it dispatches getExperiments every click).
  const wsId = await openWorkspace(page, "e2e-prem-ladder")
  await page.locator('button[role="tab"]:has-text("Record")').click()
  const reload = page.getByRole("button", { name: "Reload" })
  await expect(reload).toBeVisible({ timeout: 30_000 })
  const snackbar = page.getByText(
    /dedicated premium instance is (temporarily unreachable|unresponsive)/,
  )

  const t0 = windowStart()
  let blocked = false
  try {
    setPremiumPortBlock(instanceA, true)
    blocked = true

    // Detection: drive premium-routed 502s until the machine flips.
    await expect
      .poll(
        async () => {
          if (await snackbar.isVisible().catch(() => false)) {
            return true
          }
          await reload.click().catch(() => {})
          await page.waitForTimeout(2_000)
          return snackbar.isVisible().catch(() => false)
        },
        {
          timeout: 180_000,
          intervals: [1_000],
          message: "the unreachable snackbar never appeared after the block",
        },
      )
      .toBe(true)

    // The ladder advances on its own (see the header): each arm's own telemetry
    // POST is the probe that fails it. Just wait for the second arm.
    await expect
      .poll(
        () => uiEventTimestamps(userId, "instance_probe_armed", t0).length,
        {
          timeout: 5 * 60_000,
          intervals: [10_000],
          message:
            "the machine never armed two probes - the first doubling cannot be measured",
        },
      )
      .toBeGreaterThanOrEqual(2)

    const flips = uiEventTimestamps(userId, "instance_unreachable", t0)
    const armed = uiEventTimestamps(userId, "instance_probe_armed", t0)
    const failures = uiEventTimestamps(userId, "instance_probe_failure", t0)
    expect(
      flips,
      "exactly one detection event anchors the ladder",
    ).toHaveLength(1)
    expect(
      failures.length,
      "the first armed probe was never consumed as a failure",
    ).toBeGreaterThanOrEqual(1)
    // The sheet's wall-clock claim, with a +/-20% band around each delay:
    // detection -> arm 1 is INITIAL_PROBE_DELAY_MS...
    const gap1 = armed[0] - flips[0]
    console.log(
      `[15-premium-aws] PREM-24 measured ladder: detection -> arm1 ` +
        `${gap1}ms (expect ~30000), failure1 -> arm2 ` +
        `${armed[1] - failures[0]}ms (expect ~60000)`,
    )
    expect(
      gap1,
      `detection -> first armed probe was ${gap1}ms, outside 30s +/-20%`,
    ).toBeGreaterThanOrEqual(24_000)
    expect(
      gap1,
      `detection -> first armed probe was ${gap1}ms, outside 30s +/-20%`,
    ).toBeLessThanOrEqual(36_000)
    // ...and failure 1 -> arm 2 is its first doubling. Asserting the doubled
    // band where 30s is expected is this test's named mutation check: red.
    const gap2 = armed[1] - failures[0]
    expect(
      gap2,
      `first failure -> second armed probe was ${gap2}ms, outside 60s +/-20%`,
    ).toBeGreaterThanOrEqual(48_000)
    expect(
      gap2,
      `first failure -> second armed probe was ${gap2}ms, outside 60s +/-20%`,
    ).toBeLessThanOrEqual(72_000)

    // Terminal + Retry, staged through the app's own snapshot contract while
    // the block still holds, so nothing can heal the machine before the
    // button is clicked. The write races the machine's own snapshot writes,
    // which is why the reload follows immediately.
    await page.evaluate(
      ([id]) =>
        localStorage.setItem(
          "premium_unreachable_snapshot",
          JSON.stringify({
            instance_id: id,
            unreachable_since: Date.now() - 60_000,
            failed_probes: 5,
            is_terminal: true,
            updated_at: Date.now(),
          }),
        ),
      [instanceA],
    )
    await page.reload()
    const terminalBar = page.getByText(/unresponsive after multiple attempts/)
    await expect(terminalBar).toBeVisible({ timeout: 60_000 })
    const retryButton = page.getByRole("button", { name: "Retry" })
    await expect(retryButton).toBeVisible({ timeout: 10_000 })

    // Recovery must not re-assign: the row and the instance are fine.
    const assignWrites: string[] = []
    page.on("request", (req) => {
      if (req.url().includes("/premium/assign") && req.method() !== "GET") {
        assignWrites.push(`${req.method()} ${req.url()}`)
      }
    })

    // Retry is clicked while the port is STILL blocked, which is what makes it
    // attributable: lifting the outage first would recover the machine on its
    // own before any click - onPremiumReachable CLEARs from any
    // verified 200 and never consults the terminal flag, and the reload's own
    // mount re-arms premium routing off a still-valid /status. So the button's
    // effect is asserted here on the one thing only it can do: reset the probe
    // budget, which returns the snackbar to its non-terminal variant.
    const tRetry = windowStart()
    await retryButton.click()
    await expect
      .poll(
        () => uiEventCount(userId, "instance_unreachable_manual_retry", tRetry),
        {
          ...CLOUDWATCH_POLL,
          message: "no manual-retry telemetry arrived after the Retry click",
        },
      )
      .toBe(1)
    await expect(
      page.getByText(/dedicated premium instance is temporarily unreachable/),
      "Retry did not reset the terminal state to a retrying one",
    ).toBeVisible({ timeout: 90_000 })

    // Now lift the outage. The re-armed ladder finds the instance healthy on
    // its next probe - no click needed, since each arm's telemetry POST is
    // itself the probe - and the machine clears for real.
    setPremiumPortBlock(instanceA, false)
    blocked = false
    await expect
      .poll(() => uiEventCount(userId, "instance_reachable", tRetry), {
        timeout: 5 * 60_000,
        intervals: [10_000],
        message: "no instance_reachable event after the outage was lifted",
      })
      .toBe(1)
    await expect(snackbar).toBeHidden({ timeout: 60_000 })
    expect(
      assignWrites,
      "the recovery re-assigned instead of re-probing",
    ).toHaveLength(0)

    // The same assignment came through the whole outage: identical row, no
    // re-assign, and the ALB still serves it.
    const after = (await statusViaPage(page)).assignment!
    expect(after.instance_id).toBe(instanceA)
    expect(after.assigned_at).toBe(assignment.assigned_at)
    await expect
      .poll(() => premiumTargetHealth(userId).includes("healthy"), {
        timeout: 3 * 60_000,
        intervals: [15_000],
        message: `premium-${userId}-tg unhealthy after the recovery`,
      })
      .toBe(true)
  } finally {
    if (blocked) setPremiumPortBlock(instanceA, false)
    await page.request
      .delete(`${apiUrl()}/workspace/${wsId}`, {
        headers: await apiHeaders(page),
        timeout: RELEASE_REQUEST_TIMEOUT_MS,
      })
      .catch(() => {})
  }
})
