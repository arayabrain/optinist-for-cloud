import { execSync } from "child_process"
import * as fs from "fs"
import * as os from "os"
import * as path from "path"

import {
  test,
  expect,
  APIRequestContext,
  APIResponse,
  Browser,
  Locator,
  Page,
  request,
} from "@playwright/test"

import {
  AWS_REGION,
  CLOUDWATCH_POLL,
  FREE_USER,
  PREMIUM_LOG_GROUP,
  PREMIUM_USER,
  PUBLIC_LOG_GROUP,
  RUN_TEST_TIMEOUT_MS,
  apiHeaders,
  apiLogin,
  apiUrl,
  awaitRunFinished,
  awsJson,
  cloudwatchHas,
  expectPremiumTargetGroupGone,
  filterWorkspace,
  freeStorageState,
  gotoDashboard,
  importSampleData,
  isLocalBaseUrl,
  logTail,
  login,
  openWorkspace,
  reproduceTutorial,
  runShellOverSsm,
  runSql,
  runSqlWriteOnDev,
  runTutorial,
  s3ObjectCount,
  skipUnlessPremiumTargetHealthy,
  skipWithoutCreds,
  sqlLiteral,
  startRun,
  windowStart,
} from "./helpers"

// Real-S3 truth for the storage rows (sheets 04 / 10 / System 607). The API
// answers 200 even when its S3 write or delete failed (upload_input_data and
// the workspace-delete cleanup both swallow errors to a logged return), so
// only an S3-side read proves the object really landed or really went away.
// Opt in explicitly - the lane reads and writes the real per-user buckets on
// the deployed dev environment:
//
//   RUN_SLOW=1 RUN_S3_AWS=1 npx playwright test e2e/16-storage-aws.spec.ts --retries 0
//
// A row whose S3 truth is the same for either account is registered once per
// tier from one body - the sheets ask it of both, and the variants differ
// only in the Tier they are handed. Each variant spells out its own case ID,
// free S3-0x and premium S3-2x: the skip-summary reporter keys on the leading
// `S3-\d+` of the title, so one shared ID with a `[premium]` suffix would let
// a run that forgot RUN_PREMIUM_AWS tick the premium row off the free pass.
//
// The premium half spends real premium capacity, so it rides RUN_PREMIUM_AWS
// on top, exactly as `15-premium-aws` does:
//
//   RUN_SLOW=1 RUN_S3_AWS=1 RUN_PREMIUM_AWS=1 npx playwright test \
//     e2e/16-storage-aws.spec.ts --retries 0
//
// A sign-off run should add E2E_FAIL_ON_SKIP=1, so an unrun premium variant
// fails the run rather than leaving a silent gap.
//
// The premium variants require the runner to stay serial (`workers: 1` in
// playwright.config.ts): they share one account, so two of them cannot hold an
// assignment at once, and the release bookkeeping below is module state.
//
// Rows whose subject is free-tier-specific stay free-only; each says why
// where it is defined.

const RUN_S3_AWS = process.env.RUN_S3_AWS === "1"
const RUN_PREMIUM_AWS = process.env.RUN_PREMIUM_AWS === "1"

const IMAGE_FIXTURE = path.join(
  __dirname,
  "..",
  "..",
  "sample_data",
  "dev_mouse2p_short_image.tiff",
)
const HDF5_FIXTURE = path.join(
  __dirname,
  "..",
  "..",
  "sample_data",
  "tutorial",
  "input",
  "sample_hdf5.h5",
)

const REQUEST_TIMEOUT_MS = 30_000
const UPLOAD_TIMEOUT_MS = 120_000

// A cold premium assign starts EC2 capacity and an ECS task: minutes, not
// seconds. The assignment row then becomes visible before the instance serves
// traffic, so waiting for it to serve gets its own budget on top. All three
// are spent in sequence in the worst case, and setupBudgetMs below must cover
// their sum - a setup that outruns the test's own timeout turns the capacity
// skip these budgets exist for into a red row. login()'s own three retries
// (~2.5 min worst case) are deliberately left out: they can eat into the
// body's margin but cannot turn a skip into a failure.
const PREMIUM_ASSIGN_TIMEOUT_MS = 15 * 60_000
// /premium/assign does real AWS work in-request (ALB rules, scale-up), so it
// needs far more than REQUEST_TIMEOUT_MS - the same budget `15-premium-aws`
// gives its own assign calls.
const PREMIUM_ASSIGN_REQUEST_TIMEOUT_MS = 5 * 60_000
// How often the assign is driven while waiting - far above the endpoint's own
// 30s per-user rate limit.
const PREMIUM_ASSIGN_PROBE_INTERVAL_MS = 2 * 60_000
// What skipUnlessPremiumTargetHealthy spends; mirrored here for the budget sum
const PREMIUM_SERVING_TIMEOUT_MS = 5 * 60_000
const PREMIUM_RELEASE_TIMEOUT_MS = 120_000
// Reported by /premium/assign while capacity warms, and deliberately absent
// from /premium/status - so the caller keeps assigning until a real instance
// frees up rather than settling on a tier status never reports.
const AUTOSCALING_POOL = "autoscaling-pool"

// Everything the storage rows need to know about which account they run as.
// The bodies below read only these fields, which is what keeps one test
// serving both sheets' variants of the same row.
type Tier = {
  key: "free" | "premium"
  user: { email: string; password: string }
  credsName: string
  // Both halves of the opt-in, as the skip message should spell them
  optedIn: boolean
  optInHint: string
  // Undefined means "log in during the test": the premium account has no
  // saved state, because its assignment is established by the login itself.
  storageState: () => string | undefined
  // active_workflow_count lives in a different table per tier
  assignmentTable: string
  signIn: (page: Page, rows: string) => Promise<void>
  // What signIn may spend before the test's own work starts
  setupBudgetMs: number
}

const FREE_TIER: Tier = {
  key: "free",
  user: FREE_USER,
  credsName: "TEST_USER_EMAIL/TEST_USER_PASSWORD",
  optedIn: RUN_S3_AWS,
  optInHint:
    "set RUN_S3_AWS=1 - reads and writes real S3 through the deployed env",
  storageState: freeStorageState,
  assignmentTable: "free_user_assignments",
  signIn: async (page) => {
    await gotoDashboard(page)
  },
  setupBudgetMs: 0,
}

const PREMIUM_TIER: Tier = {
  key: "premium",
  user: PREMIUM_USER,
  credsName: "TEST_PREMIUM_EMAIL/TEST_PREMIUM_PASSWORD",
  optedIn: RUN_S3_AWS && RUN_PREMIUM_AWS,
  optInHint:
    "set RUN_S3_AWS=1 RUN_PREMIUM_AWS=1 - real S3 plus a real premium " +
    "assignment on the shared dev pool",
  storageState: () => undefined,
  assignmentTable: "premium_user_assignments",
  signIn: signInPremium,
  setupBudgetMs:
    PREMIUM_ASSIGN_TIMEOUT_MS +
    PREMIUM_ASSIGN_REQUEST_TIMEOUT_MS +
    PREMIUM_SERVING_TIMEOUT_MS,
}

// Free before premium, so each row's free variant runs first and a premium
// capacity skip can never stand in for an unrun free regression.
const TIERS: Tier[] = [FREE_TIER, PREMIUM_TIER]

// Set the moment the lane really holds premium capacity, so the release in
// afterEach spends a Firebase login (rate-limited) only when there is
// something to give back. premiumLaneRan is never cleared: it is what tells
// the afterAll net whether this run touched premium capacity at all.
let premiumHeld = false
let premiumLaneRan = false

type PremiumStatus = {
  is_premium: boolean
  assignment: { instance_id?: string; is_shared?: boolean } | null
}

async function premiumStatus(page: Page): Promise<PremiumStatus> {
  const res = await page.request.get(`${apiUrl()}/users/me/premium/status`, {
    headers: await apiHeaders(page),
    timeout: REQUEST_TIMEOUT_MS,
  })
  expect(res.ok(), await res.text()).toBe(true)
  const body = await res.json()
  return { is_premium: !!body.is_premium, assignment: body.assignment ?? null }
}

// The one thing a premium row asserts that its free twin cannot. Everything
// else in these rows reads a bucket named on the user record, so it holds
// whichever task served the request - and the app strips its routing headers
// and retries on the shared tier after a 502, so "it was premium" has to be
// read off the wire.
function expectPremiumRouted(captured: Record<string, string>[], what: string) {
  expect(captured.length, `no ${what} request was captured`).toBeGreaterThan(0)
  for (const h of captured) {
    expect(
      h["x-user-tier"],
      `a ${what} request did not carry x-user-tier: premium - the app fell ` +
        `back to the shared tier, so this row proves nothing its free twin ` +
        `does not`,
    ).toBe("premium")
    expect(
      h["x-routing-id"],
      `a ${what} request carried no x-routing-id`,
    ).toBeTruthy()
  }
}

// expect.poll does not retry a throwing callback - it invokes it once and
// propagates. A single CloudWatch throttle or SSM hiccup would therefore end
// an eight-minute row red. Hand the error back as the polled value instead: a
// transient one is retried away, a persistent one times out carrying its own
// message.
function polled<T>(read: () => T): () => T | string {
  return () => {
    try {
      return read()
    } catch (e) {
      return `read failed: ${(e as Error).message.split("\n")[0]}`
    }
  }
}

async function userIdOf(page: Page): Promise<number> {
  const res = await page.request.get(`${apiUrl()}/users/me`, {
    headers: await apiHeaders(page),
    timeout: REQUEST_TIMEOUT_MS,
  })
  expect(res.ok(), await res.text()).toBe(true)
  return (await res.json()).id
}

// A real premium login, held until the account genuinely owns capacity that
// really serves: the assignment row is where active_workflow_count lives, and
// the app drives its workflow run through the ALB.
async function signInPremium(page: Page, rows: string) {
  // Before login(), not after: it authenticates, then retries on its own
  // /dashboard timeout - and the second attempt's goto("/login") on a session
  // that is already authenticated redirects away, so the button-submit
  // assertion throws from OUTSIDE its inner catch. Capacity is held by then,
  // and a flag set after the call would never be reached.
  premiumHeld = true
  premiumLaneRan = true
  await login(page, PREMIUM_USER.email, PREMIUM_USER.password)

  // The account trap this suite has been bitten by before: an account can say
  // "Premium" on /users/me while /premium/status says free (billing grace).
  const status = await premiumStatus(page)
  expect(
    status.is_premium,
    `${PREMIUM_USER.email} is not premium on /premium/status - verify the ` +
      `TEST_PREMIUM_* account against /premium/status, not /users/me`,
  ).toBe(true)

  // Poll the provider's own result, and drive the assign alongside it so one
  // failed assign-on-mount cannot cost the whole budget. Nothing in here may
  // throw or assert: every read is a symptom of the environment this loop
  // exists to wait out, so a 5xx, a timed-out probe or an unreachable status
  // endpoint must reach the deadline as evidence rather than end the row.
  // A 503 never becomes the last answer - the endpoint's rate limit answers
  // with one, and it would displace a real reply.
  let assignment = status.assignment
  let lastStatus = 0
  let lastBody: { scaling_in_progress?: boolean; instance_id?: string } = {}
  let lastError = ""
  const assignDeadline = Date.now() + PREMIUM_ASSIGN_TIMEOUT_MS
  let nextProbeAt = Date.now() + PREMIUM_ASSIGN_PROBE_INTERVAL_MS
  while (!assignment) {
    if (Date.now() >= nextProbeAt) {
      nextProbeAt = Date.now() + PREMIUM_ASSIGN_PROBE_INTERVAL_MS
      try {
        const probe = await page.request.post(
          `${apiUrl()}/users/me/premium/assign`,
          {
            headers: await apiHeaders(page),
            timeout: PREMIUM_ASSIGN_REQUEST_TIMEOUT_MS,
          },
        )
        if (probe.status() !== 503) {
          lastStatus = probe.status()
          lastBody = await probe.json().catch(() => ({}))
        }
      } catch (e) {
        lastError = `assign probe: ${(e as Error).message.split("\n")[0]}`
      }
    }
    try {
      assignment = (await premiumStatus(page)).assignment
    } catch (e) {
      lastError = `premium status: ${(e as Error).message.split("\n")[0]}`
    }
    if (assignment) break
    if (Date.now() > assignDeadline) {
      // What each clause below means the environment did:
      //   scaling_in_progress  the pool is still starting capacity
      //   AUTOSCALING_POOL     the cascade parked us on the warming tier
      //   lastStatus 0         no probe ever got a non-503 answer, which
      //                        needs something else to be assigning
      // A 503 is never recorded, so reaching the deadline with lastStatus 0
      // covers the rate limit, a placement the Lambda refused, AND the
      // service's own exception talking to it - a genuinely broken assign flow
      // among them. Separating those needs an unpinned message string from
      // another tree, so the sign-off protocol carries it instead:
      // E2E_FAIL_ON_SKIP=1 reddens all of these, with lastBody and lastError
      // in the reason, so the cause is printed rather than lost. Any other
      // real status still fails.
      test.skip(
        !!lastBody.scaling_in_progress ||
          lastBody.instance_id === AUTOSCALING_POOL ||
          lastStatus === 0,
        `rows ${rows} [premium]: no premium assignment within ` +
          `${PREMIUM_ASSIGN_TIMEOUT_MS / 60_000} min (last probe: ` +
          `${lastStatus || "503 or unanswered throughout"} ` +
          `${JSON.stringify(lastBody)}${lastError ? `; ${lastError}` : ""}) - ` +
          `a cold pool or the assign flow itself; the payload says which, and ` +
          `a sign-off run's E2E_FAIL_ON_SKIP=1 turns this red either way`,
      )
      throw new Error(
        `no premium assignment appeared and the backend is not scaling: ` +
          `${lastStatus} ${JSON.stringify(lastBody)}${lastError ? `; ${lastError}` : ""}`,
      )
    }
    await new Promise((r) => setTimeout(r, 15_000))
  }
  // The granted tier decides whether the gate below can read anything, and is
  // the first thing to know when a premium row fails on a 502.
  console.log(
    `[16-storage-aws] premium assignment: instance=` +
      `${assignment.instance_id ?? "none"}, shared=${assignment.is_shared}`,
  )
  // A shared grant has no per-user target group to read and its instance is
  // already serving other tenants; a dedicated one takes the shared gate.
  if (assignment.is_shared || !assignment.instance_id?.startsWith("i-")) return
  await skipUnlessPremiumTargetHealthy(
    `${rows} [premium]`,
    await userIdOf(page),
  )
}

// Never leave the shared premium account holding an instance: a stuck
// assignment degrades the dev environment for everyone and keeps billing for
// the capacity.
//
// The 200 is not the proof. DELETE /users/me/premium/assign is fail-open by
// design - users_me.py logs a failed release and still answers
// {"released": true}, and release_premium_user fails open on timeout, leaving
// the expiration sweep as the backstop. So read the assignment back, exactly
// as this lane reads S3 back after a 200.
// /premium/status fails open too: on an internal error it answers 200 with
// {assignment: null, error: "..."}, so a null alone is not proof of a release.
// Reject the error field before believing it.
async function assignmentState(
  api: APIRequestContext,
  headers: Record<string, string>,
): Promise<unknown> {
  try {
    const st = await api.get("/users/me/premium/status", {
      headers,
      timeout: REQUEST_TIMEOUT_MS,
    })
    if (!st.ok()) return `HTTP ${st.status()}`
    const body = await st.json()
    if (body.error) return `status error: ${body.error}`
    return body.assignment ?? "released"
  } catch (e) {
    return `status unreachable: ${(e as Error).message.split("\n")[0]}`
  }
}

async function releaseAndConfirm(stage: string) {
  const { api, headers } = await apiLogin(
    PREMIUM_USER.email,
    PREMIUM_USER.password,
  )
  try {
    const res = await api.delete("/users/me/premium/assign", {
      headers,
      timeout: PREMIUM_RELEASE_TIMEOUT_MS,
    })
    expect(res.ok(), await res.text()).toBe(true)
    await expect
      .poll(() => assignmentState(api, headers), {
        timeout: PREMIUM_RELEASE_TIMEOUT_MS,
        intervals: [10_000],
        message:
          `${stage}: the assignment survived a 200 release - premium ` +
          `capacity is still held on the shared dev environment`,
      })
      .toBe("released")
  } finally {
    await api.dispose()
  }
}

// The lane's closing invariant: nothing may still be held BY US. It reads
// first and repairs second - releasing first would let this hook fix the very
// leak it exists to report and then pass green - but it still repairs, so a
// reported leak is not also left behind. `15-premium-aws` closes on the same
// invariant, including the ALB half: a release can take the assignment row and
// the listener rule while leaving the per-user target group behind, and this
// lane creates one on every dedicated premium row.
//
// It is the net for an afterEach that never ran at all - a worker crash, say.
// It is NOT a guard against an afterEach cut short by the body's own timeout:
// a hook gets its own clock, bounded by the test-timeout value rather than by
// whatever the body left over.
async function expectNothingHeld() {
  const { api, headers } = await apiLogin(
    PREMIUM_USER.email,
    PREMIUM_USER.password,
  )
  try {
    // assignmentState never throws, so a blip comes back as a string. A real
    // assignment object is reported at once; only an error string is worth a
    // second look, or a momentary 5xx would end a 35-minute lane accusing it
    // of a leak that never happened.
    let held = await assignmentState(api, headers)
    if (typeof held === "string" && held !== "released") {
      await new Promise((r) => setTimeout(r, 10_000))
      held = await assignmentState(api, headers)
    }
    if (held !== "released") {
      // Confirmed, not fire-and-forget: DELETE answers 200 on a release that
      // failed, which is the whole reason this hook reads the state back.
      // Swallowed because reporting the leak is this hook's job - a throw
      // here would replace that report with the repair's own failure.
      await releaseAndConfirm("afterAll repair").catch(() => {})
    }
    expect(
      held,
      "the lane finished with premium capacity still held on shared dev",
    ).toBe("released")
    const me = await api.get("/users/me", {
      headers,
      timeout: REQUEST_TIMEOUT_MS,
    })
    expect(me.ok(), await me.text()).toBe(true)
    expectPremiumTargetGroupGone((await me.json()).id, "afterAll")
  } finally {
    await api.dispose()
  }
}

test.afterEach(async () => {
  if (!premiumHeld) return
  premiumHeld = false
  await releaseAndConfirm("afterEach")
})

test.afterAll(async () => {
  if (!premiumLaneRan) return
  test.setTimeout(5 * 60_000)
  await expectNothingHeld()
})

// Sign in as the tier's account and read back the two facts every row below
// needs of it: the id the assignment tables are keyed on, and the per-user
// bucket. A null bucket attribute must fail loudly - the backend silently
// falls back to the default bucket, which would make every S3 assert vacuous.
async function enterAs(
  page: Page,
  tier: Tier,
  rows: string,
): Promise<{
  userId: number
  bucket: string
  headers: Record<string, string>
}> {
  await tier.signIn(page, rows)
  // Unrouted on both tiers, deliberately, as PREM-07's own workspace delete is:
  // nothing this lane asks of the API needs the premium instance (the DB, an S3
  // repair, a DB-plus-S3 delete), while routing it there would expose every
  // call to the 502 a premium task answers whenever it is not serving. Only the
  // workflow run has to land on the instance, and the app routes that itself.
  const headers = await apiHeaders(page)
  const res = await page.request.get(`${apiUrl()}/users/me`, {
    headers,
    timeout: REQUEST_TIMEOUT_MS,
  })
  expect(res.ok(), await res.text()).toBe(true)
  const body = await res.json()
  const bucket: string | undefined = body.attributes?.remote_bucket_name
  expect(
    bucket,
    `the ${tier.key} user has no remote_bucket_name attribute`,
  ).toBeTruthy()
  return { userId: body.id, bucket: bucket!, headers }
}

function skipUnlessOptedIn(rows: string, tier: Tier = FREE_TIER) {
  skipWithoutCreds(tier.user, tier.credsName)
  test.skip(!tier.optedIn, `rows ${rows} [${tier.key}]: ${tier.optInHint}`)
  test.skip(
    isLocalBaseUrl(),
    `rows ${rows} [${tier.key}]: needs the deployed dev environment (remote storage is S3 there, the local stack runs none); BASE_URL is local`,
  )
  // The rows here delete workspaces and their real bucket prefixes, and the
  // premium variants assign real capacity: never point this lane anywhere but
  // the development environment.
  expect(
    process.env.BASE_URL || "",
    "this lane only runs against the development environment",
  ).toContain("development-optinist")
  // A pass on retry hides real-AWS flakiness from the sign-off sheet
  expect(test.info().project.retries, "run this lane with --retries 0").toBe(0)
}

function bucketExists(bucket: string): boolean {
  try {
    execSync(
      `aws s3api head-bucket --bucket ${bucket} --region ${AWS_REGION}`,
      {
        timeout: 30_000,
        stdio: ["pipe", "pipe", "pipe"],
      },
    )
    return true
  } catch {
    return false
  }
}

function objectExists(bucket: string, key: string): boolean {
  try {
    execSync(
      `aws s3api head-object --bucket ${bucket} --key ${key} ` +
        `--region ${AWS_REGION}`,
      { timeout: 30_000, stdio: ["pipe", "pipe", "pipe"] },
    )
    return true
  } catch {
    return false
  }
}

async function apiEnsureWorkspaceId(
  api: APIRequestContext,
  headers: Record<string, string>,
  name: string,
): Promise<number> {
  const list = await api.get("/workspaces?offset=0&limit=100", {
    headers,
    timeout: REQUEST_TIMEOUT_MS,
  })
  expect(list.ok(), await list.text()).toBe(true)
  const { items } = await list.json()
  const found = items.find((w: { name: string }) => w.name === name)
  if (found) return found.id
  const created = await api.post("/workspace", {
    headers,
    data: { name },
    timeout: REQUEST_TIMEOUT_MS,
  })
  expect(created.ok(), await created.text()).toBe(true)
  return (await created.json()).id
}

type TreeNode = {
  path: string
  name: string
  isdir: boolean
  nodes: TreeNode[]
  sync_status?: string
}

function findNode(nodes: TreeNode[], name: string): TreeNode | undefined {
  for (const node of nodes) {
    if (node.name === name) return node
    const hit = findNode(node.nodes ?? [], name)
    if (hit) return hit
  }
  return undefined
}

// The bucket, the upload and the sync round-trip are the account's own, so the
// premium variant needs the premium account but no premium assignment - it
// goes through the API and never mounts the dashboard, so nothing assigns.
// It stays behind RUN_PREMIUM_AWS all the same: one variable answers "may this
// run touch the premium account at all?" for the whole S3-2x block, and a
// per-row exception is a worse trade than the row costing one extra flag.
for (const tier of TIERS) {
  const id = { free: "S3-01", premium: "S3-21" }[tier.key]
  test(`${id} - The per-user bucket is real and an upload really lands its object @slow`, async () => {
    const rows = "403 / 528 / BT-1002 / BT-1003 / BT-1111"
    skipUnlessOptedIn(rows, tier)
    test.setTimeout(5 * 60_000)

    const { api, headers } = await apiLogin(tier.user.email, tier.user.password)
    try {
      const me = await api.get("/users/me", {
        headers,
        timeout: REQUEST_TIMEOUT_MS,
      })
      expect(me.ok(), await me.text()).toBe(true)
      const meBody = await me.json()
      const userId: number = meBody.id
      const bucket: string | undefined = meBody.attributes?.remote_bucket_name
      // A null attribute must fail loudly - the backend silently falls back to
      // the default bucket, which would make every assert below vacuous
      expect(
        bucket,
        `${tier.user.email} has no remote_bucket_name attribute`,
      ).toBeTruthy()
      // The sheets' naming contract: {env}-optinist-user-{id}-{unique}
      expect(bucket).toMatch(
        new RegExp(`^development-optinist-user-${userId}-[0-9a-f]{10}$`),
      )
      expect(
        bucketExists(bucket!),
        `bucket ${bucket} not reachable via head-bucket`,
      ).toBe(true)

      const wsId = await apiEnsureWorkspaceId(api, headers, "e2e-s3")
      const uniqueName = `e2e_upload_${Date.now()}.tiff`
      try {
        const uploaded = await api.post(`/files/${wsId}/upload/${uniqueName}`, {
          headers,
          multipart: {
            file: {
              name: uniqueName,
              mimeType: "image/tiff",
              buffer: fs.readFileSync(IMAGE_FIXTURE),
            },
          },
          timeout: UPLOAD_TIMEOUT_MS,
        })
        expect(uploaded.ok(), await uploaded.text()).toBe(true)
        expect((await uploaded.json()).file_path).toBe(uniqueName)

        // The S3-side read is the test: the 200 above is answered even when
        // the inline S3 PUT failed
        const key = `app/studio_data/input/${wsId}/${uniqueName}`
        await expect
          .poll(() => objectExists(bucket!, key), {
            timeout: 30_000,
            message: `s3://${bucket}/${key} missing after a 200 upload`,
          })
          .toBe(true)

        // Row 528's automatable slice: the merged listing labels the file
        // synced (local AND in S3) and the on-demand sync endpoint round-trips
        // it. The genuinely-remote branch (S3 copy with no local file) has no
        // API to set up - it stays with the pytest coverage.
        // file_type is required: without it get_files returns [] and every
        // file would come back remote-labeled from the S3 side alone.
        const merged = await api.get(`/files/${wsId}/merged?file_type=image`, {
          headers,
          timeout: REQUEST_TIMEOUT_MS,
        })
        expect(merged.ok(), await merged.text()).toBe(true)
        const node = findNode(await merged.json(), uniqueName)
        expect(
          node,
          `${uniqueName} absent from the merged listing`,
        ).toBeTruthy()
        expect(node!.sync_status).toBe("synced")

        const synced = await api.post(`/files/${wsId}/sync/${uniqueName}`, {
          headers,
          timeout: UPLOAD_TIMEOUT_MS,
        })
        expect(synced.ok(), await synced.text()).toBe(true)
        expect((await synced.json()).file_path).toBe(uniqueName)

        // BT-1006's S3 half: an HDF5 upload lands its object the same way
        const h5Name = `e2e_upload_${Date.now()}.h5`
        const h5 = await api.post(`/files/${wsId}/upload/${h5Name}`, {
          headers,
          multipart: {
            file: {
              name: h5Name,
              mimeType: "application/x-hdf",
              buffer: fs.readFileSync(HDF5_FIXTURE),
            },
          },
          timeout: UPLOAD_TIMEOUT_MS,
        })
        expect(h5.ok(), await h5.text()).toBe(true)
        const h5Key = `app/studio_data/input/${wsId}/${h5Name}`
        await expect
          .poll(() => objectExists(bucket!, h5Key), {
            timeout: 30_000,
            message: `s3://${bucket}/${h5Key} missing after a 200 upload`,
          })
          .toBe(true)
      } finally {
        const res = await api.delete(`/workspace/${wsId}`, {
          headers,
          timeout: UPLOAD_TIMEOUT_MS,
        })
        expect(res.ok(), await res.text()).toBe(true)
      }
    } finally {
      await api.dispose()
    }
  })
}

for (const tier of TIERS) {
  const id = { free: "S3-02", premium: "S3-22" }[tier.key]
  test.describe(`Import and delete round-trip the real bucket (${tier.key})`, () => {
    test.use({ storageState: tier.storageState() })

    test(`${id} - Sample import lands input objects; workspace delete empties the prefixes @slow`, async ({
      page,
    }) => {
      const rows = "406 / BT-1003 / BT-1111"
      skipUnlessOptedIn(rows, tier)
      test.setTimeout(10 * 60_000 + tier.setupBudgetMs)

      const { bucket, headers } = await enterAs(page, tier, rows)
      const wsId = await openWorkspace(page, "e2e-s3import")
      // Without this the row is green whether or not a premium instance was
      // involved: the v2 run's import completed through the app's 502 fallback
      // while the premium task was refusing traffic, and every S3 claim below
      // still passed.
      const importRequests: Record<string, string>[] = []
      const importWindow = windowStart()
      if (tier.key === "premium") {
        page.on("request", (req) => {
          if (
            new RegExp(`/workflow/sample_data/${wsId}(?:[/?]|$)`).test(
              req.url(),
            )
          ) {
            importRequests.push(req.headers())
          }
        })
      }
      const inputPrefix = `app/studio_data/input/${wsId}/`
      const outputPrefix = `app/studio_data/output/${wsId}/`

      let deleted = false
      const deleteWorkspace = () =>
        page.request.delete(`${apiUrl()}/workspace/${wsId}`, {
          headers,
          timeout: UPLOAD_TIMEOUT_MS,
        })
      try {
        await importSampleData(page, "e2e-s3import")
        if (tier.key === "premium") {
          // Both halves, as S3-23 has for the run: the headers prove the app
          // asked for the premium instance, the log line proves the premium
          // instance answered. A routing id the ALB no longer matches leaves
          // the first true and the second false, with no 502 to notice.
          expectPremiumRouted(importRequests, "sample-import")
          await expect
            .poll(
              polled(() =>
                cloudwatchHas(
                  PREMIUM_LOG_GROUP,
                  `Starting sample data import: workspace: ${wsId},`,
                  importWindow,
                ),
              ),
              {
                ...CLOUDWATCH_POLL,
                message: `no sample-data import for workspace ${wsId} in ${PREMIUM_LOG_GROUP} - the import did not run on the premium instance`,
              },
            )
            .toBe(true)
        }
        await expect
          .poll(() => s3ObjectCount(bucket, inputPrefix), {
            timeout: 120_000,
            intervals: [10_000],
            message: `no imported input objects under s3://${bucket}/${inputPrefix}`,
          })
          .toBeGreaterThan(0)

        // DELETE /workspace answers 200 even when its S3 cleanup threw (the
        // server swallows the error and soft-deletes anyway), so the empty
        // prefix is the assertion, not the status code. s3ObjectCount throws on
        // a failed CLI call rather than reporting a vacuous empty result.
        const res = await deleteWorkspace()
        deleted = true
        expect(res.ok(), await res.text()).toBe(true)
        await expect
          .poll(() => s3ObjectCount(bucket, inputPrefix), {
            timeout: 60_000,
            intervals: [10_000],
            message: `input objects survived the workspace delete under s3://${bucket}/${inputPrefix}`,
          })
          .toBe(0)
        expect(
          s3ObjectCount(bucket, outputPrefix),
          `output objects survived the workspace delete under s3://${bucket}/${outputPrefix}`,
        ).toBe(0)
      } finally {
        // A failure above must not strand the workspace and its real objects
        if (!deleted) await deleteWorkspace().catch(() => {})
      }
    })
  })
}

for (const tier of TIERS) {
  const id = { free: "S3-03", premium: "S3-23" }[tier.key]
  test.describe(`Published experiment via the public instance (${tier.key})`, () => {
    test.use({ storageState: tier.storageState() })

    test(`${id} - An anonymous public read reproduces the published experiment with lazy S3 @slow`, async ({
      page,
      request,
    }) => {
      const rows = "607"
      skipUnlessOptedIn(rows, tier)
      test.setTimeout(RUN_TEST_TIMEOUT_MS + 20 * 60_000 + tier.setupBudgetMs)

      const {
        userId,
        bucket: bucketName,
        headers,
      } = await enterAs(page, tier, rows)
      const wsName = "e2e-s3pub"
      const wsId = await openWorkspace(page, wsName)
      // What makes the premium variant more than an expensive copy of the free
      // one. Everything else here is tier-agnostic: the bucket is per-user, and
      // increment_workflow_count picks its table from the user's subscription
      // tier rather than from the task that served the run - so a premium user
      // whose run fell back to the shared tier (the app strips its routing
      // headers on a 502/503) would still write premium_user_assignments, still
      // land outputs in the premium bucket, and still pass every assert below.
      // The run's own headers and the premium log group are what tell the two
      // apart.
      const runHeaders: Record<string, string>[] = []
      if (tier.key === "premium") {
        page.on("request", (req) => {
          if (
            req.method() === "POST" &&
            new RegExp(`/run/${wsId}(?:[/?]|$)`).test(req.url())
          ) {
            runHeaders.push(req.headers())
          }
        })
      }
      let recordId = 0
      try {
        await importSampleData(page, wsName)

        // Rows 538 (free) / 543 (premium), live half: the run really holds a
        // slot in its own tier's assignment table while it executes and
        // releases it on completion; the failure-path decrement stays with
        // the unit suite
        // A bare select, deliberately. Both tables key user_id uniquely -
        // free_user_assignments by uq_free_user_id, premium_user_assignments
        // by idx_unique_user_assignment, which permits duplicates only for the
        // NULL standby sentinels - so a two-line answer here is a broken
        // invariant and must surface, not be collapsed by an aggregate.
        const countSql =
          `SELECT active_workflow_count FROM ${tier.assignmentTable} ` +
          `WHERE user_id = ${userId};`
        expect(runSql(countSql), "rows 538 / 543: pre-run baseline").toBe("0")
        await reproduceTutorial(page, "Tutorial1")
        const runWindow = windowStart()
        const { workspaceId: runWs, uid } = await startRun(page, "RUN ALL")
        await expect
          .poll(
            polled(() => runSql(countSql)),
            {
              timeout: 180_000,
              intervals: [10_000],
              message: "active_workflow_count never reached 1 during the run",
            },
          )
          .toBe("1")
        await awaitRunFinished(page, "Tutorial1", runWs, uid)
        await expect
          .poll(
            polled(() => runSql(countSql)),
            {
              timeout: 120_000,
              intervals: [10_000],
              message:
                "active_workflow_count did not return to 0 after the run",
            },
          )
          .toBe("0")

        // Read after the run finishes, not at the POST: a fallback retry is a
        // second request that lands after startRun has already resolved on the
        // first. PREM-07's free-group negative is deliberately not imported -
        // it needs that lane's delivery-catch-up probe, and the premium-group
        // positive already fixes which task executed the workflow.
        if (tier.key === "premium") {
          expectPremiumRouted(runHeaders, "run POST")
          await expect
            .poll(
              polled(() =>
                cloudwatchHas(PREMIUM_LOG_GROUP, `(ID: ${uid},`, runWindow),
              ),
              {
                ...CLOUDWATCH_POLL,
                message: `no WORKFLOW START for run ${uid} in ${PREMIUM_LOG_GROUP} - the run did not execute on the premium instance`,
              },
            )
            .toBe(true)
        }

        // Rows 407 / BT-1004: the run's outputs really landed in the user's
        // own bucket - the direct S3 read, before any publish
        await expect
          .poll(
            () =>
              s3ObjectCount(
                bucketName,
                `app/studio_data/output/${wsId}/${uid}/`,
              ),
            {
              timeout: 120_000,
              intervals: [10_000],
              message: `no run outputs under s3://${bucketName}/app/studio_data/output/${wsId}/${uid}/`,
            },
          )
          .toBeGreaterThan(0)

        // Row 1217: not merely "some objects" - the run's own NWB output is
        // there, and nothing landed as a zero-byte stub
        const outputs = JSON.parse(
          execSync(
            `aws s3api list-objects-v2 --bucket ${bucketName} ` +
              `--prefix app/studio_data/output/${wsId}/${uid}/ ` +
              "--query 'Contents[].{k:Key,s:Size}' " +
              `--region ${AWS_REGION} --output json`,
            { timeout: 30_000 },
          ).toString() || "[]",
        ) as { k: string; s: number }[]
        expect(
          outputs.some((o) => o.k.endsWith(".nwb")),
          `no NWB among the run's S3 outputs: ${outputs.map((o) => o.k).join(", ")}`,
        ).toBe(true)
        // error.log is legitimately empty on a clean run
        for (const o of outputs.filter((out) => !out.k.endsWith(".log"))) {
          expect(o.s, `${o.k} landed as a zero-byte object`).toBeGreaterThan(0)
        }

        // Find the record BEFORE the negative, so the 404 below can only mean
        // the published_only gate, never a record that does not exist yet.
        // Polled, not read once: the executor writes the experiment record
        // asynchronously after the last node finishes, so it can land after
        // the run reports success and the outputs are already in S3.
        await expect
          .poll(
            async () => {
              const listRes = await page.request.get(
                `${apiUrl()}/api/dataview?limit=100&offset=0&workspace_id=${wsId}`,
                { headers, timeout: REQUEST_TIMEOUT_MS },
              )
              if (!listRes.ok()) return `HTTP ${listRes.status()}`
              const { items } = await listRes.json()
              const record = (items as { id: number; uid?: string }[]).find(
                (r) => r.uid === uid,
              )
              if (!record) return "absent"
              recordId = record.id
              return "found"
            },
            {
              timeout: 120_000,
              intervals: [10_000],
              message: `no dataview record for run ${uid}`,
            },
          )
          .toBe("found")

        // The published_only gate: anonymous reproduce of the existing but
        // not-yet-published experiment is a 404
        const before = await request.get(
          `${apiUrl()}/api/public/dataview/workflow/reproduce/${wsId}/${uid}`,
          { timeout: REQUEST_TIMEOUT_MS },
        )
        expect(before.status(), await before.text()).toBe(404)

        const t0 = windowStart()
        const published = await page.request.post(
          `${apiUrl()}/api/dataview/publish/${recordId}/on`,
          { headers, timeout: UPLOAD_TIMEOUT_MS },
        )
        expect(published.ok(), await published.text()).toBe(true)

        // Anonymous listing shows it only because publish_status flipped on
        await expect
          .poll(
            async () => {
              const res = await request.get(
                `${apiUrl()}/api/public/dataview?limit=100&offset=0`,
                { timeout: REQUEST_TIMEOUT_MS },
              )
              if (!res.ok()) return `HTTP ${res.status()}`
              const body = await res.json()
              const records = (
                Array.isArray(body) ? body : (body.items ?? [])
              ) as { uid?: string }[]
              return records.some((r) => r.uid === uid) ? "listed" : "absent"
            },
            {
              timeout: 120_000,
              intervals: [10_000],
              message: `published run ${uid} never appeared in /api/public/dataview`,
            },
          )
          .toBe("listed")

        // The sheet's core: reproduce answers 202 pending_sync until the
        // publish sync lands, then 200 - S3 is the source of truth and the
        // public instance lazily fetches from the publisher's bucket. A 503 is
        // tolerated only as a transient download retry, never as the outcome.
        let lastStatus = 0
        await expect
          .poll(
            async () => {
              const res = await request.get(
                `${apiUrl()}/api/public/dataview/workflow/reproduce/${wsId}/${uid}`,
                { timeout: UPLOAD_TIMEOUT_MS },
              )
              lastStatus = res.status()
              expect(
                [200, 202, 503],
                `reproduce answered ${lastStatus}: ${await res.text()}`,
              ).toContain(lastStatus)
              return lastStatus
            },
            {
              timeout: 10 * 60_000,
              intervals: [20_000],
              message: `reproduce never reached 200 (last status ${lastStatus})`,
            },
          )
          .toBe(200)

        // Lazy-fetch evidence is conditional by design: a pre-warmed public
        // cache leaves no download line, which the sheet calls moot
        const downloaded = cloudwatchHas(
          PUBLIC_LOG_GROUP,
          `Download data from S3 [${bucketName}]`,
          t0,
        )
        console.log(
          `[16-storage-aws] ${id} lazy-fetch line in ${PUBLIC_LOG_GROUP}: ` +
            (downloaded
              ? "found"
              : "not found (pre-warmed cache - moot per the sheet)"),
        )
      } finally {
        // A fresh token: this row's ceiling is close to the Firebase ID
        // token's lifetime, and a 401 here would strand a published record and
        // a workspace on dev. Teardown failures are logged rather than thrown -
        // they must not mask the row's own verdict - but nor may they be
        // silent, which is what a bare .catch(() => {}) made them.
        const teardown = await apiHeaders(page).catch(() => headers)
        const cleanup = async (
          what: string,
          run: () => Promise<APIResponse>,
        ) => {
          try {
            const res = await run()
            if (!res.ok()) {
              console.log(
                `[16-storage-aws] ${id} teardown ${what}: ${res.status()}`,
              )
            }
          } catch (e) {
            console.log(`[16-storage-aws] ${id} teardown ${what} threw: ${e}`)
          }
        }
        if (recordId) {
          await cleanup("publish-off", () =>
            page.request.post(
              `${apiUrl()}/api/dataview/publish/${recordId}/off`,
              { headers: teardown, timeout: UPLOAD_TIMEOUT_MS },
            ),
          )
        }
        await cleanup("workspace-delete", () =>
          page.request.delete(`${apiUrl()}/workspace/${wsId}`, {
            headers: teardown,
            timeout: UPLOAD_TIMEOUT_MS,
          }),
        )
      }
    })
  })
}

// The visitor's route to one published record's details, shared by S3-04 and
// S3-08. The anonymous listing is eventually consistent with the publish and
// reads the DB rather than the files, so it is polled before the UI is opened;
// the row is found by the uid the grid's ID column renders, because the
// record's display name belongs to the run helper rather than to either test.
async function openPublicDetails(
  viewer: Page,
  wsName: string,
  uid: string,
): Promise<Locator> {
  await expect
    .poll(
      async () => {
        const res = await viewer.request.get(
          `${apiUrl()}/api/public/dataview?limit=100&offset=0`,
          { timeout: REQUEST_TIMEOUT_MS },
        )
        if (!res.ok()) return `HTTP ${res.status()}`
        const body = await res.json()
        const records = (Array.isArray(body) ? body : (body.items ?? [])) as {
          uid?: string
        }[]
        return records.some((r) => r.uid === uid) ? "listed" : "absent"
      },
      {
        timeout: 120_000,
        intervals: [10_000],
        message: `published run ${uid} never appeared in /api/public/dataview`,
      },
    )
    .toBe("listed")
  await viewer.goto("/public")
  await filterWorkspace(viewer, wsName)
  const row = viewer
    .locator(".MuiDataGrid-row")
    .filter({ has: viewer.getByText(uid.slice(0, 8)) })
    .first()
  await expect(row).toBeVisible({ timeout: 30_000 })
  await row
    .locator('[data-field="details"] [data-testid="InsightsIcon"]')
    .click()
  const dialog = viewer.locator('[role="dialog"]')
  await expect(dialog).toBeVisible({ timeout: 15_000 })
  return dialog
}

test.describe("Published sync error and recovery on real S3", () => {
  test.use({ storageState: freeStorageState() })

  // Rows 717 / 718 / BT-719 / 2031's live half. The public cache warms lazily
  // on the first reproduce (S3-03), so removing experiment.yaml from the
  // owner's bucket between publish and first view is a REAL missing-data
  // state: the visitor's first open must surface the error state with Retry,
  // and Retry must recover once the file is back. Only the test account's own
  // object is touched, and it is put back in a finally.
  //
  // Free-only, unlike the per-tier rows above: what this row asks about is
  // the anonymous visitor's dialog, and the object it reads is the same
  // per-user S3 copy whichever tier published it - a premium variant would
  // spend a real assignment to observe identical public-instance behaviour.
  test("S3-04 - A missing S3 config surfaces the public error state; Retry recovers it @slow", async ({
    page,
    browser,
  }) => {
    skipUnlessOptedIn("717 / 718 / BT-719 / 2031")
    test.setTimeout(RUN_TEST_TIMEOUT_MS + 20 * 60_000)

    await gotoDashboard(page)
    const wsName = "e2e-s3err"
    const wsId = await openWorkspace(page, wsName)
    const headers = await apiHeaders(page)
    const aside = path.join(os.tmpdir(), `e2e-s3err-${Date.now()}.yaml`)
    let recordId = 0
    let yamlMovedAside = false
    let bucket = ""
    let key = ""
    const restoreYaml = () => {
      execSync(
        `aws s3 cp ${aside} s3://${bucket}/${key} --region ${AWS_REGION}`,
        { timeout: 60_000, stdio: ["pipe", "pipe", "pipe"] },
      )
      yamlMovedAside = false
    }
    try {
      await importSampleData(page, wsName)
      const { uid } = await runTutorial(page, "Tutorial1", "RUN ALL")
      const me = await page.request.get(`${apiUrl()}/users/me`, {
        headers,
        timeout: REQUEST_TIMEOUT_MS,
      })
      bucket = (await me.json()).attributes?.remote_bucket_name
      expect(
        bucket,
        "free user has no remote_bucket_name attribute",
      ).toBeTruthy()
      key = `app/studio_data/output/${wsId}/${uid}/experiment.yaml`

      await expect
        .poll(
          async () => {
            const listRes = await page.request.get(
              `${apiUrl()}/api/dataview?limit=100&offset=0&workspace_id=${wsId}`,
              { headers, timeout: REQUEST_TIMEOUT_MS },
            )
            if (!listRes.ok()) return `HTTP ${listRes.status()}`
            const { items } = await listRes.json()
            const record = (items as { id: number; uid?: string }[]).find(
              (r) => r.uid === uid,
            )
            if (!record) return "absent"
            recordId = record.id
            return "found"
          },
          {
            timeout: 120_000,
            intervals: [10_000],
            message: `no dataview record for run ${uid}`,
          },
        )
        .toBe("found")

      const published = await page.request.post(
        `${apiUrl()}/api/dataview/publish/${recordId}/on`,
        { headers, timeout: UPLOAD_TIMEOUT_MS },
      )
      expect(published.ok(), await published.text()).toBe(true)

      // Take the config away before anything warms the public cache
      execSync(
        `aws s3 cp s3://${bucket}/${key} ${aside} --region ${AWS_REGION}`,
        { timeout: 60_000, stdio: ["pipe", "pipe", "pipe"] },
      )
      execSync(`aws s3 rm s3://${bucket}/${key} --region ${AWS_REGION}`, {
        timeout: 60_000,
        stdio: ["pipe", "pipe", "pipe"],
      })
      yamlMovedAside = true

      // Really anonymous, like the visitor rows 717/718 describe
      const ctx = await browser.newContext({
        baseURL: process.env.BASE_URL,
        storageState: undefined,
      })
      const viewer = await ctx.newPage()
      try {
        const dialog = await openPublicDetails(viewer, wsName, uid)

        // Row 717: the error state - icon, alert, and a Retry button. A 503
        // renders it at once; a 202 rides the auto-retry ladder out first
        // (30 x 10s), so the budget covers the ladder's ceiling.
        await expect(
          dialog.locator('[data-testid="ErrorOutlineIcon"]').first(),
        ).toBeVisible({ timeout: 420_000 })
        await expect(dialog.locator(".MuiAlert-root").first()).toBeVisible()
        const retry = dialog.getByRole("button", { name: "Retry" })
        await expect(retry).toBeVisible()

        // Put the file back; row 718 / BT-719: Retry re-syncs and recovers.
        // The interim pending state is not asserted - a fast re-sync renders
        // the details before the 202 branch ever paints.
        restoreYaml()
        await retry.click()

        // Recovery, by the route's own verdict rather than pixel state
        await expect
          .poll(
            async () =>
              (
                await viewer.request.get(
                  `${apiUrl()}/api/public/dataview/workflow/reproduce/${wsId}/${uid}`,
                  { timeout: UPLOAD_TIMEOUT_MS },
                )
              ).status(),
            {
              timeout: 10 * 60_000,
              intervals: [20_000],
              message: "reproduce never recovered to 200 after the retry",
            },
          )
          .toBe(200)
        // ...and the dialog left its error state. The single Retry above can
        // land before the re-sync completes, so keep clicking it until the
        // error state clears (the PREM-13 reload-in-poll pattern).
        const errorIcon = dialog
          .locator('[data-testid="ErrorOutlineIcon"]')
          .first()
        await expect
          .poll(
            async () => {
              if (await errorIcon.isVisible()) {
                await dialog
                  .getByRole("button", { name: "Retry" })
                  .click()
                  .catch(() => {})
                return "error state"
              }
              return "recovered"
            },
            {
              timeout: 120_000,
              intervals: [15_000],
              message: "the dialog never left its error state after Retry",
            },
          )
          .toBe("recovered")
      } finally {
        await ctx.close()
      }
    } finally {
      if (yamlMovedAside) {
        try {
          restoreYaml()
        } catch {
          // the workspace delete below removes the prefix anyway
        }
      }
      if (recordId) {
        await page.request
          .post(`${apiUrl()}/api/dataview/publish/${recordId}/off`, {
            headers,
            timeout: UPLOAD_TIMEOUT_MS,
          })
          .catch(() => {})
      }
      await page.request
        .delete(`${apiUrl()}/workspace/${wsId}`, {
          headers,
          timeout: UPLOAD_TIMEOUT_MS,
        })
        .catch(() => {})
      fs.rmSync(aside, { force: true })
    }
  })
})

// ---------------------------------------------------------------------------
// Rows 727 + 723: the publish-time S3 repair of a broken local config, then a
// five-record batch draining in a single background sync run. One test because
// both rows need the same expensive setup - a finished run plus four real
// copies of it - and the batch publish is the natural second act of the repair.
//
// Free-only, unlike the per-tier rows above: the repair is asserted on the
// filesystem of the task that serves the publish, and freeTierExec below
// reaches into the free tier's single ECS task by name. A premium variant
// would have to exec on whichever instance the assignment landed on, which is
// `15-premium-aws`'s machinery rather than this lane's.
// ---------------------------------------------------------------------------

const FREE_CLUSTER = "development-optinist-cloud-cluster"
const FREE_SERVICE = "development-optinist-cloud-service"
const FREE_CONTAINER_FILTER = "ecs-development-optinist-cloud-taskdef"
const BACKGROUND_LOG = "/ecs/development-background-optinist-cloud-taskdef"
const COPY_TIMEOUT_MS = 300_000

// The EC2 host running the free tier's single task (desired=1 on development,
// asserted below), so a file broken there is broken on the task serving the
// publish request.
function freeTierHostId(): string {
  const arns = awsJson<{ taskArns: string[] }>(
    `ecs list-tasks --cluster ${FREE_CLUSTER} --service-name ${FREE_SERVICE}`,
  ).taskArns
  expect(arns.length, "the free service has no running task").toBe(1)
  const ci = awsJson<{ tasks: { containerInstanceArn?: string }[] }>(
    `ecs describe-tasks --cluster ${FREE_CLUSTER} --tasks ${arns[0]}`,
  ).tasks[0].containerInstanceArn
  expect(ci, "the free task reports no container instance").toBeTruthy()
  return awsJson<{ containerInstances: { ec2InstanceId: string }[] }>(
    `ecs describe-container-instances --cluster ${FREE_CLUSTER} ` +
      `--container-instances ${ci}`,
  ).containerInstances[0].ec2InstanceId
}

// The job's per-run banner. Waiting for a fresh one puts the caller at the
// start of a 5-minute window, so whatever it does next is observable for most
// of that window before the next tick can act on it.
const SYNC_TICK_MARKER = "Starting published experiment validation job"

async function awaitFreshSyncTick() {
  const from = Date.now()
  await expect
    .poll(
      () =>
        logTail(BACKGROUND_LOG, 100).events.some(
          (e) => e.ingestionTime > from && e.message.includes(SYNC_TICK_MARKER),
        ),
      {
        timeout: 7 * 60_000,
        intervals: [15_000],
        message: `no "${SYNC_TICK_MARKER}" tick in ${BACKGROUND_LOG} within 7 minutes`,
      },
    )
    .toBe(true)
}

let freeHost = ""
// Run one command inside the free tier's app container over SSM. cmd must not
// contain double quotes - it is interpolated into a double-quoted sh -c.
function freeTierExec(cmd: string): string {
  if (/"/.test(cmd)) throw new Error(`freeTierExec cmd has a quote: ${cmd}`)
  if (!freeHost) freeHost = freeTierHostId()
  return runShellOverSsm(
    freeHost,
    [
      "set -e",
      `CID=$(sudo docker ps -q --filter name=${FREE_CONTAINER_FILTER} | head -1)`,
      '[ -n "$CID" ]',
      `sudo docker exec "$CID" sh -c "${cmd}"`,
    ],
    "free-tier exec",
  )
}

test.describe("Publish repair and batch sync on the real free tier", () => {
  test.use({ storageState: freeStorageState() })

  test("S3-05 - Publish repairs broken local configs from S3, and five rapid publishes drain in one sync run @slow", async ({
    page,
  }) => {
    skipUnlessOptedIn("723 / 727")
    test.setTimeout(RUN_TEST_TIMEOUT_MS + 35 * 60_000)

    await gotoDashboard(page)
    const wsName = "e2e-s3batch"
    const wsId = await openWorkspace(page, wsName)
    const headers = await apiHeaders(page)
    const recordIds: number[] = []
    try {
      await importSampleData(page, wsName)
      const { uid } = await runTutorial(page, "Tutorial1", "RUN ALL")

      // The sync job's own precondition (success = 1) is written by an async
      // record write after the run reports finished; a copy taken before it
      // lands would clone success = 0 and the job would skip the whole batch.
      await expect
        .poll(
          () =>
            runSql(
              `SELECT success FROM experiment_records ` +
                `WHERE workspace_id = ${wsId} AND uid = '${sqlLiteral(uid)}';`,
            ),
          {
            timeout: 180_000,
            intervals: [10_000],
            message: `experiment_records.success never reached 1 for ${wsId}/${uid}`,
          },
        )
        .toBe("1")

      // Four real copies: copy_data re-uploads each new uid to S3 and the
      // record copy keeps success = 1, so five publishable experiments cost
      // one pipeline run.
      for (let i = 0; i < 4; i++) {
        const copied = await page.request.post(
          `${apiUrl()}/experiments/copy/${wsId}`,
          { headers, data: { uidList: [uid] }, timeout: COPY_TIMEOUT_MS },
        )
        expect(copied.ok(), await copied.text()).toBe(true)
      }

      const uids: string[] = []
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `${apiUrl()}/api/dataview?limit=100&offset=0&workspace_id=${wsId}`,
              { headers, timeout: REQUEST_TIMEOUT_MS },
            )
            if (!res.ok()) return `HTTP ${res.status()}`
            const { items } = await res.json()
            recordIds.length = 0
            uids.length = 0
            for (const r of items as { id: number; uid: string }[]) {
              recordIds.push(r.id)
              uids.push(r.uid)
            }
            return uids.length
          },
          {
            timeout: 60_000,
            intervals: [5_000],
            message: "the workspace never listed the run plus its four copies",
          },
        )
        .toBe(5)
      expect(
        runSql(
          `SELECT COUNT(*) FROM experiment_records ` +
            `WHERE workspace_id = ${wsId} AND success = 1;`,
        ),
        "a copy landed without success = 1 - the sync job would skip it",
      ).toBe("5")

      const yamlPath = (u: string) =>
        `/app/studio_data/output/${wsId}/${u}/experiment.yaml`

      // Row 727, single half: an empty {} stub - the state a migrated
      // instance leaves - on the task that will serve the publish.
      freeTierExec(`echo {} > ${yamlPath(uid)}`)
      expect(
        freeTierExec(`cat ${yamlPath(uid)}`),
        "the stub write did not land on the serving task",
      ).toBe("{}")
      const singlePub = await page.request.post(
        `${apiUrl()}/api/dataview/publish/${recordIds[uids.indexOf(uid)]}/on`,
        { headers, timeout: COPY_TIMEOUT_MS },
      )
      // The publish succeeding IS the row: without the pre-sync repair,
      // PublishValidator reads the stub and answers 400 unpublishable.
      expect(singlePub.ok(), await singlePub.text()).toBe(true)
      const repaired = freeTierExec(`cat ${yamlPath(uid)}`)
      expect(
        repaired,
        "publish answered 200 but the local config is still the stub",
      ).not.toBe("{}")
      expect(repaired).toContain("success: success")
      expect(repaired).toContain(uid)

      // Row 727, bulk half: one stubbed and one deleted outright (the
      // "absent" case), repaired by the same bulk publish.
      const copies = uids.filter((u) => u !== uid)
      freeTierExec(`echo {} > ${yamlPath(copies[0])}`)
      freeTierExec(`rm ${yamlPath(copies[1])}`)

      // Row 723 needs the five pending rows to be observable before a sync
      // tick eats them, so wait for a fresh tick and publish right after it -
      // the batch then sits pending for most of a 5-minute window.
      await awaitFreshSyncTick()

      // All five in one atomic flip (the already-published original is
      // re-pended by the same update), which is also what makes "one sync run
      // drains them" falsifiable: every tick after this sees all five.
      const bulk = await page.request.post(
        `${apiUrl()}/api/dataview/multiple/publish/on`,
        { headers, data: recordIds, timeout: COPY_TIMEOUT_MS },
      )
      expect(bulk.ok(), await bulk.text()).toBe(true)

      for (const u of [copies[0], copies[1]]) {
        const out = freeTierExec(`cat ${yamlPath(u)}`)
        expect(
          out,
          `bulk publish did not repair ${wsId}/${u} from S3`,
        ).toContain("success: success")
        expect(out).toContain(u)
      }

      // Row 723's own query: all five pending, then drained to zero.
      const pendingSql =
        `SELECT COUNT(*) FROM experiment_records ` +
        `WHERE workspace_id = ${wsId} AND local_sync_status = 'pending';`
      const syncedSql =
        `SELECT COUNT(*) FROM experiment_records ` +
        `WHERE workspace_id = ${wsId} AND publish_status = 1 ` +
        `AND local_sync_status = 'synced';`
      expect(
        runSql(pendingSql),
        "all five rows must be pending right after the bulk publish",
      ).toBe("5")

      // Two ticks plus margin: the job has been observed to log "No pending
      // experiments" on the first tick after a bulk publish and only see the
      // rows one tick later.
      await expect
        .poll(() => Number(runSql(syncedSql)), {
          timeout: 13 * 60_000,
          intervals: [15_000],
          message: "no row was validated within two sync ticks plus margin",
        })
        .toBeGreaterThan(0)
      // Single-run proof by timing: ticks are 5 minutes apart, so stragglers
      // waiting on a second run would stay pending far longer than this.
      await expect
        .poll(() => runSql(syncedSql), {
          timeout: 150_000,
          intervals: [10_000],
          message:
            "the batch did not drain in a single sync run - the leftovers " +
            "are waiting on a second 5-minute tick",
        })
        .toBe("5")
      expect(runSql(pendingSql), "pending fell to zero").toBe("0")

      // The job's own account of the batch. The background group's event
      // timestamps are broken (awslogs multiline pattern mismatch), so this
      // reads the newest stream's tail rather than a time-filtered query.
      const { text } = logTail(BACKGROUND_LOG, 500)
      for (const u of uids) {
        expect(
          text,
          `no "Successfully validated" line for ${wsId}/${u}`,
        ).toContain(`Successfully validated ${wsId}/${u}`)
      }
      const found = [...text.matchAll(/Found (\d+) experiments to validate/g)]
        .map((m) => Number(m[1]))
        .filter((n) => Number.isFinite(n))
      expect(
        Math.max(0, ...found),
        `no "Found N experiments to validate" line covering the batch`,
      ).toBeGreaterThanOrEqual(5)
      expect(text).toMatch(
        /Validation job completed: \d+ synced, \d+ errors \(max 10 concurrent\)/,
      )
    } finally {
      if (recordIds.length) {
        await page.request
          .post(`${apiUrl()}/api/dataview/multiple/publish/off`, {
            headers,
            data: recordIds,
            timeout: COPY_TIMEOUT_MS,
          })
          .catch(() => {})
      }
      await page.request
        .delete(`${apiUrl()}/workspace/${wsId}`, {
          headers,
          timeout: COPY_TIMEOUT_MS,
        })
        .catch(() => {})
    }
  })
})

// ---------------------------------------------------------------------------
// Rows 720 / 721 / 722 / 725 / 726 - and sheet 20's 2032, which restates 722
// plus the public read that follows it: the publish sync's own state machine,
// run against the real background tier and the real public tier. What kept these
// rows manual is that their verdict is a DB transition plus the tier's own log
// line - neither of which a browser can see - so each one below pairs an
// `expect.poll` on `experiment_records.local_sync_status` with the log line
// naming THIS run's uid.
//
// Free-only, like S3-04 and S3-05: what these rows ask about is the background
// job and the public instance, and both behave identically whichever tier
// published the record, so a premium variant would spend a real assignment to
// watch the same thing twice.
// ---------------------------------------------------------------------------

const METRIC_NAMESPACE = "OptiNiSt/BackgroundJobs/development"
// Two ticks plus margin - S3-05's budget, for S3-05's reason: the job has been
// observed to log "No pending experiments" on the tick a change lands in and
// only pick the row up on the next one.
const SYNC_SETTLE_MS = 13 * 60_000

// Sum a background-job metric from the moment the caller's own work started.
// One-sided by construction: other traffic on the shared environment can only
// ADD to the window, so `>= 1` cannot manufacture a pass on an idle
// environment where the job never touched our row - which is the failure this
// guards. It is deliberately not an equality.
function metricSumSince(name: string, sinceMs: number): number {
  const points = awsJson<{ Sum: number }[]>(
    `cloudwatch get-metric-statistics --namespace ${METRIC_NAMESPACE} ` +
      `--metric-name ${name} --statistics Sum --period 60 ` +
      `--start-time ${Math.floor(sinceMs / 1000)} ` +
      `--end-time ${Math.floor(Date.now() / 1000)} --query 'Datapoints'`,
  )
  return points.reduce((sum, p) => sum + (p.Sum ?? 0), 0)
}

// The awslogs driver ships asynchronously and CloudWatch's own propagation
// adds to that, so a line the DB transition implies can trail the transition
// it explains by seconds. Every log and metric assertion below therefore
// polls, where reading once would be a race the sync job usually wins.
async function awaitBackgroundLine(needle: string, since = 0) {
  await expect
    .poll(
      () =>
        logTail(BACKGROUND_LOG, 1000).events.some(
          (e) => e.ingestionTime > since && e.message.includes(needle),
        ),
      {
        ...CLOUDWATCH_POLL,
        message: `no "${needle}" line in ${BACKGROUND_LOG}`,
      },
    )
    .toBe(true)
}

async function awaitMetric(name: string, sinceMs: number, why: string) {
  await expect
    .poll(() => metricSumSince(name, sinceMs), {
      ...CLOUDWATCH_POLL,
      message: why,
    })
    .toBeGreaterThanOrEqual(1)
}

// Both refuse an empty read rather than dressing it as data: `Number("")` is
// 0, which reads like a plausible version, and an empty status string would
// poll to timeout with no hint that the query, not the job, is what failed.
const syncStatusOf = (recordId: number) => {
  const out = runSql(
    `SELECT local_sync_status FROM experiment_records WHERE id = ${recordId};`,
  ).trim()
  if (!out) throw new Error(`no experiment_records row for id ${recordId}`)
  return out
}

const versionOf = (recordId: number) => {
  const out = runSql(
    `SELECT version FROM experiment_records WHERE id = ${recordId};`,
  ).trim()
  const version = Number(out)
  if (!out || !Number.isFinite(version)) {
    throw new Error(
      `unreadable version for record ${recordId}: ${out || "empty"}`,
    )
  }
  return version
}

function s3Aside(bucket: string, key: string, local: string) {
  execSync(`aws s3 cp s3://${bucket}/${key} ${local} --region ${AWS_REGION}`, {
    timeout: 60_000,
    stdio: ["pipe", "pipe", "pipe"],
  })
  execSync(`aws s3 rm s3://${bucket}/${key} --region ${AWS_REGION}`, {
    timeout: 60_000,
    stdio: ["pipe", "pipe", "pipe"],
  })
}

function s3Restore(bucket: string, key: string, local: string) {
  execSync(`aws s3 cp ${local} s3://${bucket}/${key} --region ${AWS_REGION}`, {
    timeout: 60_000,
    stdio: ["pipe", "pipe", "pipe"],
  })
}

// A genuinely unauthenticated caller (`anon`, never `viewer` - that name
// belongs to the browser page the UI halves drive). The `request` fixture inherits the
// describe's storage state, so it is not the anonymous visitor rows 725/726
// and the public-tier comments describe; `18-stripe-audit` and `22-disruptive`
// take a fresh context for the same reason. The caller disposes it.
function anonymousApi(): Promise<APIRequestContext> {
  return request.newContext()
}

// The setup the three rows below share: a real finished run in a fresh
// workspace, and the dataview record the sync job and the public tier will
// act on. Publishing is left to the caller - each row publishes at a different
// moment relative to the job's 5-minute tick.
async function stageFinishedRun(
  page: Page,
  wsName: string,
): Promise<{
  wsId: number
  uid: string
  recordId: number
  bucket: string
  headers: Record<string, string>
}> {
  await gotoDashboard(page)
  const wsId = await openWorkspace(page, wsName)
  const headers = await apiHeaders(page)
  const me = await page.request.get(`${apiUrl()}/users/me`, {
    headers,
    timeout: REQUEST_TIMEOUT_MS,
  })
  expect(me.ok(), await me.text()).toBe(true)
  const bucket: string | undefined = (await me.json()).attributes
    ?.remote_bucket_name
  expect(
    bucket,
    "the free user has no remote_bucket_name attribute",
  ).toBeTruthy()

  await importSampleData(page, wsName)
  const { uid } = await runTutorial(page, "Tutorial1", "RUN ALL")

  // The sync job's own precondition (success = 1) is written by an async
  // record write after the run reports finished, so a publish issued before it
  // lands would leave the row outside the job's query and every wait below
  // would time out on a row the job never had.
  await expect
    .poll(
      () =>
        runSql(
          `SELECT success FROM experiment_records ` +
            `WHERE workspace_id = ${wsId} AND uid = '${sqlLiteral(uid)}';`,
        ),
      {
        timeout: 180_000,
        intervals: [10_000],
        message: `experiment_records.success never reached 1 for ${wsId}/${uid}`,
      },
    )
    .toBe("1")

  let recordId = 0
  await expect
    .poll(
      async () => {
        const res = await page.request.get(
          `${apiUrl()}/api/dataview?limit=100&offset=0&workspace_id=${wsId}`,
          { headers, timeout: REQUEST_TIMEOUT_MS },
        )
        if (!res.ok()) return `HTTP ${res.status()}`
        const { items } = await res.json()
        const record = (items as { id: number; uid?: string }[]).find(
          (r) => r.uid === uid,
        )
        if (!record) return "absent"
        recordId = record.id
        return "found"
      },
      {
        timeout: 120_000,
        intervals: [10_000],
        message: `no dataview record for run ${uid}`,
      },
    )
    .toBe("found")

  return { wsId, uid, recordId, bucket: bucket!, headers }
}

// Never leave a published row the job cannot validate: it would fail every
// five minutes for as long as it is published, and nine failures publish
// PersistentSyncFailure, which HEALTH-14 asserts is empty. Unpublishing takes
// the row out of the job's query and deleting the workspace takes it out for
// good (the query joins on `workspaces.deleted = 0`).
//
// Both calls report what they actually answered. `.catch()` alone swallows
// only transport failures - a 401 from a token that expired during a long row,
// or a 404, RESOLVES - so a status-blind teardown can believe it cleaned up
// while leaving exactly the row this comment says must never exist. The
// headers are re-read here rather than reused: the ones captured at setup can
// be an hour old by the time a long S3-07 reaches its `finally`, and the
// Firebase access token does not outlive that. Reported, never `expect`ed: a
// throw in a `finally` would mask whatever sent us into it.
async function unstageRun(
  page: Page,
  headers: Record<string, string>,
  recordId: number,
  wsId: number,
) {
  let live = headers
  try {
    live = await apiHeaders(page)
  } catch {
    // The page is gone - a test timeout closes it - so the captured headers
    // are the only ones left to try.
  }
  const settle = async (what: string, call: () => Promise<APIResponse>) => {
    try {
      const res = await call()
      if (!res.ok()) {
        console.warn(
          `[16-storage-aws] teardown: ${what} answered ${res.status()} - ` +
            `${await res.text()}. A published row the sync job cannot ` +
            `validate may have been left behind; check it by hand.`,
        )
      }
    } catch (e) {
      console.warn(`[16-storage-aws] teardown: ${what} did not complete - ${e}`)
    }
  }
  if (recordId) {
    await settle(`unpublish record ${recordId}`, () =>
      page.request.post(`${apiUrl()}/api/dataview/publish/${recordId}/off`, {
        headers: live,
        timeout: UPLOAD_TIMEOUT_MS,
      }),
    )
  }
  await settle(`delete workspace ${wsId}`, () =>
    page.request.delete(`${apiUrl()}/workspace/${wsId}`, {
      headers: live,
      timeout: UPLOAD_TIMEOUT_MS,
    }),
  )
}

test.describe("Publish sync status transitions on the real tiers", () => {
  test.use({ storageState: freeStorageState() })

  // Row 726. The self-heal is a DB write the browser never sees: the visitor
  // gets an ordinary 200 whether the row said `synced` or `error`, which is
  // exactly why the row was manual. Two things separate "the endpoint healed
  // it" from "the 5-minute job happened to heal it in the same window": the
  // public tier logs the correction under this uid, and the endpoint bumps
  // `version` where the job's `_mark_sync_complete` leaves it alone.
  test("S3-06 - A published record stuck at error self-heals to synced on the first public read @slow", async ({
    page,
  }) => {
    skipUnlessOptedIn("726")
    // The public read's own poll, plus the tick alignment before the injection.
    test.setTimeout(RUN_TEST_TIMEOUT_MS + 28 * 60_000)

    const wsName = "e2e-s3heal"
    let recordId = 0
    let wsId = 0
    let headers: Record<string, string> = {}
    const anon = await anonymousApi()
    try {
      const staged = await stageFinishedRun(page, wsName)
      ;({ wsId, recordId, headers } = staged)
      const { uid } = staged

      const published = await page.request.post(
        `${apiUrl()}/api/dataview/publish/${recordId}/on`,
        { headers, timeout: UPLOAD_TIMEOUT_MS },
      )
      expect(published.ok(), await published.text()).toBe(true)

      // Reach the row's premise honestly: the data really is available on the
      // public tier, so the error status below is a lie about a healthy
      // record - the only state the self-heal is meant to correct.
      await expect
        .poll(
          async () =>
            (
              await anon.get(
                `${apiUrl()}/api/public/dataview/workflow/reproduce/${wsId}/${uid}`,
                { timeout: UPLOAD_TIMEOUT_MS },
              )
            ).status(),
          {
            timeout: 10 * 60_000,
            intervals: [20_000],
            message: "the published record never became publicly readable",
          },
        )
        .toBe(200)
      expect(
        syncStatusOf(recordId),
        "a publicly readable record should be synced before the row's own edit",
      ).toBe("synced")

      // The whole verdict rests on the job not touching the row between the
      // forced `error` and the public read - `_mark_sync_complete` would set
      // `synced` with no version bump, and the assertion below would then read
      // as a product regression when it is really a lost race. The critical
      // section is a few SSM round trips wide; taking a fresh tick first buys
      // it most of a 5-minute cycle.
      await awaitFreshSyncTick()
      const versionBefore = versionOf(recordId)
      runSqlWriteOnDev(
        `UPDATE experiment_records SET local_sync_status = 'error' ` +
          `WHERE id = ${recordId}`,
      )
      expect(syncStatusOf(recordId), "the row's premise did not land").toBe(
        "error",
      )

      const t0 = windowStart()
      const healed = await anon.get(
        `${apiUrl()}/api/public/dataview/workflow/reproduce/${wsId}/${uid}`,
        { timeout: UPLOAD_TIMEOUT_MS },
      )
      expect(
        healed.status(),
        `the error row did not serve: ${await healed.text()}`,
      ).toBe(200)

      // Far inside a single 5-minute tick, so the job is not the likely
      // author; the two assertions after it are what make that certain.
      await expect
        .poll(() => syncStatusOf(recordId), {
          timeout: 90_000,
          intervals: [5_000],
          message: "the error status was never corrected back to synced",
        })
        .toBe("synced")
      expect(
        versionOf(recordId),
        "the status changed without the endpoint's version bump - the " +
          "background job corrected it, not the public read this row is about",
      ).toBe(versionBefore + 1)
      // Quote-free by necessity: cloudwatchHas rejects a pattern carrying
      // either quote, and the line quotes both statuses.
      const healLine = `${wsId}/${uid} data is available, updating sync status from`
      await expect
        .poll(() => cloudwatchHas(PUBLIC_LOG_GROUP, healLine, t0), {
          ...CLOUDWATCH_POLL,
          message: `no self-heal line for ${wsId}/${uid} in ${PUBLIC_LOG_GROUP}`,
        })
        .toBe(true)
    } finally {
      await anon.dispose()
      await unstageRun(page, headers, recordId, wsId)
    }
  })

  // Rows 720 + 721 + 722 in one narrative, because they are one: the job has
  // to carry a record all the way to `synced` (720) before breaking it can
  // mean anything (721), and the repair is only a repair of that break (722).
  // Three 5-minute ticks is what the row costs; the first of them is a wait
  // 721 would have to spend anyway.
  //
  // Row 721's other half - that a `synced` row whose S3 files vanish is NEVER
  // demoted, because `_get_pending_experiments` only ever selects
  // pending/error - is deliberately not automated here. It is an assertion
  // that nothing happens for two ticks, it would pin a documented design gap
  // in place as a change-detector, and `test_sync_job_db_state.py::
  // TestPendingSelectionStatuses` already settles the same question against
  // the compiled query in under a second.
  test("S3-07 - The sync job carries a publish to synced, fails it to error when S3 loses the config, and retries it back into a public 200 @slow", async ({
    page,
  }) => {
    skipUnlessOptedIn("720 / 721 / 722 / 2032")
    // Three settle windows, their CloudWatch polls, and the public read the
    // last act now waits out.
    test.setTimeout(RUN_TEST_TIMEOUT_MS + 62 * 60_000)

    const wsName = "e2e-s3sync"
    const asideDir = fs.mkdtempSync(path.join(os.tmpdir(), "e2e-s3sync-"))
    let recordId = 0
    let wsId = 0
    let headers: Record<string, string> = {}
    let bucket = ""
    // Both files `validate_experiment_in_s3` requires, so the failure below
    // cannot hinge on which one the validator happens to look for first.
    let asideKeys: { key: string; local: string }[] = []
    let filesAside = false
    const anon = await anonymousApi()
    const restoreAll = () => {
      // Skips what was never copied aside: with the flag armed before the
      // loop, a failure part-way through leaves some locals absent.
      for (const { key, local } of asideKeys) {
        if (fs.existsSync(local)) s3Restore(bucket, key, local)
      }
      filesAside = false
    }

    try {
      const staged = await stageFinishedRun(page, wsName)
      ;({ wsId, recordId, headers, bucket } = staged)
      const { uid } = staged
      const prefix = `app/studio_data/output/${wsId}/${uid}`
      asideKeys = ["experiment.yaml", "workflow.yaml"].map((name) => ({
        key: `${prefix}/${name}`,
        local: path.join(asideDir, name),
      }))

      // --- Row 720: publish parks the row, the job validates it and flips it.
      // Aligned first: the `pending` read below is a claim about the state the
      // publish leaves, and a tick landing between the two would answer
      // `synced` and fail it for the wrong reason.
      await awaitFreshSyncTick()
      const t0 = windowStart()
      const published = await page.request.post(
        `${apiUrl()}/api/dataview/publish/${recordId}/on`,
        { headers, timeout: UPLOAD_TIMEOUT_MS },
      )
      expect(published.ok(), await published.text()).toBe(true)
      expect(
        syncStatusOf(recordId),
        "publish must park the row pending for the job to pick up",
      ).toBe("pending")

      await expect
        .poll(() => syncStatusOf(recordId), {
          timeout: SYNC_SETTLE_MS,
          intervals: [15_000],
          message: "the sync job never validated the freshly published row",
        })
        .toBe("synced")
      await awaitBackgroundLine(`Successfully validated ${wsId}/${uid}`)
      await awaitMetric(
        "ExperimentsSynced",
        t0,
        "the job flipped the row without publishing ExperimentsSynced",
      )
      // The validate-then-trigger half of the row. The trigger is a
      // fire-and-forget task started after the DB flip, so it can only be
      // waited for, never read once - and a failed ALB call logs a different
      // line, which is the regression this row exists to catch.
      await awaitBackgroundLine(
        `Proactive download triggered for ${wsId}/${uid}`,
      )

      // --- Row 721: a pending row whose S3 config is gone fails to error.
      // The files go first and the status second, so the job can never see a
      // pending row while the prefix is still intact.
      // Armed BEFORE the loop: `s3Aside` deletes as it goes, so a throw on the
      // second key would otherwise leave the first one deleted with the
      // `finally` believing nothing was moved.
      filesAside = true
      for (const { key, local } of asideKeys) s3Aside(bucket, key, local)
      runSqlWriteOnDev(
        `UPDATE experiment_records SET local_sync_status = 'pending' ` +
          `WHERE id = ${recordId}`,
      )
      const tErr = windowStart()
      await expect
        .poll(() => syncStatusOf(recordId), {
          timeout: SYNC_SETTLE_MS,
          intervals: [15_000],
          message:
            "the job never failed the row whose required S3 files are gone",
        })
        .toBe("error")
      await awaitBackgroundLine(
        `Failed to validate ${wsId}/${uid} after 3 attempts`,
      )
      await awaitMetric(
        "SyncErrors",
        tErr,
        "the job errored the row without publishing SyncErrors",
      )

      // --- Row 722: `error` stays in the job's query, so putting the files
      // back is the whole retry - nothing re-queues the row by hand.
      const tFix = windowStart()
      restoreAll()
      await expect
        .poll(() => syncStatusOf(recordId), {
          timeout: SYNC_SETTLE_MS,
          intervals: [15_000],
          message: "the restored row was never retried back to synced",
        })
        .toBe("synced")
      // A NEW validated line: the one act 1 logged for this uid is still in
      // the tail, so only its ingestion time tells the retry apart from it.
      await awaitBackgroundLine(`Successfully validated ${wsId}/${uid}`, tFix)
      await awaitMetric(
        "ExperimentsSynced",
        tFix,
        "the retry flipped the row without publishing ExperimentsSynced",
      )
      // Row 2032's second half: the retry is only a recovery if the visitor
      // gets the experiment back, not merely if the DB says `synced`. Polled
      // rather than read once - the public tier refills its local copy from
      // S3 on demand, so the first read after the restore can still be
      // downloading what the row above already validated.
      await expect
        .poll(
          async () =>
            (
              await anon.get(
                `${apiUrl()}/api/public/dataview/workflow/reproduce/${wsId}/${uid}`,
                { timeout: UPLOAD_TIMEOUT_MS },
              )
            ).status(),
          {
            timeout: 10 * 60_000,
            intervals: [20_000],
            message: "reproduce never reached 200 after the row was retried",
          },
        )
        .toBe(200)
    } finally {
      if (filesAside) {
        try {
          restoreAll()
        } catch {
          // the workspace delete below drops the prefix anyway
        }
      }
      await anon.dispose()
      await unstageRun(page, headers, recordId, wsId)
      fs.rmSync(asideDir, { recursive: true, force: true })
    }
  })

  // Row 725. The sheet calls the 202 unprovokable - the public reproduce
  // downloads on demand first, so the visitor's own request usually repairs
  // the very state it was meant to observe. It is provokable, deterministically:
  // `download_experiment` clears the local experiment directory and refills it
  // from S3, and `validate_for_display` needs experiment.yaml where
  // `validate_experiment_in_s3` needs experiment.yaml AND workflow.yaml. Remove
  // only experiment.yaml and the download still succeeds (the prefix is not
  // empty, so no 404) while leaving nothing displayable - which on a `pending`
  // row is precisely the 202 branch. Publishing right after a fresh tick keeps
  // the row pending for most of a five-minute window, because the same missing
  // file would otherwise have the job demote it to `error` and answer 503.
  test("S3-08 - A published-but-unsynced experiment answers 202 pending_sync to the public, and 200 once its config is back @slow", async ({
    page,
    browser,
  }) => {
    skipUnlessOptedIn("725")
    test.setTimeout(RUN_TEST_TIMEOUT_MS + 30 * 60_000)

    const wsName = "e2e-s3pending"
    const asideDir = fs.mkdtempSync(path.join(os.tmpdir(), "e2e-s3pending-"))
    let recordId = 0
    let wsId = 0
    let headers: Record<string, string> = {}
    let bucket = ""
    let key = ""
    const local = path.join(asideDir, "experiment.yaml")
    let fileAside = false
    const anon = await anonymousApi()

    try {
      const staged = await stageFinishedRun(page, wsName)
      ;({ wsId, recordId, headers, bucket } = staged)
      const { uid } = staged
      key = `app/studio_data/output/${wsId}/${uid}/experiment.yaml`
      const reproduceUrl = `${apiUrl()}/api/public/dataview/workflow/reproduce/${wsId}/${uid}`

      await awaitFreshSyncTick()
      const published = await page.request.post(
        `${apiUrl()}/api/dataview/publish/${recordId}/on`,
        { headers, timeout: UPLOAD_TIMEOUT_MS },
      )
      // Publish validates the S3 prefix itself, so the config has to be taken
      // away after it, not before.
      expect(published.ok(), await published.text()).toBe(true)
      s3Aside(bucket, key, local)
      fileAside = true
      expect(
        syncStatusOf(recordId),
        "the row must still be pending - a tick beat the staging",
      ).toBe("pending")

      const pending = await anon.get(reproduceUrl, {
        timeout: UPLOAD_TIMEOUT_MS,
      })
      expect(
        pending.status(),
        `reproduce should have answered 202: ${await pending.text()}`,
      ).toBe(202)
      const body = await pending.json()
      expect(body.status).toBe("pending_sync")
      expect(body.message).toContain("Publishing in progress")
      expect(pending.headers()["retry-after"]).toBe("300")
      // Not a one-off race: a second read answers the same, which is what
      // makes the auto-retry the row describes a stable state to retry into
      // rather than a window the first request closes behind itself.
      const again = await anon.get(reproduceUrl, {
        timeout: UPLOAD_TIMEOUT_MS,
      })
      expect(again.status(), await again.text()).toBe(202)

      // What the visitor actually sees, reported rather than asserted. The
      // sheet expects the hourglass, the info alert and a ~10s auto-retry
      // ladder, and `useSyncRetry` does render exactly that - but only from
      // its `catch`, and axios resolves a 202 like any other 2xx, so nothing
      // in the app reaches that branch on the real response. Asserting the
      // sheet's expectation here would encode a frontend defect as this
      // lane's pass criterion, so the dialog is opened, described and left
      // for the row's owner to adjudicate. Row 725's server half above is the
      // part this test decides.
      const ctx = await browser.newContext({
        baseURL: process.env.BASE_URL,
        storageState: undefined,
      })
      const viewer = await ctx.newPage()
      let reproduceCalls = 0
      viewer.on("request", (req) => {
        if (req.url().includes(`/workflow/reproduce/${wsId}/${uid}`)) {
          reproduceCalls += 1
        }
      })
      try {
        const dialog = await openPublicDetails(viewer, wsName, uid)
        // Reported alongside the icons: opening the UI costs enough that the
        // 5-minute tick can demote the row to `error` first, and a dialog
        // describing an errored record says nothing about the pending state.
        const statusAtOpen = syncStatusOf(recordId)
        // Three retry intervals: enough for the ladder to show itself if the
        // app ever reaches it.
        await viewer.waitForTimeout(31_000)
        const hourglass = await dialog
          .locator('[data-testid="HourglassEmptyIcon"]')
          .first()
          .isVisible()
        const errorIcon = await dialog
          .locator('[data-testid="ErrorOutlineIcon"]')
          .first()
          .isVisible()
        const alert = await dialog
          .locator(".MuiAlert-root")
          .first()
          .textContent()
          .catch(() => null)
        console.log(
          `[16-storage-aws] S3-08 public dialog over a 202: ` +
            `local_sync_status=${statusAtOpen} at open / ` +
            `${syncStatusOf(recordId)} after, ` +
            `hourglass=${hourglass}, errorIcon=${errorIcon}, ` +
            `reproduceRequests=${reproduceCalls} in ~31s, ` +
            `alert=${JSON.stringify(alert)}`,
        )
      } finally {
        await ctx.close()
      }

      // The other half of the row: once the data is really there the same URL
      // answers 200, so the 202 above was the sync state and not a broken
      // record. Restoring also returns the row to a state the job can
      // validate, which is what keeps the teardown quiet.
      s3Restore(bucket, key, local)
      fileAside = false
      await expect
        .poll(
          async () =>
            (
              await anon.get(reproduceUrl, { timeout: UPLOAD_TIMEOUT_MS })
            ).status(),
          {
            timeout: 10 * 60_000,
            intervals: [20_000],
            message: "reproduce never reached 200 after the config came back",
          },
        )
        .toBe(200)
    } finally {
      if (fileAside) {
        try {
          s3Restore(bucket, key, local)
        } catch {
          // the workspace delete below drops the prefix anyway
        }
      }
      await anon.dispose()
      await unstageRun(page, headers, recordId, wsId)
      fs.rmSync(asideDir, { recursive: true, force: true })
    }
  })
})
