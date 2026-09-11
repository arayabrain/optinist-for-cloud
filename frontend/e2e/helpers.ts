import { execFileSync, execSync } from "child_process"
import * as fs from "fs"
import * as path from "path"

import {
  APIRequestContext,
  chromium,
  expect,
  Page,
  request,
  test,
} from "@playwright/test"

// ---------------------------------------------------------------------------
// Local docker stack. Specs that need state no API exposes (a plan, a role, a
// verified email) drive the containers directly, so they run against the local
// stack only and skip elsewhere.
// ---------------------------------------------------------------------------

export const REPO_ROOT = path.resolve(__dirname, "../..")
const COMPOSE = "docker compose -f docker-compose.dev.multiuser.yml"
const DOCKER_EXEC_TIMEOUT_MS = 30_000

// The deployed RDS is private and behind a TLS-only proxy, so SQL there runs
// through SSM on an in-VPC instance that fetches its own credentials. The
// host is resolved by Name tag because the environment's daily stop/start
// replaces its ASG instances, so any hardcoded id rots within a day.
const SSM_INSTANCE_NAME =
  process.env.RDS_SSM_INSTANCE_NAME || "development-optinist-background"
export const RDS_PROXY_HOST =
  process.env.RDS_PROXY_HOST ||
  "development-optinist-rds-proxy.proxy-cdmeogcuo1v2.ap-northeast-1.rds.amazonaws.com"
const RDS_SECRET_ID =
  process.env.RDS_SECRET_ID || "development-optinist/database/config"
const SSM_POLL_TIMEOUT_MS = 120_000

function aws(args: string, input?: string): string {
  return execSync(`aws ${args}`, {
    input,
    stdio: ["pipe", "pipe", "pipe"],
    timeout: 60_000,
  })
    .toString()
    .trim()
}

let ssmInstance = ""
function ssmInstanceId(): string {
  if (ssmInstance) return ssmInstance
  ssmInstance =
    process.env.RDS_SSM_INSTANCE ||
    aws(
      `ec2 describe-instances --region ap-northeast-1 ` +
        `--filters Name=tag:Name,Values=${SSM_INSTANCE_NAME} ` +
        `Name=instance-state-name,Values=running ` +
        `--query 'Reservations[0].Instances[0].InstanceId' --output text`,
    )
  if (!ssmInstance || ssmInstance === "None") {
    throw new Error(`no running ${SSM_INSTANCE_NAME} instance for SSM SQL`)
  }
  return ssmInstance
}

// Run shell commands on an in-VPC instance via SSM and return stdout. The
// deployed lanes use it for anything the API does not expose: SQL through the
// TLS-only RDS proxy, and reads/writes on an ECS task's local filesystem.
export function runShellOverSsm(
  instanceId: string,
  commands: string[],
  label = "SSM command",
): string {
  const paramsFile = path.join(
    fs.mkdtempSync(path.join(require("os").tmpdir(), "e2e-ssm-")),
    "params.json",
  )
  fs.writeFileSync(paramsFile, JSON.stringify({ commands }))
  let commandId: string
  try {
    commandId = aws(
      `ssm send-command --region ap-northeast-1 --instance-ids ${instanceId} ` +
        `--document-name AWS-RunShellScript --parameters file://${paramsFile} ` +
        `--query Command.CommandId --output text`,
    )
  } finally {
    fs.rmSync(path.dirname(paramsFile), { recursive: true, force: true })
  }

  const deadline = Date.now() + SSM_POLL_TIMEOUT_MS
  for (;;) {
    const raw = aws(
      `ssm get-command-invocation --region ap-northeast-1 --command-id ${commandId} ` +
        `--instance-id ${instanceId} --output json`,
    )
    const inv = JSON.parse(raw)
    if (inv.Status === "Success")
      return (inv.StandardOutputContent || "").trim()
    if (!["Pending", "InProgress", "Delayed"].includes(inv.Status)) {
      throw new Error(
        `${label} failed (${inv.Status}): ${inv.StandardErrorContent || inv.StatusDetails}`,
      )
    }
    if (Date.now() > deadline) throw new Error(`${label} timed out`)
    execSync("sleep 2")
  }
}

function runSqlOverSsm(sql: string): string {
  const py =
    "python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])'"
  const script = [
    "set -e",
    `CFG=$(aws secretsmanager get-secret-value --region ap-northeast-1 --secret-id ${RDS_SECRET_ID} --query SecretString --output text)`,
    `export MYSQL_PWD=$(printf '%s' "$CFG" | ${py} password)`,
    `DBU=$(printf '%s' "$CFG" | ${py} username)`,
    `DBN=$(printf '%s' "$CFG" | ${py} database)`,
    `mariadb --ssl -h ${RDS_PROXY_HOST} -u "$DBU" --connect-timeout=10 "$DBN" -N <<'SQL'\n${sql}\nSQL`,
  ]
  return runShellOverSsm(ssmInstanceId(), script, "SSM SQL")
}

// Off the local stack this reaches the shared dev RDS, where the throwaway
// rows a spec may delete locally are real. Reads are allowed there; anything
// that writes must say so by running against a local stack.
function assertReadOnly(sql: string) {
  const statement = sql
    .replace(/--[^\n]*/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .trim()
  // The mysql CLI executes every statement it is piped, so a read-only
  // first statement must not smuggle a second one behind a semicolon
  if (/;\s*\S/.test(statement)) {
    throw new Error(
      `runSql refused a multi-statement query against ${process.env.BASE_URL}: ` +
        `${statement.split("\n")[0]}`,
    )
  }
  if (!/^(select|show)\b/i.test(statement)) {
    throw new Error(
      `runSql refused a non-read statement against ${process.env.BASE_URL}: ` +
        `${statement.split("\n")[0]}. Specs that mutate the database are ` +
        `local-stack only; gate them on localStackSkipReason().`,
    )
  }
}

// The one sanctioned write path to the deployed dev RDS. runSql refuses writes
// so a spec cannot casually mutate the shared environment; a lane that must
// stage a state the API cannot reach (the is_shared nudge behind row 608's
// migration) opts in through this loud name instead. Development only, one
// statement only.
export function runSqlWriteOnDev(sql: string): string {
  expect(
    process.env.BASE_URL || "",
    "runSqlWriteOnDev only runs against the development environment",
  ).toContain("development-optinist")
  if (/;\s*\S/.test(sql.trim())) {
    throw new Error(`runSqlWriteOnDev refuses multi-statement SQL: ${sql}`)
  }
  return runSqlOverSsm(sql)
}

export function runSql(sql: string): string {
  if (!isLocalBaseUrl()) {
    assertReadOnly(sql)
    return runSqlOverSsm(sql)
  }
  return execSync(
    `${COMPOSE} exec -T db sh -c ` +
      `'exec mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" -N "$MYSQL_DATABASE"'`,
    {
      cwd: REPO_ROOT,
      input: sql,
      stdio: ["pipe", "pipe", "pipe"],
      // execSync blocks the event loop, so a hung exec cannot be cut short by
      // the test timeout: without this the run stalls to the global timeout
      timeout: DOCKER_EXEC_TIMEOUT_MS,
    },
  )
    .toString()
    .trim()
}

// ---------------------------------------------------------------------------
// Deployed-env AWS assertions, shared by the opt-in real-AWS lanes
// (15-premium-aws, 16-storage-aws).
// ---------------------------------------------------------------------------

export const AWS_REGION = "ap-northeast-1"
// Where each backend log line lands. /users/me/* routes to the public tier
// (premium routing headers only exist after assignment and only the app sends
// them), workflow compute logs on the tier that ran it, and login-time lines
// land on the default (free) tier.
// Named for the environment under test, as 17-aws-health names its resources:
// hardcoding development had a production run watch development's group and
// time out on a line that had landed correctly somewhere else. Exported so the
// spec reads the same four names rather than deriving identical ones twice.
export const TARGET_ENV = process.env.HEALTH_ENV || "development"
export const FREE_LOG_GROUP = `/ecs/${TARGET_ENV}-optinist-cloud-taskdef`
export const PREMIUM_LOG_GROUP = `/ecs/${TARGET_ENV}-premium-optinist-cloud-taskdef`
export const PUBLIC_LOG_GROUP = `/ecs/${TARGET_ENV}-public-optinist-cloud-taskdef`
export const BACKGROUND_LOG_GROUP = `/ecs/${TARGET_ENV}-background-optinist-cloud-taskdef`

// MUI's default error.main, which the theme does not override: the colour the
// destructive buttons must actually be.
export const ERROR_RED = "rgb(211, 47, 47)"

export const CLOUDWATCH_POLL = { timeout: 240_000, intervals: [15_000] }

// Small clock-skew slack for --start-time windows. Deliberately tight: a
// wide window would match lines a PREVIOUS test's teardown logged for the
// same user, making the assert pass without this test's action.
export function windowStart(): number {
  return Date.now() - 15_000
}

// One CloudWatch probe; positive assertions expect.poll it (propagation takes
// up to ~2 min), negative ones sample it once AFTER a positive passed - the
// awslogs driver runs non-blocking, so absence is only meaningful once
// presence of a same-window line proves delivery caught up.
// pattern is matched as a single quoted term: no double quotes inside.
export function cloudwatchHas(
  logGroup: string,
  pattern: string,
  sinceMs: number,
): boolean {
  // The pattern is interpolated into a single-quoted shell argument wrapped in
  // double quotes, so either quote character breaks out of it.
  if (/["']/.test(pattern)) {
    throw new Error(
      `cloudwatchHas pattern must not contain a quote: ${pattern}`,
    )
  }
  const out = execSync(
    `aws logs filter-log-events --log-group-name ${logGroup} ` +
      `--start-time ${sinceMs} --filter-pattern '"${pattern}"' ` +
      `--max-items 1 --query 'events[0].message' --output text ` +
      `--region ${AWS_REGION}`,
    { timeout: 30_000 },
  )
    .toString()
    .trim()
  return out !== "" && out !== "None"
}

// The awslogs driver stamps every background-tier event with the task's start
// time (its multiline pattern does not match the app's log format), so
// filter-log-events --start-time finds nothing there however live the task is.
// Reading the stream's tail and trusting ingestionTime sidesteps that.
export function logTail(
  logGroup: string,
  limit = 300,
): {
  lastIngestion: number
  text: string
  events: { ingestionTime: number; message: string }[]
} {
  const streams = awsJson<{ logStreamName: string }[]>(
    `logs describe-log-streams --log-group-name ${logGroup} ` +
      `--order-by LastEventTime --descending --max-items 1 ` +
      `--query 'logStreams[]'`,
  )
  expect(streams.length, `${logGroup} has no log streams`).toBeGreaterThan(0)
  const events = awsJson<{ ingestionTime: number; message: string }[]>(
    `logs get-log-events --log-group-name ${logGroup} ` +
      `--log-stream-name ${streams[0].logStreamName} --limit ${limit} ` +
      `--query 'events[].{ingestionTime:ingestionTime,message:message}'`,
  )
  expect(events.length, `${logGroup} newest stream is empty`).toBeGreaterThan(0)
  return {
    lastIngestion: Math.max(...events.map((e) => e.ingestionTime)),
    text: events.map((e) => e.message).join("\n"),
    events,
  }
}

// Elapsed-time progress for the rows that run for tens of minutes against real
// AWS. Without it a stalled run reads exactly like a slow one, and the only
// signal is the test timeout half an hour later.
//   step() -> always prints
//   tick() -> prints at most once per PROGRESS_TICK_MS, for a poll callback
const PROGRESS_TICK_MS = 60_000

export function progressLog(
  file: string,
  label: string,
): {
  step: (message: string) => void
  tick: (message: string) => void
} {
  const started = Date.now()
  let last = 0
  const line = (message: string) =>
    console.log(
      `[${file}] ${label}: ` +
        `+${((Date.now() - started) / 60_000).toFixed(1)}m ${message}`,
    )
  return {
    step: line,
    tick: (message: string) => {
      if (Date.now() - last < PROGRESS_TICK_MS) return
      last = Date.now()
      line(message)
    },
  }
}

// One read-only AWS CLI call, parsed. Callers pass their own --query.
export function awsJson<T>(args: string): T {
  return JSON.parse(
    execSync(`aws ${args} --output json --region ${AWS_REGION}`, {
      timeout: 60_000,
      stdio: ["pipe", "pipe", "pipe"],
      // Same reason `runCompose` carries one: a `get-log-events` page of a few
      // hundred lines with `exc_info` tracebacks, JSON-escaped, passes Node's
      // 1MB default and surfaces as an opaque ENOBUFS from inside whatever
      // poll asked for it. The CLI caps its own response at 1MB, so this is
      // only ever headroom.
      maxBuffer: 64 * 1024 * 1024,
    }).toString(),
  )
}

// Throws on CLI failure instead of returning a vacuous empty result, so
// "the prefix is empty" can never pass on a throttled or misconfigured call
export function s3ObjectCount(bucket: string, prefix: string): number {
  // length(Contents), not KeyCount: the CLI auto-paginates list-objects-v2 and
  // its merged result drops KeyCount, so that query answers "None" even for a
  // prefix holding objects - which made every caller throw.
  // execFileSync, not execSync: argv goes straight to the CLI, so no prefix
  // can break out of a shell quote.
  const out = execFileSync(
    "aws",
    [
      "s3api",
      "list-objects-v2",
      "--bucket",
      bucket,
      "--prefix",
      prefix,
      "--query",
      "length(Contents || `[]`)",
      "--output",
      "text",
      "--region",
      AWS_REGION,
    ],
    { timeout: 60_000, stdio: ["pipe", "pipe", "pipe"] },
  )
    .toString()
    .trim()
  const count = Number(out)
  if (!Number.isFinite(count)) {
    throw new Error(`list-objects-v2 count for ${bucket}/${prefix}: ${out}`)
  }
  return count
}

// apiHeaders plus the premium routing headers the app itself would send.
// Playwright's request context bypasses the axios interceptor, so without
// these a premium user's helper API calls route to the free tier - which
// never saw a premium instance's local files (/experiments would report a
// premium run absent forever).
export async function routedApiHeaders(
  page: Page,
): Promise<Record<string, string>> {
  const headers = await apiHeaders(page)
  const routing = await page.evaluate(() => ({
    id: localStorage.getItem("routing_id"),
    tier: localStorage.getItem("routing_tier"),
  }))
  if (routing.id && routing.tier) {
    headers["X-Routing-ID"] = routing.id
    headers["X-User-Tier"] = routing.tier
  }
  return headers
}

// Compose subcommands the backend-restart spec needs (restart, logs). Kept
// here so the compose file and the exec timeout have one definition.
export function runCompose(
  args: string,
  timeoutMs = DOCKER_EXEC_TIMEOUT_MS,
): string {
  return execSync(`${COMPOSE} ${args}`, {
    cwd: REPO_ROOT,
    stdio: ["pipe", "pipe", "pipe"],
    timeout: timeoutMs,
    // A few thousand log lines with tracebacks blow the 1MB default, which
    // surfaces as an opaque compose failure rather than "the log is too big".
    maxBuffer: 64 * 1024 * 1024,
  })
    .toString()
    .trim()
}

export function runInBackend(cmd: string, input?: string): string {
  return execSync(`${COMPOSE} exec -T studio-dev-be ${cmd}`, {
    cwd: REPO_ROOT,
    stdio: ["pipe", "pipe", "pipe"],
    input,
    timeout: DOCKER_EXEC_TIMEOUT_MS,
  })
    .toString()
    .trim()
}

// Backend Python inside the deployed dev container, for the rows that ask the
// job's own code rather than re-implementing its query. The task's DB settings
// arrive as DB_*, which cloud-startup.sh maps into MYSQL_* for the app process
// only, so an exec has to map them again - inside the container, so no secret
// crosses the SSM boundary. The source is base64'd to survive two levels of
// shell quoting.
//
// `instanceId` overrides the INSTANCE_ID the job resolves. Passing a synthetic
// id is what makes backdating logged_out_at safe on shared dev:
// `_get_users_for_cleanup` filters on `instance_id == resolve_instance_id()`,
// so no real cleanup worker will ever select a row carrying it, however far
// back its stamp is. That covers the deleting half of the job only - the
// orphan sweep is not instance-filtered and treats an unresolvable id as a
// terminated instance, so it deletes the seeded row itself (DB row and usage
// log, never files). Callers must re-check their seed rather than assume it
// survived.
export function runInDeployedBackend(
  python: string,
  instanceId?: string,
): string {
  const envFlag = instanceId ? `-e INSTANCE_ID=${instanceId} ` : ""
  const b64 = Buffer.from(python, "utf-8").toString("base64")
  return runShellOverSsm(
    ssmInstanceId(),
    [
      "set -e",
      "C=$(docker ps --format '{{.Names}}' | " +
        "grep -m1 -- -background-optinist-cloud-container || true)",
      '[ -n "$C" ] || { echo "no studio container on this host" >&2; exit 1; }',
      `docker exec ${envFlag}-w /app "$C" sh -c '` +
        'export MYSQL_SERVER="$DB_HOST" MYSQL_USER="$DB_USER" ' +
        'MYSQL_PASSWORD="$DB_PASSWORD" MYSQL_DATABASE="$DB_NAME"; ' +
        `echo ${b64} | base64 -d | python -'`,
    ],
    "deployed backend python",
  )
}

// Registration leaves the address unverified, and an unverified account cannot
// log in. Dev Firebase has no inbox to click through.
export function verifyEmail(email: string) {
  runInBackend(
    "poetry run python -",
    `
import firebase_admin
from firebase_admin import auth, credentials
cred = credentials.Certificate("studio/config/auth/firebase_private.json")
firebase_admin.initialize_app(cred)
auth.update_user(auth.get_user_by_email("${email}").uid, email_verified=True)
`,
  )
}

export function deleteFirebaseUser(email: string) {
  runInBackend(
    "poetry run python -",
    `
import firebase_admin
from firebase_admin import auth, credentials
cred = credentials.Certificate("studio/config/auth/firebase_private.json")
firebase_admin.initialize_app(cred)
try:
    auth.delete_user(auth.get_user_by_email("${email}").uid)
except auth.UserNotFoundError:
    pass
`,
  )
}

const SWEEP_SCRIPT = ".github/scripts/sweep_e2e_firebase_users.py"

// A run that dies before its afterAll orphans the Firebase user even though the
// DB row is gone, which puts it beyond the reach of any DB-driven cleanup.
// Returns how many accounts the sweep removed.
// The script is kept out of the shipped image, so a deployed run sends its
// source instead of invoking a path that is not there.
export function sweepE2eFirebaseUsers(): number {
  const out = isLocalBaseUrl()
    ? runInBackend(`poetry run python ${SWEEP_SCRIPT}`)
    : runInDeployedBackend(
        fs.readFileSync(path.join(REPO_ROOT, SWEEP_SCRIPT), "utf-8"),
      )
  // poetry and firebase_admin can print their own lines before the count, and
  // an empty output parses as 0 rather than as the failure it is
  const last = out.split("\n").pop() ?? ""
  if (!/^\d+$/.test(last)) {
    throw new Error(`sweep printed ${JSON.stringify(out)} instead of a count`)
  }
  return Number(last)
}

export function activeUserRows(email: string): number {
  return Number(
    runSql(
      `SELECT COUNT(*) FROM users WHERE email = '${sqlLiteral(email)}'
         AND active = 1;`,
    ),
  )
}

// Register + verify, idempotently. Throws with both statuses when the account
// still cannot log in, because a silent failure here surfaces much later as an
// unrelated assertion. Local stack only: the duplicate-row check below reads
// the docker DB directly, so callers must gate on localStackSkipReason().
export async function ensureRegisteredUser(
  email: string,
  password: string,
  name: string,
  roleId = 20,
): Promise<void> {
  const api = await request.newContext({ baseURL: apiUrl() })
  const register = () =>
    api.post("/api/register", {
      data: { name, role_id: roleId, email, password },
    })
  try {
    const creds = { email, password }
    const existing = await api.post("/auth/login", { data: creds })
    if (existing.ok()) return
    // A row means the account is real and the configured password is simply
    // wrong. Registering anyway adds a SECOND active row at this address,
    // which nothing in the schema prevents and which then breaks every lookup
    // expecting a single id - including this spec's own `beforeAll`.
    if (activeUserRows(email) > 0) {
      throw new Error(
        `${email} already has an active row but its password does not match: ` +
          `${existing.status()} ${await existing.text()}`,
      )
    }
    // A 5xx is the dev backend restarting under --reload, not a verdict on the
    // account: deleting a real Firebase user on one is unrecoverable.
    if (existing.status() >= 500) {
      throw new Error(
        `login for ${email} failed transiently, not deleting it: ` +
          `${existing.status()} ${await existing.text()}`,
      )
    }
    // No row, so whatever Firebase still holds is an orphan from a reset stack
    // or a deletion that stopped after its Firebase step. Left in place it
    // makes registration answer EMAIL_EXISTS forever.
    deleteFirebaseUser(email)
    const registered = await register()
    verifyEmail(email)
    const loggedIn = await api.post("/auth/login", { data: creds })
    if (!loggedIn.ok()) {
      throw new Error(
        `bootstrap failed for ${email}: register ${registered.status()} ` +
          `${await registered.text()}; login ${loggedIn.status()} ` +
          `${await loggedIn.text()}`,
      )
    }
  } finally {
    await api.dispose()
  }
}

// The confirm-by-typing dialog used for deletions
export function confirmDialog(page: Page) {
  return page.locator('[role="dialog"]')
}

// Hold a request open until the test releases it, so an assertion on an
// in-flight state never races a wall clock. No Promise.withResolvers on node 20
export function routeGate() {
  let release = () => {}
  const held = new Promise<void>((resolve) => (release = resolve))
  return { held, release: () => release() }
}

export function isLocalBaseUrl(): boolean {
  return /localhost|127\.0\.0\.1/.test(
    process.env.BASE_URL || "http://localhost:3000",
  )
}

// ---------------------------------------------------------------------------
// Stripe reads. Same key source as infrastructure/scripts/manual_test_scan.py:
// the environment's own secret, GET only, and never a live key. Specs that need
// to compare our stored state against Stripe's use this rather than trusting
// the app's own view of it.
// ---------------------------------------------------------------------------

// An account whose subscription is really backed by Stripe. This is NOT
// TEST_PREMIUM_*: the deployed lane's premium accounts are premium in our
// database only and own no Stripe customer at all, so every Stripe-side
// assertion about them is vacuous. Configure it to enable the invoice and
// cancel-round-trip rows; they skip with a reason while it is unset.
export const STRIPE_USER = {
  email: process.env.TEST_STRIPE_EMAIL || "",
  password: process.env.TEST_STRIPE_PASSWORD || "",
}

export function stripeAccountSkipReason(): string {
  if (!STRIPE_USER.email || !STRIPE_USER.password) {
    return (
      "needs TEST_STRIPE_EMAIL/TEST_STRIPE_PASSWORD - an account with a real " +
      "Stripe-backed subscription. TEST_PREMIUM_* and TEST_PREMIUM2_* cannot " +
      "serve as-is: they are premium in the database only and own no Stripe " +
      "customer"
    )
  }
  // Being configured is not the same as being usable. Checking here turns
  // "pointed at an account with no Stripe data" into a skip that says so,
  // rather than a failure deep inside a helper looking for the subscription.
  const customers = stripeGet("/v1/customers", {
    email: STRIPE_USER.email,
    limit: 3,
  }).data as Record<string, unknown>[]
  if (!customers.length) {
    return (
      `${STRIPE_USER.email} owns no Stripe customer, so there is no ` +
      "subscription or invoice to read. Either point TEST_STRIPE_* at an " +
      "account that has one, or give this account a real subscription in the " +
      "environment's Stripe test account"
    )
  }
  return ""
}

let stripeKeyCache = ""

export const LIVE_STRIPE_REFUSAL = "refusing to read Stripe with a live key"

// The rows that read Stripe in-process cannot run where the key is live, but the
// rest of their lane can: manual_test_scan.py reads the same account GET-only
// under --check production. So this names the reason and they skip on it, and
// the throw below stays as the backstop for a call that did not ask first. Any
// other failure here - absent credentials, a malformed secret - returns "" on
// purpose, so it surfaces where it happens instead of as a silent skip.
export function liveStripeSkipReason(): string {
  try {
    stripeKey()
    return ""
  } catch (e) {
    const message = (e as Error).message
    return message.includes(LIVE_STRIPE_REFUSAL) ? message : ""
  }
}

export function stripeKey(): string {
  if (stripeKeyCache) return stripeKeyCache
  // Same secret the app and manual_test_scan.py read: <env>-optinist/stripe/config
  // HEALTH_ENV is the deployed lanes' environment selector, so it decides this
  // too: a dev default here would audit dev's Stripe during a prod release run.
  const env =
    process.env.STRIPE_SECRET_ENV ||
    `${process.env.HEALTH_ENV || "development"}-optinist`
  const secret = JSON.parse(
    aws(
      `secretsmanager get-secret-value --region ${AWS_REGION} ` +
        `--secret-id ${env}/stripe/config --query SecretString --output text`,
    ),
  )
  const key = secret.secret_key
  if (!key) throw new Error(`${env}/stripe/config has no secret_key`)
  // A live key would make these reads production reads; the scan refuses one
  // without --allow-live and so does this.
  if (key.startsWith("sk_live")) {
    throw new Error(LIVE_STRIPE_REFUSAL)
  }
  stripeKeyCache = key
  return key
}

export function stripeGet(
  apiPath: string,
  params: Record<string, string | number> = {},
): Record<string, unknown> {
  const query = Object.entries(params)
    .map(
      ([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`,
    )
    .join("&")
  const url = `https://api.stripe.com${apiPath}${query ? "?" + query : ""}`
  const out = execSync(
    `curl -sS -H "Authorization: Bearer $STRIPE_KEY" ${JSON.stringify(url)}`,
    {
      env: { ...process.env, STRIPE_KEY: stripeKey() },
      stdio: ["pipe", "pipe", "pipe"],
      timeout: 30_000,
    },
  ).toString()
  const body = JSON.parse(out)
  if (body.error) {
    throw new Error(`Stripe GET ${apiPath}: ${JSON.stringify(body.error)}`)
  }
  return body
}

// The one Stripe subscription belonging to an account, by email.
export function stripeSubscriptionFor(email: string): Record<string, any> {
  const customers = stripeGet("/v1/customers", { email, limit: 10 })
    .data as Record<string, any>[]
  expect(
    customers.length,
    `Stripe customers for ${email} (exactly one expected)`,
  ).toBe(1)
  const subs = stripeGet("/v1/subscriptions", {
    customer: customers[0].id,
    status: "all",
    limit: 10,
  }).data as Record<string, any>[]
  const active = subs.filter((s) => ["active", "trialing"].includes(s.status))
  expect(active.length, `active Stripe subscriptions for ${email}`).toBe(1)
  return active[0]
}

// The per-user premium target group's health states, empty when the group does
// not exist. Shared by the premium lane and the disruptive lane, which both use
// it as the ALB's own answer to "is this user's instance really serving".
export function premiumTargetHealth(userId: number): string[] {
  try {
    const arn = execSync(
      `aws elbv2 describe-target-groups --names premium-${userId}-tg ` +
        `--region ${AWS_REGION} --query 'TargetGroups[0].TargetGroupArn' ` +
        `--output text`,
      { timeout: 30_000, stdio: ["pipe", "pipe", "pipe"] },
    )
      .toString()
      .trim()
    const out = execSync(
      `aws elbv2 describe-target-health --target-group-arn ${arn} ` +
        `--region ${AWS_REGION} ` +
        `--query 'TargetHealthDescriptions[].TargetHealth.State' --output text`,
      { timeout: 30_000, stdio: ["pipe", "pipe", "pipe"] },
    )
      .toString()
      .trim()
    return out ? out.split(/\s+/) : []
  } catch {
    return []
  }
}

// A dedicated assignment goes live (DB row, ALB rule, target group) before the
// premium ECS task on that instance serves traffic, so work driven through the
// ALB too early answers 502. Waiting out the task placement is not the row
// under test: a cluster that never gets a premium target serving leaves those
// rows unverified rather than failed. Shared by every lane that drives real
// work at a premium instance - `16-storage-aws` failed its first premium run
// for want of this gate, which is why it lives here rather than in one spec.
export async function skipUnlessPremiumTargetHealthy(
  rows: string,
  userId: number,
) {
  const deadline = Date.now() + 5 * 60_000
  let states: string[] = []
  for (;;) {
    states = premiumTargetHealth(userId)
    if (states.includes("healthy")) return
    if (Date.now() > deadline) break
    await new Promise((r) => setTimeout(r, 15_000))
  }
  test.skip(
    true,
    `rows ${rows}: premium-${userId}-tg never reported a healthy target ` +
      `(states: ${states.join(",") || "none"}) - the dev cluster could not ` +
      `keep a premium task serving; rerun when it has free CPU`,
  )
}

// The ALB half of "nothing is still held by us". A hard release can delete the
// assignment row and the listener rule but fail the target-group deletion,
// stranding a rule-less TG no sweep can find (issue #814) - so /premium/status
// reporting released is only half the invariant. Only a genuine NotFound counts
// as absence: premiumTargetHealth() is no substitute, it swallows every error
// and returns [] for an existing-but-empty group exactly as for a missing one.
export function expectPremiumTargetGroupGone(userId: number, stage: string) {
  try {
    execSync(
      `aws elbv2 describe-target-groups --names premium-${userId}-tg ` +
        `--region ${AWS_REGION}`,
      { timeout: 30_000, stdio: ["pipe", "pipe", "pipe"] },
    )
  } catch (e) {
    const msg =
      (e as Error).message + String((e as { stderr?: Buffer }).stderr || "")
    if (msg.includes("TargetGroupNotFound")) return
    throw e
  }
  throw new Error(`${stage}: premium-${userId}-tg still exists after release`)
}

// ---------------------------------------------------------------------------
// Premium pool inspection and staging, shared by the @prem lane and the
// disruptive lane's premium rows (OUT-04/05).
// ---------------------------------------------------------------------------

const PREMIUM_CLUSTER = "development-optinist-cloud-cluster"
const PREMIUM_ECS_SERVICE = "development-premium-optinist-cloud-service"

// Every premium instance the pool currently holds, in any live state. The
// migration rows need one the user is NOT on, whatever state it is parked in.
export function premiumInstances(): { id: string; state: string }[] {
  return awsJson<{ id: string; state: string }[]>(
    `ec2 describe-instances ` +
      `--filters Name=tag:Name,Values=development-premium-* ` +
      `Name=instance-state-name,Values=running,stopped,pending,stopping ` +
      `--query 'Reservations[].Instances[].{id:InstanceId,state:State.Name}'`,
  )
}

export function instanceState(instanceId: string): string {
  return premiumInstances().find((i) => i.id === instanceId)?.state ?? "gone"
}

export function runningPremiumInstanceIds(): string[] {
  const out = execSync(
    `aws ec2 describe-instances --region ${AWS_REGION} ` +
      `--filters "Name=tag:Name,Values=development-premium-*" ` +
      `"Name=instance-state-name,Values=running" ` +
      `--query 'Reservations[].Instances[].InstanceId' --output text`,
    { timeout: 30_000 },
  )
    .toString()
    .trim()
  return out ? out.split(/\s+/) : []
}

// Instance ids that currently hold a real (non-standby) premium user, by the
// same definition process_shared_instance_optimization uses to reject an
// instance as a migration target: status IN ('active','terminating') AND
// is_standby = 0. An instance carrying one of these can never satisfy the
// optimizer's `not real_users` gate, so a migration-target staging must route
// around it - see stageSecondRunningInstance.
export function occupiedPremiumInstanceIds(): Set<string> {
  const out = runSql(
    `SELECT DISTINCT instance_id FROM premium_user_assignments ` +
      `WHERE status IN ('active','terminating') AND is_standby = 0 ` +
      `AND user_id IS NOT NULL AND instance_id LIKE 'i-%';`,
  )
  return new Set(
    out
      ? out
          .split(/\s+/)
          .map((s) => s.trim())
          .filter(Boolean)
      : [],
  )
}

// The premium service's task on a given instance, as the console's Tasks tab
// shows it. Rows 1203/1204 are written against that view.
export function premiumTaskOn(
  instanceId: string,
): { taskArn: string; lastStatus: string } | undefined {
  const arns = awsJson<{ taskArns: string[] }>(
    `ecs list-tasks --cluster ${PREMIUM_CLUSTER} --service-name ${PREMIUM_ECS_SERVICE}`,
  ).taskArns
  if (!arns.length) return undefined
  const tasks = awsJson<{
    tasks: {
      taskArn: string
      lastStatus: string
      containerInstanceArn?: string
    }[]
  }>(
    `ecs describe-tasks --cluster ${PREMIUM_CLUSTER} --tasks ${arns.join(" ")}`,
  ).tasks
  const cis = tasks
    .map((t) => t.containerInstanceArn)
    .filter((a): a is string => !!a)
  if (!cis.length) return undefined
  const owners = awsJson<{
    containerInstances: {
      containerInstanceArn: string
      ec2InstanceId: string
    }[]
  }>(
    `ecs describe-container-instances --cluster ${PREMIUM_CLUSTER} ` +
      `--container-instances ${cis.join(" ")}`,
  ).containerInstances
  const mine = owners.find((c) => c.ec2InstanceId === instanceId)
  if (!mine) return undefined
  return tasks.find((t) => t.containerInstanceArn === mine.containerInstanceArn)
}

export function premiumTaskStatusOn(instanceId: string): string {
  return premiumTaskOn(instanceId)?.lastStatus ?? "none"
}

// The idle scale-down lives in the monitoring Lambda's scheduled action, so
// tests fire the same event cron does instead of waiting on the cron.
export function invokeMonitoringSweep(): string {
  const out = execSync(
    `aws lambda invoke --function-name development-premium-manager ` +
      `--payload '{"source":"aws.events","detail-type":"Scheduled Event"}' ` +
      `--cli-binary-format raw-in-base64-out --log-type Tail ` +
      `--query LogResult --output text --region ${AWS_REGION} /dev/null`,
    { timeout: 300_000 },
  )
    .toString()
    .trim()
  return Buffer.from(out, "base64").toString("utf-8")
}

// Stage a second RUNNING premium instance for a migration or recovery to land
// on.
//
// This is not scene-setting the rows using it could skip: the optimizer only
// migrates onto an instance it already considers available (running, no
// real users, ECS task ready). Without this step the optimizer does exactly
// the right thing - it logs "marked for migration: has_shared_flag=True"
// every pass and then "no running instances available. Triggering scaling to
// start stopped instances..." - but nothing ever starts, because the dev pool
// self-heals to a STOPPED standby and that scaling call does not wake it.
// A candidate must also host NO real user: the optimizer only accepts an
// instance with zero real users as a migration target, so one that already
// hosts another user is a decoy that can never be migrated onto.
// occupiedPremiumInstanceIds() screens those out; when every other running
// instance is occupied the create-standby branch below mints a genuinely empty
// one. The standby rows have to go as well - one makes try_reserve_instance
// answer "already reserved" and the optimizer skips the instance, which is what
// the DELETE ... user_id IS NULL statements below are for.
// Idempotent: with the candidate already staged, only the row re-delete does
// anything, so callers may re-run it after a wait.
export async function stageSecondRunningInstance(
  excludeId: string,
): Promise<string> {
  const p = progressLog("helpers", "stage-premium-target")
  const pickFree = () => {
    const occupied = occupiedPremiumInstanceIds()
    const free = premiumInstances().filter(
      (i) => i.id !== excludeId && !occupied.has(i.id),
    )
    // Settled first: a `pending`/`stopping` candidate costs the settle wait
    // below, and is one the manager is still cycling.
    return (
      free.find((i) => i.state === "running" || i.state === "stopped") ??
      free[0]
    )?.id
  }
  let candidate = pickFree()
  p.step(
    candidate
      ? `found free instance ${candidate} (${instanceState(candidate)})`
      : `no free instance beside ${excludeId}; asking the manager for one`,
  )
  if (!candidate) {
    // No free instance to migrate onto - either the user holds the only one,
    // or every other running instance already hosts a real user. The pool
    // self-heals to exactly ONE standby and the sweep terminates the extras,
    // so ask the manager for another. create_standby refuses while
    // get_standby_count() already meets PREMIUM_STANDBY_POOL_SIZE, so the
    // standby rows come out first.
    runSqlWriteOnDev(
      `DELETE FROM premium_user_assignments WHERE is_standby = 1
         AND user_id IS NULL`,
    )
    awsJson(
      `lambda invoke --function-name development-premium-manager ` +
        `--cli-binary-format raw-in-base64-out ` +
        `--payload '{"action":"create_standby"}' /dev/null`,
    )
    await expect
      .poll(
        () => {
          candidate = pickFree()
          p.tick("waiting for create_standby to mint an instance")
          return !!candidate
        },
        {
          timeout: 8 * 60_000,
          intervals: [20_000],
          message: "the manager never created a second premium instance",
        },
      )
      .toBe(true)
    p.step(`manager created ${candidate}`)
    // The Lambda answers with the id and stops the instance ~30s later, so a
    // start issued in that window is silently undone. Wait it out.
    await expect
      .poll(
        () => {
          p.tick(`waiting for ${candidate} to settle to stopped`)
          return instanceState(candidate!)
        },
        {
          timeout: 8 * 60_000,
          intervals: [20_000],
          message: `${candidate} never settled to stopped after creation`,
        },
      )
      .toBe("stopped")
  }
  runSqlWriteOnDev(
    `DELETE FROM premium_user_assignments WHERE instance_id = '${candidate}'
         AND user_id IS NULL`,
  )
  // The standby row is gone above, so the manager stops cycling the instance -
  // but it can still be mid-cycle right now, and start-instances rejects a
  // transitional one (IncorrectInstanceState). A standby is stopped ~30s after
  // it appears, so one the pool's own self-heal has just minted reads `pending`
  // or `stopping` for about a minute. The create-standby branch waits this out
  // for the instance IT asked for; every candidate needs the same.
  await expect
    .poll(
      () => {
        const state = instanceState(candidate!)
        expect(
          state,
          `${candidate} was terminated while it was being staged`,
        ).not.toBe("gone")
        p.tick(`waiting for ${candidate} to settle (${state})`)
        return state === "running" || state === "stopped"
      },
      {
        timeout: 8 * 60_000,
        intervals: [20_000],
        message: `${candidate} never left pending/stopping`,
      },
    )
    .toBe(true)
  if (instanceState(candidate!) !== "running") {
    awsJson(`ec2 start-instances --instance-ids ${candidate}`)
  }
  p.step(`starting ${candidate}`)
  await expect
    .poll(
      () => {
        p.tick(`waiting for ${candidate} to reach running`)
        return runningPremiumInstanceIds().includes(candidate!)
      },
      {
        timeout: 8 * 60_000,
        intervals: [15_000],
        message: `${candidate} never reached running`,
      },
    )
    .toBe(true)
  // A running EC2 is not yet a migration target: the readiness check the
  // optimizer runs demands a premium ECS task on it, so raise the service's
  // desired count to cover both instances and wait for the placement.
  const desired = awsJson<{ services: { desiredCount: number }[] }>(
    `ecs describe-services --cluster ${PREMIUM_CLUSTER} ` +
      `--services ${PREMIUM_ECS_SERVICE}`,
  ).services[0].desiredCount
  if (desired < 2) {
    awsJson(
      `ecs update-service --cluster ${PREMIUM_CLUSTER} ` +
        `--service ${PREMIUM_ECS_SERVICE} --desired-count 2`,
    )
  }
  await expect
    .poll(
      () => {
        p.tick(`waiting for a premium ECS task on ${candidate}`)
        return premiumTaskStatusOn(candidate!)
      },
      {
        timeout: 10 * 60_000,
        intervals: [20_000],
        message: `no premium ECS task ever placed on ${candidate}`,
      },
    )
    .toBe("RUNNING")
  // The delete above races the pool manager, which re-adds a standby row for
  // an instance it just started. Clear it again so the optimizer will take it.
  runSqlWriteOnDev(
    `DELETE FROM premium_user_assignments WHERE instance_id = '${candidate}'
         AND user_id IS NULL`,
  )
  // pickFree() read occupancy once, up to ~18 minutes of polling ago. If another
  // lane has claimed the candidate since, returning it hands the caller the exact
  // decoy this screening exists to remove, and the caller hangs to its own
  // timeout with the same symptom.
  expect(
    occupiedPremiumInstanceIds().has(candidate!),
    `${candidate} was claimed by a real user while it was being staged`,
  ).toBe(false)
  p.step(`staged ${candidate}`)
  return candidate!
}

// A @disruptive test degrades the shared environment while it runs - an outage,
// a stopped task, a filled disk - so it may only run when nobody else is on it.
// This asks the environment itself rather than trusting a coordination message:
// any recently-active session belonging to an account that is not one of ours is
// somebody's work in progress.
const IDLE_WINDOW_MINUTES = 30

export function otherActiveUsers(): string[] {
  const ours = [
    process.env.TEST_USER_EMAIL,
    process.env.TEST_PREMIUM_EMAIL,
    process.env.TEST_PREMIUM2_EMAIL,
    process.env.TEST_ADMIN_EMAIL,
    process.env.TEST_LIFECYCLE_EMAIL,
    process.env.TEST_STRIPE_EMAIL,
  ]
    .filter(Boolean)
    .map((email) => `'${sqlLiteral(email as string)}'`)
  const exclude = ours.length ? `AND u.email NOT IN (${ours.join(", ")})` : ""
  const rows = runSql(
    `SELECT u.email FROM free_user_assignments f JOIN users u ON u.id = f.user_id
       WHERE f.logged_out_at IS NULL
         AND f.last_activity > UTC_TIMESTAMP() - INTERVAL ${IDLE_WINDOW_MINUTES} MINUTE
         ${exclude}
     UNION
     SELECT u.email FROM premium_user_assignments p JOIN users u ON u.id = p.user_id
       WHERE p.last_activity > UTC_TIMESTAMP() - INTERVAL ${IDLE_WINDOW_MINUTES} MINUTE
         ${exclude};`,
  )
  return rows
    ? rows
        .split("\n")
        .map((r) => r.trim())
        .filter(Boolean)
    : []
}

// Returns the reason a @disruptive spec must not run right now, or "" if the
// environment is idle enough to disturb.
export function disruptiveSkipReason(): string {
  // A local or unset BASE_URL must skip, not sail past the in-use check below.
  if (isLocalBaseUrl()) {
    return "scales the deployed development tiers; BASE_URL is local or unset"
  }
  if (!process.env.RUN_DISRUPTIVE) {
    return "degrades the shared environment; opt in with RUN_DISRUPTIVE=1"
  }
  const sql = sqlSkipReason()
  if (sql) {
    // Refusing to disrupt is the safe answer when we cannot tell who is on.
    return `cannot check whether the environment is in use: ${sql}`
  }
  const others = otherActiveUsers()
  return others.length
    ? `the environment is in use by ${others.join(", ")} ` +
        `(active within ${IDLE_WINDOW_MINUTES} minutes) - disrupting it now would ` +
        `break their session`
    : ""
}

// Returns the reason a docker-driven spec cannot run here, or "" if it can.
export function localStackSkipReason(): string {
  if (!isLocalBaseUrl()) {
    return "spec mutates the local docker DB; BASE_URL is not local"
  }
  try {
    runSql("SELECT 1;")
  } catch {
    return "local docker db container not reachable"
  }
  return ""
}

// Returns the reason SQL is unreachable here, or "" if it can run. Unlike
// localStackSkipReason this accepts the deployed RDS, for specs that only read
// or write rows and need no docker-only helper.
export function sqlSkipReason(): string {
  try {
    runSql("SELECT 1;")
  } catch (e) {
    return isLocalBaseUrl()
      ? "local docker db container not reachable"
      : `deployed RDS not reachable over SSM: ${(e as Error).message.split("\n")[0]}`
  }
  return ""
}

export function sqlLiteral(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/'/g, "''")
}

// Storage state saved after a single UI login; authed specs reuse it so each run
// needs only a handful of Firebase logins (rate limits)
const AUTH_DIR = path.join(__dirname, ".auth")
export const FREE_STORAGE_STATE = path.join(AUTH_DIR, "free.json")
export const ADMIN_STORAGE_STATE = path.join(AUTH_DIR, "admin.json")

export function freeStorageState(): string | undefined {
  return fs.existsSync(FREE_STORAGE_STATE) ? FREE_STORAGE_STATE : undefined
}

// Resolved per test rather than at module load, because the admin account does
// not exist until the spec's own beforeAll has registered and promoted it
export function adminStorageState(): string | undefined {
  return fs.existsSync(ADMIN_STORAGE_STATE) ? ADMIN_STORAGE_STATE : undefined
}

// One UI login, saved for reuse. Retries because a cold CRA dev server can take
// longer than the login timeout to compile the login route.
export async function saveStorageState(
  statePath: string,
  email: string,
  password: string,
  baseURL = process.env.BASE_URL || "http://localhost:3000",
) {
  fs.mkdirSync(path.dirname(statePath), { recursive: true })
  const browser = await chromium.launch()
  try {
    // storageState must be cleared explicitly: a page opened inside a spec's
    // `test.use()` scope inherits that scope's state, so this one would start
    // already signed in as the previous run, be redirected off /login before
    // the form is filled, and save that stale token back over the file.
    const page = await browser.newPage({ baseURL, storageState: undefined })
    for (let attempt = 1; ; attempt++) {
      await page.goto("/login")
      await page.locator('[data-testid="email"]').fill(email)
      await page.locator('[data-testid="password"]').fill(password)
      await page.locator('[data-testid="button-submit"]').click()
      try {
        await page.waitForURL(/\/dashboard/, { timeout: 60_000 })
        break
      } catch (e) {
        if (attempt >= 3) throw e
      }
    }
    await page.context().storageState({ path: statePath })
  } finally {
    await browser.close()
  }
}

// For specs running with the saved storage state: land on the dashboard
// without going through the login form
export async function gotoDashboard(page: Page) {
  await page.goto("/dashboard")
  await expect(page).toHaveURL(/\/dashboard/, { timeout: 30_000 })
  await dismissStorageWarning(page)
}

// Credentials come from env vars or e2e/.env (gitignored) — never commit them.
export const FREE_USER = {
  email: process.env.TEST_USER_EMAIL || "",
  password: process.env.TEST_USER_PASSWORD || "",
}
export const PREMIUM_USER = {
  email: process.env.TEST_PREMIUM_EMAIL || "",
  password: process.env.TEST_PREMIUM_PASSWORD || "",
}
export const UNVERIFIED_USER = {
  email: process.env.TEST_UNVERIFIED_EMAIL || "",
  password: process.env.TEST_UNVERIFIED_PASSWORD || "",
}

export function skipWithoutCreds(
  user: { email: string; password: string } = FREE_USER,
  name = "TEST_USER_EMAIL/TEST_USER_PASSWORD",
) {
  test.skip(!user.email || !user.password, `${name} not set`)
}

export async function login(
  page: Page,
  email = FREE_USER.email,
  password = FREE_USER.password,
  dismissWarning = true,
) {
  for (let attempt = 0; attempt < 3; attempt++) {
    await page.goto("/login")
    await expect(page.locator('[data-testid="button-submit"]')).toBeVisible({
      timeout: 30_000,
    })
    await page.locator('[data-testid="email"]').fill(email)
    await page.locator('[data-testid="password"]').fill(password)
    await page.locator('[data-testid="button-submit"]').click()
    try {
      await expect(page).toHaveURL(/\/dashboard/, { timeout: 15_000 })
      if (dismissWarning) await dismissStorageWarning(page)
      return
    } catch {
      // Login timed out — retry
    }
  }
  throw new Error(`Login failed after 3 attempts for ${email}`)
}

export async function logout(page: Page) {
  await page.locator('[aria-label="open profile menu"]').click()
  await page.locator('li:has-text("Sign Out")').click()
  // A "Jobs Running" dialog may appear if jobs are active
  const signOutAnyway = page.locator('button:has-text("Sign Out Anyway")')
  try {
    await expect(signOutAnyway).toBeVisible({ timeout: 2_000 })
    await signOutAnyway.click()
  } catch {
    // No running jobs — continue
  }
  await expect(page).toHaveURL(/\/login/, { timeout: 15_000 })
}

export async function dismissStorageWarning(page: Page) {
  const handleLater = page.locator('button:has-text("Handle later")')
  try {
    await expect(handleLater).toBeVisible({ timeout: 3_000 })
    await handleLater.click()
  } catch {
    // No storage warning — continue
  }
}

export async function goToWorkspaces(page: Page) {
  await page.goto("/workspaces")
  await expect(page.locator("text=Workspaces").first()).toBeVisible({
    timeout: 15_000,
  })
}

// Shared workspace for data-dependent specs (03/05/06/07): sample data is
// imported once per run; global-setup deletes all e2e-* workspaces at start
export const DATA_WS = "e2e-data"

export function apiUrl(): string {
  const baseURL = process.env.BASE_URL || "http://localhost:3000"
  return process.env.API_URL || baseURL.replace(/:\d+$/, ":8000")
}

// A backend on USE_FIREBASE_TOKEN=False validates ExToken, not the Bearer
// token, so both have to be sent to cover either auth mode
export function authHeaders(token: string, exToken?: string | null) {
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` }
  if (exToken) headers.ExToken = exToken
  return headers
}

// A request context authenticated by API login, for work that has no page:
// global-setup (before any browser exists) and the cleanup group
export async function apiLogin(
  email = FREE_USER.email,
  password = FREE_USER.password,
): Promise<{ api: APIRequestContext; headers: Record<string, string> }> {
  const api = await request.newContext({ baseURL: apiUrl() })
  const res = await api.post("/auth/login", { data: { email, password } })
  if (!res.ok()) {
    // Read the body before dispose(), which tears the response down with it
    const body = await res.text()
    await api.dispose()
    throw new Error(
      `${email} rejected by ${apiUrl()}/auth/login ` +
        `(${res.status()}): ${body}`,
    )
  }
  const { access_token, ex_token } = await res.json()
  return { api, headers: authHeaders(access_token, ex_token) }
}

// Deletes the account's e2e-* workspaces and returns the names removed.
// DELETE /workspace/{id} also drops the workspace's input/output data, in S3
// too when remote storage is on.
export async function deleteE2eWorkspaces(
  api: APIRequestContext,
  headers: Record<string, string>,
): Promise<string[]> {
  const list = await api.get("/workspaces?offset=0&limit=100", { headers })
  if (!list.ok()) {
    throw new Error(`GET /workspaces ${list.status()}: ${await list.text()}`)
  }
  const { items } = await list.json()
  const deleted: string[] = []
  for (const ws of items as { id: number; name: string }[]) {
    if (!/^e2e-/.test(ws.name)) continue
    const res = await api.delete(`/workspace/${ws.id}`, { headers })
    if (res.ok()) deleted.push(ws.name)
  }
  return deleted
}

// The app keeps its tokens in localStorage, so page.request needs them
// passed explicitly; the page must already be on the app origin
export async function apiHeaders(page: Page) {
  const { token, exToken } = await page.evaluate(() => ({
    token: localStorage.getItem("access_token"),
    exToken: localStorage.getItem("ExToken"),
  }))
  if (!token) {
    throw new Error(
      "No access_token in localStorage — is the storage state stale? Delete e2e/.auth and rerun.",
    )
  }
  return authHeaders(token, exToken)
}

// Find-or-create a workspace via the API — avoids the virtualized grid
// (row ordering, render races) for tests that aren't about the grid itself
export async function ensureWorkspaceId(
  page: Page,
  name: string,
): Promise<number> {
  const headers = await apiHeaders(page)
  const list = await page.request.get(
    `${apiUrl()}/workspaces?offset=0&limit=100`,
    { headers },
  )
  if (!list.ok()) {
    throw new Error(`GET /workspaces ${list.status()}: ${await list.text()}`)
  }
  const { items } = await list.json()
  const found = items.find((w: { name: string }) => w.name === name)
  if (found) return found.id
  const created = await page.request.post(`${apiUrl()}/workspace`, {
    headers,
    data: { name },
  })
  if (!created.ok()) {
    throw new Error(
      `POST /workspace ${created.status()}: ${await created.text()}`,
    )
  }
  return (await created.json()).id
}

export async function openWorkspace(page: Page, name: string): Promise<number> {
  const id = await ensureWorkspaceId(page, name)
  await page.goto(`/workspaces/${id}`)
  await expect(
    page.locator('button[role="tab"]:has-text("Workflow")'),
  ).toBeVisible({ timeout: 15_000 })
  return id
}

export async function importSampleData(page: Page, workspaceName: string) {
  // The import silently no-ops until GET /workspace/{id} populates the store,
  // and the Record tab renders nothing that proves it has. Probe on Workflow,
  // then switch to Record, where the menu entry is enabled.
  await page.locator('button[role="tab"]:has-text("Workflow")').click()
  await expect(page.locator(`text=${workspaceName}`).first()).toBeVisible({
    timeout: 15_000,
  })
  await page.locator('button[role="tab"]:has-text("Record")').click()
  await page.locator('[data-testid="MenuBookIcon"]').click()
  await page.getByText("Import sample data").click()
  const dialog = page.locator('[role="dialog"]:has-text("Import sample data?")')
  await expect(dialog).toBeVisible()
  await dialog.getByRole("button", { name: "OK" }).click()
  await expect(page.locator("text=Sample data import success")).toBeVisible({
    timeout: 120_000,
  })
}

// Go to the Record tab; import sample data first if no records exist yet
export async function ensureTutorialRecords(page: Page, workspaceName: string) {
  await page.locator('button[role="tab"]:has-text("Record")').click()
  const hasRecords = await page
    .locator('[data-testid="reproduce-button"]')
    .first()
    .waitFor({ timeout: 10_000 })
    .then(() => true)
    .catch(() => false)
  if (!hasRecords) {
    await importSampleData(page, workspaceName)
    await expect(
      page.locator('[data-testid="reproduce-button"]').first(),
    ).toBeVisible({ timeout: 30_000 })
  }
}

export async function reproduceTutorial(page: Page, tutorialName: string) {
  await page.locator('button[role="tab"]:has-text("Record")').click()
  const row = page.locator(`tr:has-text("${tutorialName}")`).first()
  await expect(row).toBeVisible({ timeout: 15_000 })
  // Disabled while a previous run is still settling — wait, then reload
  // once (a fresh fetch clears stale running state)
  const reproduceButton = row.locator('[data-testid="reproduce-button"]')
  const enabled = await expect(reproduceButton)
    .toBeEnabled({ timeout: 60_000 })
    .then(() => true)
    .catch(() => false)
  if (!enabled) {
    await page.reload()
    await page.locator('button[role="tab"]:has-text("Record")').click()
    await expect(row).toBeVisible({ timeout: 15_000 })
    await expect(reproduceButton).toBeEnabled({ timeout: 60_000 })
  }
  await reproduceButton.click()
  const dialog = page.locator('[role="dialog"]:has-text("Reproduce workflow?")')
  await expect(dialog).toBeVisible({ timeout: 10_000 })
  await dialog.locator('[data-testid="reproduce-confirm-button"]').click()
  await expect(page.locator("text=Successfully reproduced.")).toBeVisible({
    timeout: 60_000,
  })
  // Reproduce loads the workflow into the flowchart tab
  await expect(
    page.locator('button[role="tab"]:has-text("Workflow")'),
  ).toHaveAttribute("aria-selected", "true", { timeout: 30_000 })
}

// The run split button: after a reproduce it defaults to by-uid "RUN"
// (immediate, same uid, reuses existing outputs); "RUN ALL" starts a fresh
// run under a new uid via the name dialog. Select the mode explicitly so a
// test exercises the path it claims to.
export async function startRun(page: Page, mode: "RUN" | "RUN ALL") {
  await page.locator('button:has([data-testid="ArrowDropDownIcon"])').click()
  await page.locator(`li:text-is("${mode}")`).click()
  // Anchor on the run POST actually going out (the click's storage
  // pre-check makes the dispatch async, and navigating away too early
  // silently cancels it)
  const runStarted = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      /\/run\//.test(r.url()) &&
      !r.url().includes("/run/result"),
  )
  await page.locator(`button:text-is("${mode}")`).click()
  if (mode === "RUN ALL") {
    const dialog = page.locator(
      '[role="dialog"]:has-text("Name and run workflow")',
    )
    await expect(dialog).toBeVisible({ timeout: 15_000 })
    // Must not contain "Tutorial" — record-row locators match by substring
    await dialog.locator("input").fill("e2e-runall")
    await dialog.getByRole("button", { name: "Run", exact: true }).click()
  }
  // Both /run/{ws} and /run/{ws}/{uid} answer the run's uid as a bare string.
  // Assert the status first: an ALB 502 answers HTML, and parsing that as the
  // uid reports a JSON SyntaxError instead of the status that caused it.
  const response = await runStarted
  expect(
    response.ok(),
    `run POST ${response.url()} answered ${response.status()}: ` +
      `${(await response.text()).slice(0, 200)}`,
  ).toBe(true)
  return {
    workspaceId: response.url().match(/\/run\/(\d+)/)?.[1] ?? "",
    uid: String(await response.json()),
  }
}

// experiment.yaml's own success flag, which finalization writes: "running"
// until the pipeline settles, then "success" or "error".
async function recordedRunStatus(
  page: Page,
  workspaceId: string,
  uid: string,
): Promise<string> {
  const headers = await routedApiHeaders(page)
  const res = await page.request.get(`${apiUrl()}/experiments/${workspaceId}`, {
    headers,
  })
  if (!res.ok()) return `GET /experiments/${workspaceId} -> ${res.status()}`
  const experiments = (await res.json()) as Record<string, { success?: string }>
  return experiments?.[uid]?.success ?? "absent"
}

// Reproduce a tutorial and run it to completion. Loading an already-finished
// experiment fires a phantom "Workflow finished" snackbar, so wait it out
// before waiting for the real one. "RUN ALL" = fresh uid, full pipeline
// compute (5-10 min, @slow); "RUN" = by-uid rerun, which snakemake treats
// as already complete for the imported tutorials (seconds).
// A real snakemake run. The ceiling is environment-dependent: the docker stack
// CI uses clears a tutorial well inside the default, a deployed environment can
// take far longer, so RUN_TIMEOUT_MS raises it without a code edit.
export const RUN_TIMEOUT_MS = Number(process.env.RUN_TIMEOUT_MS ?? 840_000)
// After the snackbar, how long the recorded result may take to settle
const RECORD_TIMEOUT_MS = 300_000
// The surrounding reproduce/start steps need headroom above the run itself
export const RUN_TEST_TIMEOUT_MS = RUN_TIMEOUT_MS + RECORD_TIMEOUT_MS + 60_000

export async function awaitRunFinished(
  page: Page,
  tutorialName: string,
  workspaceId: string,
  uid: string,
) {
  const finished = page.locator("text=Workflow finished")
  const aborted = page.locator("text=Workflow aborted")
  // Race the two terminal snackbars: waiting on success alone burns the whole
  // ceiling on a run that already died, and reports it as a plain timeout
  await expect(finished.or(aborted).first()).toBeVisible({
    timeout: RUN_TIMEOUT_MS,
  })
  await expect(
    aborted,
    `${tutorialName} aborted instead of finishing`,
  ).toHaveCount(0)

  // The snackbar alone is not proof. Where the workflow PID file is not visible
  // to the API process, unrun nodes are reported errored, no node is left
  // pending, and the frontend calls that FINISHED seconds into the run.
  let recorded = ""
  await expect
    .poll(
      async () => (recorded = await recordedRunStatus(page, workspaceId, uid)),
      {
        timeout: RECORD_TIMEOUT_MS,
        message: `${tutorialName} (${uid}) never settled in experiment.yaml`,
      },
    )
    .toMatch(/^(success|error)$/)
  expect(recorded, `${tutorialName} recorded "${recorded}", not success`).toBe(
    "success",
  )
}

export async function runTutorial(
  page: Page,
  tutorialName: string,
  mode: "RUN" | "RUN ALL" = "RUN ALL",
) {
  await reproduceTutorial(page, tutorialName)
  const { workspaceId, uid } = await startRun(page, mode)
  await expect(page.locator("text=Workflow finished")).toBeHidden({
    timeout: 30_000,
  })
  await awaitRunFinished(page, tutorialName, workspaceId, uid)
  return { workspaceId, uid }
}

// Mints the success record + thumbnails the dataview needs.
//
// This is NOT a no-op: `git ls-files sample_data/tutorial/output` is 12 files,
// all of them experiment/snakemake/workflow YAML. No node output directories, no
// JSON, no NWB ship at all, and global setup deletes the e2e-* workspace each
// run, so snakemake recomputes from scratch. Budget a real run (see RUN_TIMEOUT_MS
// inner wait in runTutorial), not the ~15s a rerun-by-uid would take.
export async function ensureCompletedTutorialRun(
  page: Page,
  wsName: string,
  tutorialName = "Tutorial1",
) {
  await ensureTutorialRecords(page, wsName)
  await runTutorial(page, tutorialName, "RUN")
}

// Publishing requires a cloud bucket on the account (the backend 400s
// without one). Local-stack users have none, so set a placeholder attribute
// - the S3-existence check is skipped in local storage mode, and all S3
// size lookups swallow errors. Deployed users have real buckets, and
// overwriting one with the placeholder breaks publishing for that account.
export function ensurePublishableAccount() {
  if (!isLocalBaseUrl()) return
  const email = process.env.TEST_USER_EMAIL
  if (!email) return
  try {
    runSql(
      `UPDATE users SET attributes = JSON_SET(
         COALESCE(attributes, JSON_OBJECT()),
         '$.remote_bucket_name', 'e2e-local-placeholder')
       WHERE email = '${sqlLiteral(email)}';`,
    )
  } catch {
    // Not a local stack
  }
}

type DataviewItem = {
  id: number
  name?: string
  publish_status?: number
  thumbnails?: { image_url?: string | null }
}

// Scoped to DATA_WS: an unscoped name match could publish (and expose) a
// same-named record from another workspace on a shared environment
let dataWsId = 0

export async function findDataviewRecord(
  page: Page,
  name: string,
): Promise<DataviewItem | undefined> {
  if (!dataWsId) dataWsId = await ensureWorkspaceId(page, DATA_WS)
  const headers = await apiHeaders(page)
  const res = await page.request.get(
    `${apiUrl()}/api/dataview?limit=100&offset=0&workspace_id=${dataWsId}`,
    { headers },
  )
  if (!res.ok()) {
    throw new Error(`GET /api/dataview ${res.status()}: ${await res.text()}`)
  }
  const { items } = await res.json()
  return (items as DataviewItem[]).find((record) => record.name === name)
}

export async function setPublished(page: Page, name: string, on: boolean) {
  const record = await findDataviewRecord(page, name)
  if (!record) {
    if (!on) return
    throw new Error(`no dataview record named ${name} to publish`)
  }
  const headers = await apiHeaders(page)
  // Publish syncs and validates against S3 in-request on a deployed env, which
  // outlasts the default request timeout
  const res = await page.request.post(
    `${apiUrl()}/api/dataview/publish/${record.id}/${on ? "on" : "off"}`,
    { headers, timeout: 120_000 },
  )
  if (!res.ok()) {
    throw new Error(
      `publish ${name} ${on} -> ${res.status()}: ${await res.text()}`,
    )
  }
}

// Mint-or-find the success record, then publish it through the API - the
// assertions stay on the public UI.
export async function ensurePublishedRecord(page: Page, tutorialName: string) {
  const existing = await findDataviewRecord(page, tutorialName)
  if (!existing) {
    // Minting costs a real workflow run (see RUN_TIMEOUT_MS). RUN_TIMEOUT_MS is
    // env-overridable, so raise a caller's budget, never lower it.
    test.setTimeout(Math.max(test.info().timeout, RUN_TEST_TIMEOUT_MS))
    await openWorkspace(page, DATA_WS)
    await ensureCompletedTutorialRun(page, DATA_WS, tutorialName)
    // The record registers slightly after "Workflow finished"
    await expect
      .poll(async () => Boolean(await findDataviewRecord(page, tutorialName)), {
        timeout: 90_000,
      })
      .toBe(true)
  }
  await setPublished(page, tutorialName, true)
}

// Shared routing contract fixture: sourcing the mock bodies from it keeps them
// on the real response shape and guarantees no body carries a user_id.
const PREMIUM_CONTRACT = JSON.parse(
  fs.readFileSync(
    path.join(
      __dirname,
      "..",
      "src",
      "utils",
      "routing",
      "__fixtures__",
      "premium_routing",
      "premium_contract.json",
    ),
    "utf-8",
  ),
)

// Route-mock an active premium assignment (status + heartbeat + beacon token)
// for assignment-dependent UI where the real AWS flow is unreachable. No
// X-Routing-ID header: the local backend runs non-standalone and rejects a
// request carrying a routing_id it did not mint, so seeding a fake one here
// would 403 every later real request. Seeding is covered at the jest
// interceptor level instead.
//
// `shared` mocks the shared-resource fallback instead of a dedicated instance.
// That is a different frontend branch: with no dedicated instance the app keeps
// its "being prepared" notice up rather than announcing success, and it records
// the fallback in the routing service.
// `scaling` mocks the instance-still-starting state: no assignment yet, and the
// assign call answers the retryable scaling shape the app polls on.
export async function mockPremiumAssignment(
  page: Page,
  {
    shared = false,
    scaling = false,
  }: { shared?: boolean; scaling?: boolean } = {},
) {
  const status = PREMIUM_CONTRACT.premium_status
  await page.route("**/users/me/premium/status", (route) =>
    route.fulfill({
      json: {
        ...status,
        assignment: scaling
          ? null
          : {
              ...status.assignment,
              assigned_at: new Date().toISOString(),
              is_shared: shared,
            },
      },
    }),
  )
  if (scaling) {
    // The retry branch emits exactly these fields (response_model_exclude_unset
    // strips the rest), so the mock must not carry instance fields with it
    await page.route("**/users/me/premium/assign", (route) =>
      route.fulfill({
        json: {
          message: "Premium instance is being prepared",
          assigned: false,
          scaling_in_progress: true,
          retry_after: 30,
        },
      }),
    )
  }
  await page.route("**/users/me/premium/heartbeat", (route) =>
    route.fulfill({ json: PREMIUM_CONTRACT.premium_heartbeat }),
  )
  await page.route("**/users/me/premium/beacon-token", (route) =>
    route.fulfill({ json: { token: "e2e" } }),
  )
}

// Server-side filter on the Workspace column, which is only offered where
// DataviewRecords renders without a workspaceId: /dataview and /public
export async function filterWorkspace(page: Page, value: string) {
  const header = page.locator(
    '.MuiDataGrid-columnHeader[data-field="workspace_name"]',
  )
  await header.hover()
  await header.locator(".MuiDataGrid-menuIcon button").click()
  await page.getByRole("menuitem", { name: /^filter$/i }).click()
  await page.locator(".MuiDataGrid-filterForm input").last().fill(value)
  await page.keyboard.press("Escape")
}

export async function createWorkspace(page: Page, name: string) {
  await goToWorkspaces(page)
  await page.locator('button:has-text("New")').first().click()
  const dialog = page.locator('[role="dialog"]')
  await expect(dialog).toBeVisible()
  await dialog.locator('[placeholder="Workspace Name"]').fill(name)
  await dialog.locator('button:has-text("Ok")').click()
  await expect(dialog).toBeHidden({ timeout: 15_000 })
  await expect(page.locator(`text=${name}`).first()).toBeVisible({
    timeout: 30_000,
  })
}
