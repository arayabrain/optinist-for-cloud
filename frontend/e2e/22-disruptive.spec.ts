import { test, expect, request, Page } from "@playwright/test"

import {
  AWS_REGION,
  CLOUDWATCH_POLL,
  PREMIUM_USER,
  PUBLIC_LOG_GROUP,
  apiHeaders,
  apiLogin,
  apiUrl,
  awsJson,
  cloudwatchHas,
  disruptiveSkipReason,
  invokeMonitoringSweep,
  login,
  openWorkspace,
  premiumTargetHealth,
  progressLog,
  runSql,
  sqlSkipReason,
  stageSecondRunningInstance,
  windowStart,
} from "./helpers"

// Tests that have to break the environment to observe anything. Every one is
// tagged @disruptive, so it is filtered out unless RUN_DISRUPTIVE=1, and
// disruptiveSkipReason() additionally refuses to start when the database shows
// somebody else active in the last 30 minutes.
//
//   RUN_DISRUPTIVE=1 npx playwright test e2e/22-disruptive.spec.ts --retries 0
//
// Each restores what it disturbed, and asserts the restore: leaving the free
// tier scaled to zero is worse than a red test. The restore is in a finally
// block so a failed assertion mid-outage still puts the tier back.

const CLUSTER = "development-optinist-cloud-cluster"
const FREE_SERVICE = "development-optinist-cloud-service"
const PUBLIC_SERVICE = "development-public-optinist-cloud-service"

type Service = {
  desiredCount: number
  runningCount: number
  status: string
  deployments: { id: string; status: string; rolloutState?: string }[]
}

function describeService(service: string): Service {
  const out = awsJson<{ services: Service[] }>(
    `ecs describe-services --cluster ${CLUSTER} --services ${service} ` +
      `--region ${AWS_REGION}`,
  )
  expect(out.services.length, `ECS knows service ${service}`).toBe(1)
  return out.services[0]
}

function scaleService(service: string, desired: number): void {
  awsJson(
    `ecs update-service --cluster ${CLUSTER} --service ${service} ` +
      `--desired-count ${desired} --region ${AWS_REGION}`,
  )
}

type PublicRecord = {
  uid?: string
  name?: string
  workspace?: { id: number }
}

// A published record, read the way an anonymous visitor reads it. Only the
// public tier serves this route (ALB p300), so it is also the tier under test.
async function publishedRecord(): Promise<PublicRecord | undefined> {
  const anon = await request.newContext({ baseURL: apiUrl() })
  try {
    const res = await anon.get("/api/public/dataview?limit=100&offset=0")
    expect(res.ok(), `GET /api/public/dataview: ${await res.text()}`).toBe(true)
    const { items } = (await res.json()) as { items: PublicRecord[] }
    return items.find((r) => r.uid && r.workspace?.id)
  } finally {
    await anon.dispose()
  }
}

// 200 = served from the EFS cache. 202 = published but not synced, which is the
// route's own way of saying it had to go back to S3 - the failure this row is
// about.
async function reproduceStatus(record: PublicRecord): Promise<number> {
  const anon = await request.newContext({ baseURL: apiUrl() })
  try {
    const res = await anon.get(
      `/api/public/dataview/workflow/reproduce/${record.workspace!.id}/` +
        `${record.uid}`,
    )
    return res.status()
  } finally {
    await anon.dispose()
  }
}

async function pollService(
  service: string,
  want: (s: Service) => boolean,
  what: string,
): Promise<void> {
  await expect
    // A replacement task can sit PENDING for minutes before the agent even
    // starts pulling, on a cluster this close to its CPU reservation.
    .poll(() => want(describeService(service)), {
      timeout: 600_000,
      intervals: [10_000],
      message: `${service} never reached: ${what}`,
    })
    .toBe(true)
}

// Computed in a hook rather than at collection: the check reads the database,
// and collection happens for every run whether this lane is included or not.
// Per test, not once per file: the lane runs long enough that a verdict taken
// before the first test would be an hour stale by the last, and "nobody else is
// using dev" is exactly the fact that goes stale.
function guardDisruptive(): void {
  const reason = disruptiveSkipReason()
  test.skip(!!reason, reason)
  // This lane scales real services. Never point it at production.
  expect(
    process.env.BASE_URL || "",
    "this lane only runs against the development environment",
  ).toContain("development-optinist")
}

// The scheduler stops this environment at 13:00 UTC on weekdays and verifies
// the stop at 13:15, rewriting min and desired to 0. A test that straddles that
// never sees what it is polling for, and on a Friday nothing recovers until
// Sunday 23:00 UTC. Every test here polls for longer than it takes to notice.
function skipIfTooCloseToScheduledStop(minutes: number): void {
  const now = new Date()
  const at = now.toISOString().slice(11, 16)
  // Up = start cron(0 23 ? * SUN-THU) .. stop cron(0 13 ? * MON-FRI), so
  // 23:00 -> 13:00 UTC on weekdays. Asked separately from the deadline below:
  // outside the window there is no stop to be too close to, and a lane run
  // against a scaled-to-zero environment reports ALB 503s as locator timeouts.
  const day = now.getUTCDay()
  const hour = now.getUTCHours()
  test.skip(
    !((hour >= 23 && day <= 4) || (hour < 13 && day >= 1 && day <= 5)),
    `the dev environment is stopped at ${at} UTC; it runs 23:00 -> 13:00 UTC ` +
      `on weekdays`,
  )
  // The next 13:00 UTC, not today's: the environment starts at 23:00 UTC, so
  // for most of a session today's stop is already behind us. Saturday and
  // Sunday carry no stop at all -- the rule only fires Mon-Fri.
  const stop = new Date(now)
  stop.setUTCHours(13, 0, 0, 0)
  while (stop <= now || stop.getUTCDay() === 0 || stop.getUTCDay() === 6) {
    stop.setUTCDate(stop.getUTCDate() + 1)
  }
  const left = Math.floor((stop.getTime() - now.getTime()) / 60_000)
  test.skip(
    left < minutes,
    `this test can run for ${minutes} minutes and the 13:00 UTC scheduled ` +
      `stop is ${left} minutes away (${at} UTC)`,
  )
}

test.describe("Disruptive: the free tier goes away @disruptive", () => {
  test.beforeEach(guardDisruptive)

  // A Playwright timeout aborts the test body without running its finally, so
  // the in-test restore is not the last line of defence it looks like. Back to
  // one task: every test here refuses to start below that.
  test.afterEach(async () => {
    if (describeService(FREE_SERVICE).desiredCount === 0) {
      scaleService(FREE_SERVICE, 1)
      console.log(`restored ${FREE_SERVICE} to 1 task after an aborted outage`)
    }
  })

  // Rows 809 / 810 / 812: the public tier serves the shell and /auth/login off
  // its own target group, so a free tier at zero tasks must not take the front
  // door down with it. Asserted by actually taking it down.
  test("OUT-01 - With the free service at zero, the public tier still serves", async () => {
    // Above the sum of the polls below: 600s scale-down, 240s CloudWatch, 600s
    // restore. A test timeout would abort the restore instead of failing it.
    test.setTimeout(1_800_000)
    skipIfTooCloseToScheduledStop(30)
    const before = describeService(FREE_SERVICE)
    expect(
      before.desiredCount,
      "the free service must be up before this test takes it down",
    ).toBeGreaterThan(0)

    const anon = await request.newContext()
    const start = Date.now()
    try {
      scaleService(FREE_SERVICE, 0)
      await pollService(FREE_SERVICE, (s) => s.runningCount === 0, "0 tasks")

      // Row 809: the shell, unauthenticated, through the same ALB
      const shell = await anon.get(`${process.env.BASE_URL}/`)
      expect(shell.status(), "the public shell during the free outage").toBe(
        200,
      )

      // Row 810: the login route, which is a public-tier rule rather than the
      // Bearer catch-all. A wrong 503 here is the rule ordering being wrong.
      // A syntactically valid address on purpose: UserAuth.email is an EmailStr,
      // so an .invalid address is refused 422 by Pydantic before the handler
      // runs, which proves only that some FastAPI app is mounted. This reaches
      // the auth path and comes back 400 INVALID_LOGIN_CREDENTIALS.
      const login = await anon.post(`${process.env.BASE_URL}/auth/login`, {
        data: { email: "nobody@example.com", password: "x" },
        failOnStatusCode: false,
      })
      expect(
        login.status(),
        `/auth/login answered ${login.status()} during the outage - a 5xx means ` +
          `the route followed the free tier down, a 404 that it is unmounted`,
      ).toBe(400)

      // Row 812: the error report itself, not merely that the tier is logging.
      // /log-report/frontend-errors is an ALB rule onto the public tier (p307)
      // and requires a token, so a 200 here also proves login worked during the
      // outage. The browser half - queueing errors until sign-in and shipping
      // them - is PUB-04/PUB-07 on a healthy tier; what this row adds is that
      // the path survives the free tier being gone.
      const marker = `e2e-out01 ${Date.now()}`
      const { api, headers } = await apiLogin()
      try {
        const report = await api.post("/log-report/frontend-errors", {
          headers,
          data: { errors: [{ level: "error", message: marker }] },
        })
        expect(
          report.status(),
          `/log-report/frontend-errors during the outage: ${await report.text()}`,
        ).toBe(200)
      } finally {
        await api.dispose()
      }
      await expect
        .poll(() => cloudwatchHas(PUBLIC_LOG_GROUP, marker, start), {
          ...CLOUDWATCH_POLL,
          message: `${marker} never reached ${PUBLIC_LOG_GROUP}`,
        })
        .toBe(true)
    } finally {
      scaleService(FREE_SERVICE, before.desiredCount)
      await pollService(
        FREE_SERVICE,
        (s) => s.runningCount === before.desiredCount,
        `back to ${before.desiredCount} task(s)`,
      )
      await anon.dispose()
    }

    // Restored, and serving authenticated traffic again
    const after = describeService(FREE_SERVICE)
    expect(after.desiredCount, "the free service is back").toBe(
      before.desiredCount,
    )
    expect(after.status, "the free service is ACTIVE again").toBe("ACTIVE")
  })

  // Row 819: a rolling deployment of the public tier must not interrupt what it
  // serves. force-new-deployment is the same action a release performs.
  test("OUT-02 - A rolling public-tier deployment keeps serving throughout", async () => {
    // Each replaced task can spend the full 600s deregistration delay
    // draining, and the tier runs two tasks, so the rollout's worst case runs
    // to roughly half an hour once per-task placement time is included.
    test.setTimeout(3_300_000)
    skipIfTooCloseToScheduledStop(55)
    const p = progressLog("22-disruptive", "OUT-02")
    const before = describeService(PUBLIC_SERVICE)
    expect(
      before.desiredCount,
      "the public service must be up",
    ).toBeGreaterThan(0)

    // Row 819: the published data has to be readable across the replacement,
    // so it is read before the deployment as well - an after-only 200 could
    // just mean the record was never there to lose.
    const record = await publishedRecord()
    test.skip(
      !record,
      "row 819 needs a published record on this environment; none is published",
    )
    expect(
      await reproduceStatus(record!),
      `published ${record!.uid} did not load before the deployment`,
    ).toBe(200)
    p.step(`published ${record!.uid} readable before the deployment`)

    const anon = await request.newContext()
    const statuses: number[] = []
    try {
      // describe-services is eventually consistent, so without pinning the
      // deployment id the first probe can read the PREVIOUS deployment as a
      // completed PRIMARY and settle before ECS has placed anything.
      const priorDeployment = describeService(PUBLIC_SERVICE).deployments.find(
        (d) => d.status === "PRIMARY",
      )?.id
      const deployStart = Date.now()
      awsJson(
        `ecs update-service --cluster ${CLUSTER} --service ${PUBLIC_SERVICE} ` +
          `--force-new-deployment --region ${AWS_REGION}`,
      )
      p.step(
        `forced a new deployment over ${priorDeployment}; each replaced task ` +
          `can drain for the full 600s deregistration delay`,
      )
      // Poll the front door while the deployment rolls; a rolling update keeps
      // the old task in service until the new one is healthy, so every one of
      // these must answer
      const deadline = Date.now() + 2_700_000
      let settled = false
      while (Date.now() < deadline) {
        const res = await anon.get(`${process.env.BASE_URL}/`, {
          failOnStatusCode: false,
        })
        statuses.push(res.status())
        // runningCount already equals desiredCount before ECS starts placing
        // the new task, so the rollout's own state is what says it finished.
        const s = describeService(PUBLIC_SERVICE)
        const primary = s.deployments.find((d) => d.status === "PRIMARY")
        expect(
          primary,
          "the service reported no PRIMARY deployment",
        ).toBeTruthy()
        expect(
          primary!.rolloutState,
          "the public deployment failed to roll out",
        ).not.toBe("FAILED")
        p.tick(
          `rollout ${primary!.rolloutState}, running ${s.runningCount}/` +
            `${before.desiredCount}, ${statuses.length} probes`,
        )
        // Not `deployments.length === 1`: a second force-new-deployment landing
        // mid-run keeps a superseded deployment listed and makes that count
        // unsatisfiable for the rest of the run. COMPLETED on a PRIMARY newer
        // than the pinned one already means the roll finished.
        if (
          primary!.id !== priorDeployment &&
          primary!.rolloutState === "COMPLETED" &&
          s.runningCount === before.desiredCount
        ) {
          settled = true
          break
        }
        await new Promise((r) => setTimeout(r, 10_000))
      }
      expect(settled, "the public deployment never settled").toBe(true)
      p.step(`rollout settled after ${statuses.length} probes`)

      // The new task mounted the same EFS filesystem, so the record is still
      // there: 202 here would mean the replacement lost the cache and the
      // route fell back to S3.
      expect(
        await reproduceStatus(record!),
        `published ${record!.uid} after the task replacement (202 = re-syncing ` +
          `from S3, so EFS did not preserve it)`,
      ).toBe(200)
      p.step("EFS preserved the published cache across the replacement")

      // Row 823: the replacement task really runs the startup sync through
      // the leader path, warming the published-data cache on the new task.
      //
      // Deliberately NOT asserted: the row's "other tasks log 'deferred to
      // leader'" line. `startup_sync_leader_lock` holds the MySQL lock only
      // while the sync runs and releases it on exit, and a rolling deployment
      // replaces tasks one at a time - so the second task boots long after
      // the lock is free and legitimately logs "scheduled" too. Contention
      // needs two tasks booting at once, which this action cannot produce -
      // a rolling deployment emits "scheduled" with no "deferred" line. The
      // loser branch is
      // test_main_unit_startup.py::TestStartupSyncLeaderElection.
      await expect
        .poll(
          () => {
            p.tick("waiting for the replacement task's startup-sync log line")
            return cloudwatchHas(
              PUBLIC_LOG_GROUP,
              "Startup sync task scheduled",
              deployStart,
            )
          },
          {
            ...CLOUDWATCH_POLL,
            message: `no "Startup sync task scheduled" line in ${PUBLIC_LOG_GROUP} after the deployment`,
          },
        )
        .toBe(true)
    } finally {
      await anon.dispose()
    }

    // A handful of probes would satisfy the filter below without the
    // deployment having rolled under any of them.
    expect(
      statuses.length,
      "too few probes to claim the tier kept serving throughout",
    ).toBeGreaterThan(5)
    expect(
      statuses.filter((s) => s !== 200),
      `non-200 responses during the rolling deployment (${statuses.length} probes)`,
    ).toEqual([])
  })

  // Row 811: premium instances are independent of free-tier health, which is
  // the original point of issue #574. The assignment is taken BEFORE the outage
  // - the row's own step 1 - so a cold premium pool cannot turn this into a
  // placement test. The workflow-on-a-premium-instance half is PREM-05's job on
  // a healthy tier; what this adds is that none of it needs the free tier.
  test("OUT-03 - A premium user keeps their instance while the free service is at zero", async () => {
    // Above the sum of the ceilings below: 300s assign, 480s settle, 600s
    // scale-down, 300s target health, 600s restore, 300s release.
    test.setTimeout(3_000_000)
    skipIfTooCloseToScheduledStop(50)
    test.skip(
      !PREMIUM_USER.email || !PREMIUM_USER.password,
      "row 811 needs TEST_PREMIUM_EMAIL / TEST_PREMIUM_PASSWORD",
    )

    type Assignment = { instance_id?: string; is_shared?: boolean }
    const setup = await apiLogin(PREMIUM_USER.email, PREMIUM_USER.password)
    let userId = 0
    let assigned: Assignment | undefined
    const statusVia = async (
      ctx: typeof setup,
    ): Promise<Assignment | undefined> => {
      const res = await ctx.api.get("/users/me/premium/status", {
        headers: ctx.headers,
        timeout: 60_000,
      })
      expect(res.ok(), await res.text()).toBe(true)
      return (await res.json()).assignment ?? undefined
    }

    try {
      userId = (
        await (
          await setup.api.get("/users/me", { headers: setup.headers })
        ).json()
      ).id
      const assign = await setup.api.post("/users/me/premium/assign", {
        headers: setup.headers,
        timeout: 300_000,
      })
      expect(assign.ok(), await assign.text()).toBe(true)
      // The cascade can answer before the instance is real (autoscaling-pool,
      // scaling_in_progress), so the settled assignment is read from status.
      const deadline = Date.now() + 480_000
      for (;;) {
        const current = await statusVia(setup)
        if (current?.instance_id?.startsWith("i-")) {
          assigned = current
          break
        }
        if (Date.now() > deadline) break
        await new Promise((r) => setTimeout(r, 15_000))
      }
      // A pool that cannot place is not this row's failure.
      test.skip(
        !assigned,
        `row 811: no premium instance was placed for user ${userId} in 8 minutes`,
      )

      const before = describeService(FREE_SERVICE)
      expect(
        before.desiredCount,
        "the free service must be up before this test takes it down",
      ).toBeGreaterThan(0)
      const anon = await request.newContext()
      try {
        scaleService(FREE_SERVICE, 0)
        await pollService(FREE_SERVICE, (s) => s.runningCount === 0, "0 tasks")

        // The shell, from the public tier, with the free tier gone
        expect(
          (await anon.get(`${process.env.BASE_URL}/`)).status(),
          "the shell during the free outage",
        ).toBe(200)

        // A brand new login, not the token minted before the outage: the row's
        // step 4 logs back in while the free service is down.
        const outage = await apiLogin(PREMIUM_USER.email, PREMIUM_USER.password)
        try {
          const during = await statusVia(outage)
          expect(
            during?.instance_id,
            "the premium assignment did not survive the free-tier outage",
          ).toBe(assigned!.instance_id)

          // The ALB's own answer to "can traffic reach that instance": the
          // per-user target group only exists for a dedicated grant, so a
          // shared one is asserted through status alone.
          if (!assigned!.is_shared) {
            await expect
              .poll(() => premiumTargetHealth(userId), {
                timeout: 300_000,
                intervals: [15_000],
                message: `premium-${userId}-tg reported no healthy target during the outage`,
              })
              .toContain("healthy")
          }
        } finally {
          await outage.api.dispose()
        }
      } finally {
        scaleService(FREE_SERVICE, before.desiredCount)
        await pollService(
          FREE_SERVICE,
          (s) => s.runningCount === before.desiredCount,
          `back to ${before.desiredCount} task(s)`,
        )
        await anon.dispose()
      }
    } finally {
      // Never leave the shared premium account holding an instance.
      if (assigned) {
        const released = await setup.api.delete("/users/me/premium/assign", {
          headers: setup.headers,
          timeout: 300_000,
        })
        expect(released.ok(), await released.text()).toBe(true)
      }
      await setup.api.dispose()
    }
  })
})

// ---------------------------------------------------------------------------
// Row 827: the public ASG replaces an instance that goes away. Its own describe
// block because the tests above are about the free tier, and this one never
// touches it.
// ---------------------------------------------------------------------------

const PUBLIC_ASG = "development-optinist-public-asg"
const PUBLIC_TG = "development-optinist-public-tg"
const PUBLIC_LB = "development-optinist-lb"
const UNHEALTHY_ALARM = "development-optinist-public-tg-unhealthy-hosts"
// Explicit rather than Playwright's implicit default: the probe cadence and the
// detection-window arithmetic both depend on a request that cannot hang.
const PROBE_TIMEOUT_MS = 30_000

type AsgInstance = { id: string; health: string; state: string }

function describeAsg(): {
  min: number
  desired: number
  instances: AsgInstance[]
} {
  const out = awsJson<{
    AutoScalingGroups: {
      MinSize: number
      DesiredCapacity: number
      Instances: {
        InstanceId: string
        HealthStatus: string
        LifecycleState: string
      }[]
    }[]
  }>(
    `autoscaling describe-auto-scaling-groups ` +
      `--auto-scaling-group-names ${PUBLIC_ASG} --region ${AWS_REGION}`,
  )
  const asg = out.AutoScalingGroups[0]
  expect(asg, `autoscaling knows ${PUBLIC_ASG}`).toBeTruthy()
  return {
    min: asg.MinSize,
    desired: asg.DesiredCapacity,
    instances: asg.Instances.map((i) => ({
      id: i.InstanceId,
      health: i.HealthStatus,
      state: i.LifecycleState,
    })),
  }
}

function inService(asg: { instances: AsgInstance[] }): string[] {
  return asg.instances
    .filter((i) => i.state === "InService" && i.health === "Healthy")
    .map((i) => i.id)
}

function publicTgArn(): string {
  return awsJson<{ TargetGroups: { TargetGroupArn: string }[] }>(
    `elbv2 describe-target-groups --names ${PUBLIC_TG} --region ${AWS_REGION}`,
  ).TargetGroups[0].TargetGroupArn
}

function publicTgHealth(): { id: string; port: number; state: string }[] {
  return awsJson<
    { Target: { Id: string; Port: number }; TargetHealth: { State: string } }[]
  >(
    `elbv2 describe-target-health --target-group-arn ${publicTgArn()} ` +
      `--region ${AWS_REGION} --query 'TargetHealthDescriptions'`,
  ).map((t) => ({
    id: t.Target.Id,
    port: t.Target.Port,
    state: t.TargetHealth.State,
  }))
}

// How long the ALB can still route to a target whose OS is already gone:
// (unhealthy_threshold + 2) x interval. Two intervals of slack, not one:
//   +1 -> the kill landing just after a passing check
//   +1 -> the terminate call and the OS shutdown, before checks start failing
// Read from the TG so a terraform change moves it.
function publicTgDetectionMs(): number {
  const tgs = awsJson<{
    TargetGroups: {
      HealthCheckIntervalSeconds: number
      UnhealthyThresholdCount: number
    }[]
  }>(
    `elbv2 describe-target-groups --names ${PUBLIC_TG} --region ${AWS_REGION}`,
  ).TargetGroups
  expect(tgs.length, `elbv2 knows ${PUBLIC_TG}`).toBe(1)
  return (
    (tgs[0].UnhealthyThresholdCount + 2) *
    tgs[0].HealthCheckIntervalSeconds *
    1000
  )
}

// The EC2 instances currently running a task of the public service. Row 827's
// third expectation is about the task, not just the instance.
function publicTaskInstances(): string[] {
  const arns = awsJson<{ taskArns: string[] }>(
    `ecs list-tasks --cluster ${CLUSTER} --service-name ${PUBLIC_SERVICE} ` +
      `--region ${AWS_REGION}`,
  ).taskArns
  if (!arns.length) return []
  const cis = awsJson<{ tasks: { containerInstanceArn?: string }[] }>(
    `ecs describe-tasks --cluster ${CLUSTER} --tasks ${arns.join(" ")} ` +
      `--region ${AWS_REGION}`,
  )
    .tasks.map((t) => t.containerInstanceArn)
    .filter((a): a is string => !!a)
  if (!cis.length) return []
  return awsJson<{ containerInstances: { ec2InstanceId: string }[] }>(
    `ecs describe-container-instances --cluster ${CLUSTER} ` +
      `--container-instances ${cis.join(" ")} --region ${AWS_REGION}`,
  ).containerInstances.map((c) => c.ec2InstanceId)
}

// Launch activities newer than a moment. More than one means the ASG is
// churning: the replacement never came up healthy and is being replaced in
// turn, which no assertion below would otherwise notice.
function launchesSince(sinceMs: number): { desc: string; cause: string }[] {
  return awsJson<{ StartTime: string; Description: string; Cause: string }[]>(
    `autoscaling describe-scaling-activities ` +
      `--auto-scaling-group-name ${PUBLIC_ASG} --max-items 20 ` +
      `--region ${AWS_REGION} --query 'Activities'`,
  )
    .filter(
      (a) =>
        Date.parse(a.StartTime) >= sinceMs &&
        a.Description.startsWith("Launching"),
    )
    .map((a) => ({ desc: a.Description, cause: a.Cause }))
}

function alarmState(): string[] {
  return awsJson<string[]>(
    `cloudwatch describe-alarms --alarm-names ${UNHEALTHY_ALARM} ` +
      `--region ${AWS_REGION} --query 'MetricAlarms[].StateValue'`,
  )
}

// [Timestamp, Maximum] pairs, time-sorted. Maxima alone cannot carry the claim
// they are logged for: get-metric-statistics returns Datapoints unordered, and
// CloudWatch omits periods with no samples - so [0,1,1,1,0] is indistinguishable
// from three non-adjacent 1s. The timestamps are what show contiguity.
function unhealthyHostSeries(sinceMs: number): [string, number][] {
  const tg = publicTgArn()
  const lb = awsJson<{ LoadBalancers: { LoadBalancerArn: string }[] }>(
    `elbv2 describe-load-balancers --names ${PUBLIC_LB} --region ${AWS_REGION}`,
  ).LoadBalancers[0].LoadBalancerArn
  return awsJson<[string, number][]>(
    `cloudwatch get-metric-statistics --namespace AWS/ApplicationELB ` +
      `--metric-name UnHealthyHostCount ` +
      `--dimensions Name=TargetGroup,Value=${tg.slice(
        tg.indexOf("targetgroup/"),
      )} Name=LoadBalancer,Value=${lb.slice(lb.indexOf("app/"))} ` +
      `--start-time ${new Date(sinceMs).toISOString()} ` +
      `--end-time ${new Date().toISOString()} ` +
      `--period 60 --statistics Maximum --region ${AWS_REGION} ` +
      // sort_by: get-metric-statistics returns Datapoints in no defined order,
      // so the unsorted list reads as flapping (`[0,1,0,1,0]`) where the metric
      // actually held one contiguous block. Math.max below does not care; the
      // row-824 log line does.
      `--query 'sort_by(Datapoints,&Timestamp)[].[Timestamp,Maximum]'`,
  )
}

test.describe("Disruptive: the public ASG replaces an instance @disruptive", () => {
  test.beforeEach(guardDisruptive)

  // Row 827, by its own Action: "Terminate one of the public EC2 instances
  // manually." Terminating is the stimulus rather than set-instance-health,
  // which would write the very health verdict the ASG is supposed to reach on
  // its own - the objection that retired the set-alarm-state version.
  //
  // Row 824's alarm is watched but deliberately NOT asserted here: a
  // terminating instance's target has been seen counted `unhealthy` (with the
  // alarm running ALARM -> OK) rather than going straight to `draining`, but
  // that is not contractual and is thin ground for a hard assertion. The
  // datapoints stay logged, and HEALTH-27 covers 824's "the alarm fires on
  // real datapoints" half read-only.
  test("ASG-01 - Terminating a public instance replaces it without dropping traffic", async () => {
    // 65 minutes: the 2400s settle loop plus the two 600s polls after it sum to
    // exactly 3600s, leaving nothing for the AWS calls between them. The real
    // cost is far lower - the terminate activity and the replacement launch
    // each take on the order of 10-20 minutes.
    test.setTimeout(3_900_000)
    skipIfTooCloseToScheduledStop(65)

    // Every capacity number here is rewritten twice a day by the scheduler, so
    // read them rather than trusting the terraform default.
    const before = describeAsg()
    test.skip(
      before.min < 2 || before.desired < 2,
      `row 827 needs the public ASG at min>=2 and desired>=2 so the surviving ` +
        `instance can serve; it is min=${before.min} desired=${before.desired}`,
    )
    const healthyBefore = inService(before)
    expect(
      healthyBefore.length,
      "both public instances must be InService and Healthy before one is killed",
    ).toBe(2)
    expect(
      publicTgHealth()
        .filter((t) => t.state === "healthy")
        .map((t) => t.id)
        .sort(),
      "the public target group must hold exactly the two healthy ASG instances",
    ).toEqual([...healthyBefore].sort())
    // A run that starts with the metric already breaching cannot attribute
    // anything below to what it did.
    expect(
      Math.max(
        0,
        ...unhealthyHostSeries(Date.now() - 300_000).map(([, m]) => m),
      ),
      "UnHealthyHostCount was already above zero before this test started",
    ).toBe(0)
    expect(alarmState(), `${UNHEALTHY_ALARM} must start in OK`).toEqual(["OK"])
    // Read before the kill: fixed config, and one fewer AWS call on the
    // post-disruption path, where a throw costs the whole run's evidence.
    const detectionMs = publicTgDetectionMs()

    const start = Date.now()
    const [victim, survivor] = healthyBefore
    const anon = await request.newContext()
    const probes: { at: number; status: number }[] = []
    let replacement = ""
    // start is taken before the terminate call, which is what launchesSince and
    // the UnHealthyHostCount read need. The detection window cannot use it: the
    // CLI round trip runs inside it. killedAt is the window's zero point.
    let killedAt = 0
    try {
      awsJson(
        `ec2 terminate-instances --instance-ids ${victim} ` +
          `--region ${AWS_REGION}`,
      )
      killedAt = Date.now()

      // Probe the front door for the whole replacement, the same way OUT-02
      // proves a rolling deployment keeps serving. The surviving instance is
      // what has to carry it.
      const deadline = Date.now() + 2_400_000
      for (;;) {
        const at = Date.now()
        // A hard terminate mid-connection is the stimulus most likely to reset
        // one, and an uncaught throw here ends the run with no probe evidence
        // at all. status 0 = the request never completed.
        let status = 0
        try {
          const res = await anon.get(`${process.env.BASE_URL}/`, {
            failOnStatusCode: false,
            timeout: PROBE_TIMEOUT_MS,
          })
          status = res.status()
        } catch {
          // A reset or a timeout leaves status 0; the loop keeps probing.
        }
        probes.push({ at, status })

        const churn = launchesSince(start)
        expect(
          churn.length,
          `the ASG launched ${churn.length} instances after one termination, ` +
            `so a replacement is failing its health check and being replaced ` +
            `in turn. Causes: ${churn
              .map((c) => c.cause.split(".")[0])
              .join(" | ")}. The tier is degraded until this settles: check ` +
            `the public launch template and the startup sync, then confirm the ` +
            `ASG returns to ${before.desired} InService instances`,
        ).toBeLessThan(2)

        const current = describeAsg()
        const live = inService(current)
        const fresh = live.filter((id) => !healthyBefore.includes(id))
        if (
          !current.instances.some((i) => i.id === victim) &&
          fresh.length === 1 &&
          live.includes(survivor) &&
          live.length === 2
        ) {
          replacement = fresh[0]
          break
        }
        expect(
          Date.now(),
          `the ASG never settled: ${JSON.stringify(current.instances)}`,
        ).toBeLessThan(deadline)
        await new Promise((r) => setTimeout(r, 15_000))
      }
    } finally {
      await anon.dispose()
    }

    // A hard terminate kills the OS before the ALB can react, and ALB does not
    // retry a failed target connection, so a bounded blip is inherent to the
    // stimulus: the ALB emits the 502 itself (TargetConnectionError, not an
    // application 5xx). What row 827 claims, scoped:
    //   inside the detection window -> 502/504 only
    //   outside it                  -> every probe 200; the survivor carries
    //                                  the tier
    //   either side                 -> at most MAX_BLIP_PROBES non-200s
    // Never tolerated: 503 (no healthy target at all - traffic dropped, not one
    // connection lost) and 0 (the request never completed, which a dead target
    // connection does not cause). The volume bound is load-bearing: class and
    // time alone let EVERY in-window probe be 502, a two-minute outage under a
    // row named "without dropping traffic".
    //
    // Soft, all five: a hard failure here skips the target-group poll, the
    // ECS-placement poll, the row-824 evidence line and the final settle check -
    // the verification this run exists to produce.
    const MAX_BLIP_PROBES = 2
    const failures = probes.filter((p) => p.status !== 200)
    const withOffset = (ps: typeof probes) =>
      ps.map((p) => ({
        afterS: Math.round((p.at - killedAt) / 1000),
        status: p.status,
      }))
    expect
      .soft(
        probes.length,
        "too few probes to claim the tier kept serving throughout",
      )
      .toBeGreaterThan(5)
    expect
      .soft(
        withOffset(
          failures.filter((p) => p.status !== 502 && p.status !== 504),
        ),
        `non-200s the termination cannot explain: only 502/504 on the victim's ` +
          `own connections are inherent to a hard terminate. 503 would mean no ` +
          `healthy target at all, and 0 that the request never completed ` +
          `(${probes.length} probes)`,
      )
      .toEqual([])
    expect
      .soft(
        failures.length,
        `too many non-200s to be the inherent connection blip: ` +
          `${JSON.stringify(withOffset(failures))} of ${probes.length} probes. ` +
          `A hard terminate costs the requests in flight to the dead target, so ` +
          `a run of them is the tier failing to carry the load on one instance`,
      )
      .toBeLessThanOrEqual(MAX_BLIP_PROBES)
    const late = failures.filter((p) => p.at - killedAt > detectionMs)
    expect
      .soft(
        withOffset(late),
        `non-200 responses more than ${detectionMs / 1000}s after the ` +
          `termination, long after the ALB had time to drop the dead target ` +
          `(${probes.length} probes)`,
      )
      .toEqual([])
    // Without this the check above passes vacuously on a run whose probes all
    // landed inside the window.
    expect
      .soft(
        probes.filter((p) => p.at - killedAt > detectionMs).length,
        "too few probes after the detection window to claim the tier recovered",
      )
      .toBeGreaterThan(5)

    // In the target group, not merely in the ASG
    await expect
      .poll(
        () =>
          publicTgHealth().some(
            (t) => t.id === replacement && t.state === "healthy",
          ),
        {
          timeout: 600_000,
          intervals: [15_000],
          message: `${replacement} never became a healthy target in ${PUBLIC_TG}`,
        },
      )
      .toBe(true)

    // Row 827's third expectation: a new ECS task really placed on it, which is
    // a separate claim from the instance coming back.
    await expect
      .poll(() => publicTaskInstances(), {
        timeout: 600_000,
        intervals: [15_000],
        message: `no public ECS task was placed on ${replacement}`,
      })
      .toContain(replacement)

    // Evidence for row 824 rather than an assertion on it: whether a
    // terminating instance's target is ever counted unhealthy is what this
    // records, and the first runs are what settle it.
    console.log(
      `ASG-01: UnHealthyHostCount during the replacement: ` +
        `${unhealthyHostSeries(start)
          .map(([t, m]) => `${t.slice(11, 16)}=${m}`)
          .join(" ")}; ` +
        `${UNHEALTHY_ALARM} is now ${alarmState().join(",")}; ` +
        `non-200 probes: ${JSON.stringify(withOffset(failures))}`,
    )

    // No restore step: nothing here changed a capacity number, and MinSize kept
    // the ASG at two. What is asserted is that it got back there.
    const after = describeAsg()
    expect(
      inService(after).sort(),
      "the ASG settled at two healthy instances",
    ).toEqual([replacement, survivor].sort())
    expect(
      after.instances.filter((i) => i.state !== "InService"),
      "an instance is still mid-lifecycle",
    ).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// Rows 6214 / 6215: the assigned premium instance itself goes away - stopped
// (OUT-04) or terminated (OUT-05) out from under the user.
// PremiumRetriggerAssign.test.tsx pins the frontend half against mocks; these
// destroy the real instance. They are the fixes-state-of-instances class
// (the ASG-01 family): user-fired only, never in the scheduled lanes, and
// each run consumes real capacity that the finally block waits to see
// replaced rather than leaving the cost to the next tester's session.
// ---------------------------------------------------------------------------

const PREMIUM_CLEANUP_LOG_GROUP = "/aws/lambda/development-premium-cleanup"

test.describe("Disruptive: the assigned premium instance goes away @disruptive", () => {
  test.beforeEach(guardDisruptive)

  const userRowInstance = (userId: number) =>
    runSql(
      `SELECT instance_id FROM premium_user_assignments
         WHERE user_id = ${userId};`,
    )
  const standbyRows = () =>
    runSql(
      `SELECT COUNT(*) FROM premium_user_assignments
         WHERE is_standby = 1 AND user_id IS NULL;`,
    )

  // Real page login and a settled dedicated assignment, with recovery
  // capacity staged so the reassignment after the outage is placement, not
  // luck. Dedicated only: destroying a shared instance takes its other
  // tenants down with it, which is nobody's row.
  async function stageAssignedPremium(
    page: Page,
  ): Promise<{ userId: number; instanceA: string; candidate: string }> {
    await login(page, PREMIUM_USER.email, PREMIUM_USER.password)
    let instanceA = ""
    await expect
      .poll(
        async () => {
          const res = await page.request.get(
            `${apiUrl()}/users/me/premium/status`,
            { headers: await apiHeaders(page), timeout: 60_000 },
          )
          expect(res.ok(), await res.text()).toBe(true)
          instanceA = (await res.json()).assignment?.instance_id ?? ""
          return instanceA
        },
        {
          timeout: 480_000,
          intervals: [15_000],
          message: "no real premium instance was placed within 8 minutes",
        },
      )
      .toMatch(/^i-[0-9a-f]+$/)
    const me = await page.request.get(`${apiUrl()}/users/me`, {
      headers: await apiHeaders(page),
      timeout: 60_000,
    })
    expect(me.ok(), await me.text()).toBe(true)
    const userId: number = (await me.json()).id
    test.skip(
      runSql(
        `SELECT is_shared FROM premium_user_assignments
           WHERE user_id = ${userId};`,
      ) !== "0",
      "the cascade granted a shared instance; destroying it would take its other tenants down",
    )
    const candidate = await stageSecondRunningInstance(instanceA)
    return { userId, instanceA, candidate }
  }

  // Release, then wait for the pool to really self-heal before ending the
  // run: these tests consume the capacity they destroy, and the PREM-06
  // pre-stage recipe in the README assumes a standby exists.
  async function releaseAndAwaitPoolHeal(page: Page) {
    await page.request
      .delete(`${apiUrl()}/users/me/premium/assign`, {
        headers: await apiHeaders(page),
        timeout: 300_000,
      })
      .catch(() => {})
    invokeMonitoringSweep()
    let count = ""
    await expect
      .poll(() => (count = standbyRows()), {
        timeout: 12 * 60_000,
        intervals: [30_000],
        message:
          "the pool never converged back to a standby after the outage - " +
          "the next tester inherits a cold, rowless pool",
      })
      .not.toBe("0")
    console.log(`[22-disruptive] standby rows after the pool heal: ${count}`)
  }

  // Row 6214: stopping the instance under the user. The app must notice
  // (DEGRADED snackbar), the backend must notice (the row leaves the stopped
  // instance - status liveness or the EventBridge reconciliation, whichever
  // wins), and the instance-lost re-trigger must land a working assignment
  // with no manual re-login.
  test("OUT-04 - Stopping the assigned instance degrades, then recovery lands a working assignment", async ({
    page,
  }) => {
    test.setTimeout(2_400_000)
    skipIfTooCloseToScheduledStop(40)
    test.skip(
      !PREMIUM_USER.email || !PREMIUM_USER.password,
      "row 6214 needs TEST_PREMIUM_EMAIL / TEST_PREMIUM_PASSWORD",
    )
    const sqlReason = sqlSkipReason()
    test.skip(!!sqlReason, `row 6214: ${sqlReason}`)

    const { userId, instanceA } = await stageAssignedPremium(page)
    // The Record tab's Reload button drives fresh premium-routed requests
    // (PREM-13's trigger mechanics); idle pages fire none.
    const wsId = await openWorkspace(page, "e2e-out-stop")
    await page.locator('button[role="tab"]:has-text("Record")').click()
    const reload = page.getByRole("button", { name: "Reload" })
    await expect(reload).toBeVisible({ timeout: 30_000 })
    const snackbar = page.getByText(
      /dedicated premium instance is (temporarily unreachable|unresponsive)/,
    )
    // The named recovery mechanism, off the app's own console line: only the
    // instance-lost re-trigger calls assign once the row is gone, so recovery
    // without this line would mean some other path did the work.
    let retriggerArmed = false
    page.on("console", (msg) => {
      if (msg.text().includes("Re-triggering assign")) retriggerArmed = true
    })

    try {
      awsJson(`ec2 stop-instances --instance-ids ${instanceA}`)

      // The app notices: DEGRADED within the click-loop window. Mutation
      // check (named): without the stop, PREM-03/PREM-13 prove this same
      // snackbar never renders against a healthy dedicated instance.
      await expect
        .poll(
          async () => {
            if (await snackbar.isVisible().catch(() => false)) return true
            await reload.click().catch(() => {})
            await page.waitForTimeout(2_000)
            return snackbar.isVisible().catch(() => false)
          },
          {
            timeout: 240_000,
            intervals: [1_000],
            message:
              "the unreachable snackbar never appeared after the instance was stopped",
          },
        )
        .toBe(true)

      // The backend notices: the row leaves the stopped instance.
      await expect
        .poll(() => userRowInstance(userId), {
          timeout: 8 * 60_000,
          intervals: [15_000],
          message: `the assignment row never left stopped ${instanceA}`,
        })
        .not.toBe(instanceA)

      // Recovery: the re-trigger reassigns onto whatever the cascade can
      // really place - new instance or the restarted one - asserted on
      // viability, not identity.
      let recovered = ""
      await expect
        .poll(
          async () => {
            await reload.click().catch(() => {})
            await page.waitForTimeout(2_000)
            const res = await page.request.get(
              `${apiUrl()}/users/me/premium/status`,
              { headers: await apiHeaders(page), timeout: 60_000 },
            )
            if (!res.ok()) return ""
            recovered = (await res.json()).assignment?.instance_id ?? ""
            return /^i-[0-9a-f]+$/.test(recovered) ? recovered : ""
          },
          {
            timeout: 12 * 60_000,
            intervals: [10_000],
            message:
              "no working assignment ever came back after the stop - the " +
              "instance-lost recovery is broken end to end",
          },
        )
        .toMatch(/^i-[0-9a-f]+$/)
      expect(
        retriggerArmed,
        "recovery happened but the instance-lost re-trigger never armed - something else assigned",
      ).toBe(true)

      // Row and ALB consistent with what the client now holds.
      expect(userRowInstance(userId)).toBe(recovered)
      await expect
        .poll(() => premiumTargetHealth(userId).includes("healthy"), {
          timeout: 5 * 60_000,
          intervals: [15_000],
          message: `premium-${userId}-tg never went healthy on ${recovered}`,
        })
        .toBe(true)
      await expect(snackbar).toBeHidden({ timeout: 120_000 })
      console.log(
        `[22-disruptive] OUT-04: recovered from stopped ${instanceA} onto ${recovered}`,
      )
    } finally {
      await releaseAndAwaitPoolHeal(page)
      await page.request
        .delete(`${apiUrl()}/workspace/${wsId}`, {
          headers: await apiHeaders(page),
          timeout: 60_000,
        })
        .catch(() => {})
    }
  })

  // Row 6215: terminating the instance under the user. The sheet's own
  // Expected names the mechanism: the ec2-state-change EventBridge rule
  // delivers the targeted reconciliation to the Cleanup Lambda (PREM-10's
  // assert, here for an OWNED instance racing the client's recovery).
  test("OUT-05 - Terminating the assigned instance: EventBridge reconciles, the client reassigns", async ({
    page,
  }) => {
    test.setTimeout(2_700_000)
    skipIfTooCloseToScheduledStop(45)
    test.skip(
      !PREMIUM_USER.email || !PREMIUM_USER.password,
      "row 6215 needs TEST_PREMIUM_EMAIL / TEST_PREMIUM_PASSWORD",
    )
    const sqlReason = sqlSkipReason()
    test.skip(!!sqlReason, `row 6215: ${sqlReason}`)

    const { userId, instanceA, candidate } = await stageAssignedPremium(page)
    // 1-then-not (the PREM-10 rule): the row must point at the doomed
    // instance before the termination, or its move proves nothing.
    expect(userRowInstance(userId), "pre-termination row").toBe(instanceA)

    const t0 = windowStart()
    try {
      awsJson(`ec2 terminate-instances --instance-ids ${instanceA}`)

      // The event-driven path, not the hourly walk: this line only comes
      // from the reconcile_instance action the EventBridge rule delivers.
      await expect
        .poll(
          () =>
            cloudwatchHas(
              PREMIUM_CLEANUP_LOG_GROUP,
              `Targeted instance reconciliation for ${instanceA}`,
              t0,
            ),
          {
            timeout: 10 * 60_000,
            intervals: [15_000],
            message:
              `no targeted reconciliation for ${instanceA} in ` +
              `${PREMIUM_CLEANUP_LOG_GROUP} after terminating it`,
          },
        )
        .toBe(true)
      await expect
        .poll(() => userRowInstance(userId), {
          timeout: 8 * 60_000,
          intervals: [15_000],
          message: `the assignment row never left terminated ${instanceA}`,
        })
        .not.toBe(instanceA)

      // The client's recovery reassigns onto real capacity - the staged
      // candidate is what makes this placement rather than luck, but the
      // assert is on viability, not on which instance won.
      let recovered = ""
      await expect
        .poll(
          async () => {
            const res = await page.request.get(
              `${apiUrl()}/users/me/premium/status`,
              { headers: await apiHeaders(page), timeout: 60_000 },
            )
            if (!res.ok()) return ""
            recovered = (await res.json()).assignment?.instance_id ?? ""
            return /^i-[0-9a-f]+$/.test(recovered) ? recovered : ""
          },
          {
            timeout: 12 * 60_000,
            intervals: [10_000],
            message:
              "no working assignment ever came back after the termination",
          },
        )
        .toMatch(/^i-[0-9a-f]+$/)
      expect(
        recovered,
        "the recovery reassigned onto the terminated instance",
      ).not.toBe(instanceA)
      expect(userRowInstance(userId)).toBe(recovered)
      await expect
        .poll(() => premiumTargetHealth(userId).includes("healthy"), {
          timeout: 5 * 60_000,
          intervals: [15_000],
          message: `premium-${userId}-tg never went healthy on ${recovered}`,
        })
        .toBe(true)
      console.log(
        `[22-disruptive] OUT-05: recovered from terminated ${instanceA} onto ` +
          `${recovered} (staged candidate was ${candidate})`,
      )
    } finally {
      // A real instance was destroyed: the heal wait here is the replacement
      // budget, spent in this run instead of the next tester's session.
      await releaseAndAwaitPoolHeal(page)
    }
  })
})
