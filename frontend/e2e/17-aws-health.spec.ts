import { execSync } from "child_process"

import { test, expect } from "@playwright/test"

import {
  AWS_REGION,
  awsJson,
  BACKGROUND_LOG_GROUP as BACKGROUND_LOG,
  cloudwatchHas,
  FREE_LOG_GROUP as FREE_LOG,
  FREE_USER,
  isLocalBaseUrl,
  logTail,
  PREMIUM_LOG_GROUP as PREMIUM_LOG,
  PUBLIC_LOG_GROUP as PUBLIC_LOG,
  RDS_PROXY_HOST,
  runSql,
  skipWithoutCreds,
  sqlLiteral,
  sqlSkipReason,
  TARGET_ENV,
} from "./helpers"

// Read-only truth for the AWS Monitoring sheets (BT-1101..1108, BT-1111,
// System 12, and the System 08 infrastructure rows) plus the SQL integrity
// audit rows of System 20. Every call here reads: no ECS scaling, no alarm
// state pushed, no row written. That is why the lane needs no opt-in flag -
// it costs nothing, mutates nothing, and runs in well under a minute.
//
//   BASE_URL=<dev origin>  npx playwright test e2e/17-aws-health.spec.ts
//   HEALTH_ENV=subscr BASE_URL=https://www.araya-optinist.com \
//     RDS_PROXY_HOST=... RDS_SECRET_ID=... RDS_SSM_INSTANCE_NAME=... \
//     STRIPE_SECRET_ENV=subscr-optinist \
//     npx playwright test e2e/17-aws-health.spec.ts
//
// The RDS and Stripe selectors are mandatory off development: their defaults
// point at development, and the lane refuses to run rather than report
// development's data as another environment's.
//
// It needs AWS credentials for the account hosting the environment, so it
// skips on a local BASE_URL (CI only ever runs local) and fails loudly rather
// than skipping when the credentials are missing on a deployed run. HEALTH-20..24
// additionally read the RDS over SSM, which is configured for development only;
// point RDS_PROXY_HOST / RDS_SECRET_ID / RDS_SSM_INSTANCE_NAME at another
// environment to run them there.

// Which deployed environment to read. `subscr` is production; the lane writes
// nothing, so pointing it there is the intended way to health-check a release.
const ENV = TARGET_ENV
const CLUSTER = `${ENV}-optinist-cloud-cluster`
const SERVICES = [
  `${ENV}-optinist-cloud-service`,
  `${ENV}-premium-optinist-cloud-service`,
  `${ENV}-public-optinist-cloud-service`,
  `${ENV}-background-optinist-cloud-service`,
]
const FREE_TG = `${ENV}-optinist-tg`
const PUBLIC_TG = `${ENV}-optinist-public-tg`
const RDS_INSTANCE = `${ENV}-optinist-cloud-rds`
const METRIC_NAMESPACE = `OptiNiSt/BackgroundJobs/${ENV}`
const BUCKET_PREFIX = `${ENV}-optinist-user-`

// Terraform declares these for every environment. Per-user premium target-group
// alarms are deliberately absent: they come and go with the assignment pool.
const EXPECTED_ALARMS = [
  `${ENV}-background-cpu-high`,
  `${ENV}-background-memory-high`,
  `${ENV}-background-task-stopped`,
  `${ENV}-optinist-alb-5xx-errors`,
  `${ENV}-optinist-cpu-high`,
  `${ENV}-optinist-cpu-low`,
  `${ENV}-optinist-ebs-queue-length-high`,
  `${ENV}-optinist-efs-burst-credits-low`,
  `${ENV}-optinist-efs-throughput-high`,
  `${ENV}-optinist-free-tg-response-time-high`,
  `${ENV}-optinist-free-tg-unhealthy-hosts`,
  `${ENV}-optinist-high-iowait`,
  `${ENV}-optinist-load-average-high`,
  `${ENV}-optinist-memory-high`,
  `${ENV}-optinist-memory-low`,
  `${ENV}-optinist-public-tg-response-time-high`,
  `${ENV}-optinist-public-tg-unhealthy-hosts`,
  `${ENV}-optinist-rds-connections-high`,
  `${ENV}-optinist-rds-cpu-high`,
  `${ENV}-optinist-rds-storage-low`,
  `${ENV}-premium-cpu-high`,
  `${ENV}-premium-memory-high`,
  `${ENV}-public-cleanup-errors`,
]

// Budget, not infrastructure health: it fires on monthly spend and drives
// nothing, so it says nothing about whether the environment is serving.
const COST_ALARM = `${ENV}-monthly-cost-high`

// An idle environment holds its scale-in trigger in ALARM by design: that is
// what tells the ASG to give an instance back. Recognise it by what it does
// rather than by its name, so a new scale-in alarm needs no edit here - and so
// a "-low" alarm that grew a real action stops being waved through.
function isScaleInTrigger(alarm: { name: string; actions: string[] }): boolean {
  return (
    alarm.actions.length > 0 &&
    alarm.actions.every((a) =>
      /^arn:aws:autoscaling:.*:scalingPolicy:/.test(a),
    ) &&
    /-low$/.test(alarm.name)
  )
}

// An alarm's own metric, over the window it evaluates. Only ever called for an
// alarm already in ALARM, so the happy path pays nothing.
type AlarmDetail = {
  name: string
  state: string
  actions: string[]
  namespace: string
  metric: string
  dimensions: { Name: string; Value: string }[]
  period: number
  statistic?: string
  extendedStatistic?: string
  threshold: number
  operator: string
  evaluationPeriods: number
}

function breaches(value: number, threshold: number, operator: string): boolean {
  switch (operator) {
    case "GreaterThanThreshold":
      return value > threshold
    case "GreaterThanOrEqualToThreshold":
      return value >= threshold
    case "LessThanThreshold":
      return value < threshold
    case "LessThanOrEqualToThreshold":
      return value <= threshold
    default:
      // An operator we do not model must not be silently waved through.
      return true
  }
}

// Is the alarm's metric still breaching right now? An ALARM state lags its
// metric by evaluation_periods, and treat_missing_data=missing holds the old
// state indefinitely once the metric stops publishing, so "the state says
// ALARM" and "the environment is unhealthy" are different questions. A metric
// that has gone quiet answers "unknown", which counts as still breaching:
// silence is not evidence of health.
//
// get-metric-data, not get-metric-statistics: it takes a percentile and a plain
// statistic as the same `Stat` string and answers with one flat Values array
// either way, where get-metric-statistics puts a plain statistic at the
// datapoint's top level and a percentile inside ExtendedStatistics.
function stillBreaching(alarm: AlarmDetail): boolean {
  const window = Math.max(alarm.period * alarm.evaluationPeriods, 300)
  const query = JSON.stringify([
    {
      Id: "m",
      MetricStat: {
        Metric: {
          Namespace: alarm.namespace,
          MetricName: alarm.metric,
          Dimensions: alarm.dimensions,
        },
        Period: alarm.period,
        Stat: alarm.extendedStatistic || alarm.statistic || "Maximum",
      },
    },
  ])
  // The query rides inside single quotes in the shell command below.
  expect(query, "alarm metadata carries a quote character").not.toContain("'")
  const values = awsJson<number[]>(
    `cloudwatch get-metric-data --metric-data-queries '${query}' ` +
      `--start-time ${new Date(Date.now() - window * 1000).toISOString()} ` +
      `--end-time ${new Date().toISOString()} ` +
      `--scan-by TimestampDescending ` +
      `--query 'MetricDataResults[0].Values'`,
  )
  if (!values.length) return true
  return breaches(values[0], alarm.threshold, alarm.operator)
}

// The dev scheduler starts this environment at 23:00 UTC Sun-Thu (08:00 JST) and
// the public tier's health-check grace period is 900s, so for the first half
// hour the tasks, targets, logs and alarms this lane reads are all mid-boot -
// the public target group really does hold two unhealthy targets and its alarm
// really is in ALARM. Grading that is grading the clock: the alarm sits in
// ALARM for the ~15-minute daily start-up window every weekday.
// A skip says so; a failure sends someone hunting an outage that ends itself.
function startUpWindowReason(): string {
  if (ENV !== "development") return ""
  const now = new Date()
  const day = now.getUTCDay()
  const minutes = now.getUTCHours() * 60 + now.getUTCMinutes()
  const start = 23 * 60
  if (day > 4 || minutes < start || minutes >= start + 30) return ""
  return (
    `the ${ENV} environment starts on its schedule at 23:00 UTC and is ` +
    `${minutes - start} minutes into that start-up: its targets, tasks and ` +
    `alarms are still settling. Rerun after 23:30 UTC (08:30 JST).`
  )
}

function skipUnlessDeployed(rows: string) {
  test.skip(
    isLocalBaseUrl(),
    `rows ${rows}: reads the deployed environment's AWS resources; BASE_URL is local`,
  )
  const booting = startUpWindowReason()
  test.skip(!!booting, `rows ${rows}: ${booting}`)
  // A BASE_URL from a different environment than HEALTH_ENV would read one
  // environment's AWS resources while calling another one's HTTP endpoints.
  expect(
    process.env.BASE_URL,
    `set BASE_URL to the ${ENV} environment's frontend origin`,
  ).toBeTruthy()
  // The RDS and Stripe selectors default to development independently of
  // HEALTH_ENV, so leaving them unset here would report development's data as
  // this environment's.
  if (ENV !== "development") {
    for (const name of [
      "RDS_PROXY_HOST",
      "RDS_SSM_INSTANCE_NAME",
      "RDS_SECRET_ID",
      "STRIPE_SECRET_ENV",
    ]) {
      expect(
        process.env[name],
        `HEALTH_ENV=${ENV} requires ${name}; its default points at development`,
      ).toBeTruthy()
    }
  }
}

function premiumDesiredCount(): number {
  return awsJson<number>(
    `ecs describe-services --cluster ${CLUSTER} --services ${SERVICES[1]} ` +
      `--query 'services[0].desiredCount'`,
  )
}

function runningTaskArns(): string[] {
  return awsJson<string[]>(
    `ecs list-tasks --cluster ${CLUSTER} --query 'taskArns[]'`,
  )
}

// Returns the sum and how many datapoints produced it: an absent metric sums
// to 0 exactly like a healthy idle one, so the count is what tells them apart.
function metricSum(
  metricName: string,
  hours: number,
): { sum: number; points: number } {
  const now = Math.floor(Date.now() / 1000)
  const points = awsJson<{ Sum: number }[]>(
    `cloudwatch get-metric-statistics --namespace ${METRIC_NAMESPACE} ` +
      `--metric-name ${metricName} --statistics Sum --period 3600 ` +
      `--start-time ${now - hours * 3600} --end-time ${now} ` +
      `--query 'Datapoints'`,
  )
  return {
    sum: points.reduce((total, p) => total + p.Sum, 0),
    points: points.length,
  }
}

function bucketExists(bucket: string): boolean {
  try {
    execSync(
      `aws s3api head-bucket --bucket ${bucket} --region ${AWS_REGION}`,
      { timeout: 30_000, stdio: ["pipe", "pipe", "pipe"] },
    )
    return true
  } catch {
    return false
  }
}

test.describe("Compute and routing", () => {
  test("HEALTH-01 - every tier's ECS service is ACTIVE and fully placed", () => {
    skipUnlessDeployed("BT-1102 / 1201")

    const services = awsJson<
      {
        name: string
        status: string
        desired: number
        running: number
        pending: number
      }[]
    >(
      `ecs describe-services --cluster ${CLUSTER} --services ${SERVICES.join(" ")} ` +
        `--query 'services[].{name:serviceName,status:status,desired:desiredCount,` +
        `running:runningCount,pending:pendingCount}'`,
    )

    expect(services.map((s) => s.name).sort()).toEqual([...SERVICES].sort())
    for (const svc of services) {
      expect(svc.status, `${svc.name} status`).toBe("ACTIVE")
      expect(svc.running, `${svc.name} running vs desired`).toBe(svc.desired)
      expect(svc.pending, `${svc.name} pending`).toBe(0)
    }
    // The premium service idles at zero, so running == desired is vacuous for
    // it; the tiers that must always serve traffic are asserted to be up.
    for (const name of [SERVICES[0], SERVICES[2], SERVICES[3]]) {
      expect(
        services.find((s) => s.name === name)!.running,
        `${name} must always have a task`,
      ).toBeGreaterThan(0)
    }
  })

  test("HEALTH-02 - every running task is healthy and none stopped on an error", () => {
    skipUnlessDeployed("BT-1103 / 1202")

    const arns = runningTaskArns()
    expect(arns.length, "cluster has no running tasks").toBeGreaterThan(0)
    const tasks = awsJson<
      { group: string; last: string; desired: string; health: string }[]
    >(
      `ecs describe-tasks --cluster ${CLUSTER} --tasks ${arns.join(" ")} ` +
        `--query 'tasks[].{group:group,last:lastStatus,desired:desiredStatus,` +
        `health:healthStatus}'`,
    )
    for (const task of tasks) {
      expect(task.last, `${task.group} lastStatus`).toBe("RUNNING")
      expect(task.desired, `${task.group} desiredStatus`).toBe("RUNNING")
      expect(task.health, `${task.group} healthStatus`).toBe("HEALTHY")
    }

    // Best-effort by nature: ECS drops stopped tasks from list-tasks about an
    // hour after they stop, so on a quiet cluster this list is empty and the
    // block below does not run. Absence here is not evidence of no crashes -
    // HEALTH-12's fatal-marker scan of the logs is what covers that.
    const stopped = awsJson<string[]>(
      `ecs list-tasks --cluster ${CLUSTER} --desired-status STOPPED ` +
        `--query 'taskArns[]'`,
    )
    if (stopped.length > 0) {
      const details = awsJson<{ group: string; reason: string }[]>(
        `ecs describe-tasks --cluster ${CLUSTER} --tasks ${stopped.join(" ")} ` +
          `--query 'tasks[].{group:group,reason:stoppedReason}'`,
      )
      // A scale-down or a deployment replaces tasks by design; an OOM kill, a
      // failed container or an essential-container exit does not.
      for (const task of details) {
        expect(
          task.reason || "",
          `${task.group} stopped for a non-routine reason`,
        ).toMatch(/scal|deployment|Scaling|user|service/i)
      }
    }
  })

  test("HEALTH-03 - the free and public target groups hold only healthy targets", async () => {
    skipUnlessDeployed("BT-1104 / 1218")
    // The poll below absorbs a draining target for up to 3 minutes per group.
    test.setTimeout(420_000)

    for (const name of [FREE_TG, PUBLIC_TG]) {
      const arn = awsJson<string>(
        `elbv2 describe-target-groups --names ${name} ` +
          `--query 'TargetGroups[0].TargetGroupArn'`,
      )
      // A rolling task replacement leaves a target draining or initialising for
      // a minute or two, which is the environment working. Poll so a transient
      // does not read as an outage, but keep the assertion absolute.
      const health = () =>
        awsJson<string[]>(
          `elbv2 describe-target-health --target-group-arn ${arn} ` +
            `--query 'TargetHealthDescriptions[].TargetHealth.State'`,
        )
      expect(
        health().length,
        `${name} has no registered targets`,
      ).toBeGreaterThan(0)
      await expect
        .poll(() => health().filter((state) => state !== "healthy"), {
          timeout: 180_000,
          intervals: [15_000],
          message: `${name} still holds an unhealthy target`,
        })
        .toEqual([])
    }
  })

  test("HEALTH-04 - the ALB routes each path family to the tier that owns it", () => {
    skipUnlessDeployed("806 / 807")

    const listener = awsJson<string>(
      `elbv2 describe-listeners --load-balancer-arn ` +
        `$(aws elbv2 describe-load-balancers --region ${AWS_REGION} ` +
        `--query 'LoadBalancers[?LoadBalancerName==\`${ENV}-optinist-lb\`]` +
        `.LoadBalancerArn' --output text) ` +
        // Terraform sets this listener to 443 or 8080 on enable_custom_domain,
        // so exclude the redirect listener rather than naming a port.
        `--query 'Listeners[?Port!=\`80\`].ListenerArn | [0]'`,
    )
    type Rule = {
      Priority: string
      Conditions: {
        Field: string
        PathPatternConfig?: { Values: string[] }
        HttpHeaderConfig?: { HttpHeaderName: string; Values: string[] }
      }[]
      Actions: { TargetGroupArn?: string }[]
    }
    const rules = awsJson<Rule[]>(
      `elbv2 describe-rules --listener-arn ${listener} --query 'Rules[]'`,
    )
    // Three-way on purpose: an else-branch of "free" also swallowed the
    // per-user premium target groups, so a Bearer rule repointed at
    // premium-<id>-tg or a stale TG still read as free.
    const tierOf = (rule: Rule) => {
      const arn = rule.Actions[0].TargetGroupArn || ""
      if (arn.includes(PUBLIC_TG)) return "public"
      if (arn.includes(FREE_TG)) return "free"
      return arn.replace(/^.*targetgroup\//, "") || "no target group"
    }
    const byPath = (path: string) =>
      rules.find((r) =>
        r.Conditions.some((c) =>
          (c.PathPatternConfig?.Values || []).includes(path),
        ),
      )

    // An authenticated request carries a Bearer token, and that alone is what
    // sends it to the tier holding the user's workspace files.
    const bearer = rules.find((r) =>
      r.Conditions.some(
        (c) => c.HttpHeaderConfig?.HttpHeaderName === "Authorization",
      ),
    )
    expect(bearer, "no Authorization-header rule on the listener").toBeTruthy()
    expect(bearer!.Conditions[0].HttpHeaderConfig!.Values).toEqual(["Bearer *"])
    expect(tierOf(bearer!), "Bearer rule tier").toBe("free")

    // Login and registration must reach a tier that is up before any user has
    // been assigned one, which is why they are pinned rather than left to the
    // Bearer rule.
    expect(tierOf(byPath("/auth/*")!), "/auth/* tier").toBe("public")
    expect(tierOf(byPath("/users/me")!), "/users/me tier").toBe("public")
    expect(tierOf(byPath("/api/register")!), "/api/register tier").toBe("free")
    expect(tierOf(byPath("/manifest.json")!), "static asset tier").toBe(
      "public",
    )
    expect(
      tierOf(byPath("/api/public/dataview")!),
      "public dataview tier",
    ).toBe("public")

    // The Bearer rule is a catch-all for authed traffic, so every path family
    // that must escape it has to be evaluated first.
    const bearerPriority = Number(bearer!.Priority)
    for (const path of [
      "/auth/*",
      "/users/me",
      "/api/register",
      "/manifest.json",
      "/api/public/dataview",
    ]) {
      expect(
        Number(byPath(path)!.Priority),
        `${path} must be evaluated before the Bearer rule`,
      ).toBeLessThan(bearerPriority)
    }

    // Anything unmatched belongs to the public tier: it is the only one that
    // serves the SPA shell to a visitor who has not logged in.
    const fallback = rules.find((r) => r.Priority === "default")
    expect(tierOf(fallback!), "default action tier").toBe("public")
  })

  test("HEALTH-05 - both auto scaling groups are in service within their bounds", () => {
    skipUnlessDeployed("826")

    const groups = awsJson<
      {
        name: string
        min: number
        max: number
        desired: number
        inService: number
      }[]
    >(
      `autoscaling describe-auto-scaling-groups ` +
        `--query 'AutoScalingGroups[?starts_with(AutoScalingGroupName, \`${ENV}-optinist\`)]` +
        `.{name:AutoScalingGroupName,min:MinSize,max:MaxSize,desired:DesiredCapacity,` +
        `inService:length(Instances[?LifecycleState==\`InService\`])}'`,
    )
    expect(groups.map((g) => g.name).sort()).toEqual([
      `${ENV}-optinist-asg`,
      `${ENV}-optinist-public-asg`,
    ])
    for (const group of groups) {
      expect(group.min, `${group.name} min`).toBeGreaterThan(0)
      expect(group.desired, `${group.name} desired`).toBeGreaterThanOrEqual(
        group.min,
      )
      expect(group.desired, `${group.name} desired`).toBeLessThanOrEqual(
        group.max,
      )
      expect(group.inService, `${group.name} in-service instances`).toBe(
        group.desired,
      )
    }
  })
})

test.describe("Storage and database", () => {
  test("HEALTH-06 - the RDS instance is available and its own alarms are evaluating OK", () => {
    skipUnlessDeployed("BT-1105 / 1214")

    expect(
      awsJson<string>(
        `rds describe-db-instances --db-instance-identifier ${RDS_INSTANCE} ` +
          `--query 'DBInstances[0].DBInstanceStatus'`,
      ),
    ).toBe("available")

    // CloudWatch already evaluates CPU, free storage and connection count
    // against the thresholds terraform set, so read its verdict rather than
    // re-deriving the bounds here. INSUFFICIENT_DATA is a failure: it means
    // nobody is watching.
    const alarms = awsJson<{ name: string; state: string }[]>(
      `cloudwatch describe-alarms --alarm-names ` +
        `${ENV}-optinist-rds-cpu-high ${ENV}-optinist-rds-storage-low ` +
        `${ENV}-optinist-rds-connections-high ` +
        `--query 'MetricAlarms[].{name:AlarmName,state:StateValue}'`,
    )
    expect(alarms).toHaveLength(3)
    for (const alarm of alarms) expect(alarm.state, alarm.name).toBe("OK")
  })

  test("HEALTH-07 - the app reaches the database over an encrypted connection", () => {
    skipUnlessDeployed("1215")
    const reason = sqlSkipReason()
    expect(reason, "row 1215 needs the deployed RDS over SSM").toBe("")

    // RequireTLS on the proxy is what governs the app's own channel. The
    // cipher read below is this harness's mariadb client, which passes --ssl
    // itself, so it can only ever corroborate - never establish - encryption.
    const proxies = awsJson<{ name: string; requireTls: boolean }[]>(
      `rds describe-db-proxies --query ` +
        `'DBProxies[].{name:DBProxyName,requireTls:RequireTLS}'`,
    )
    const proxy = proxies.find((p) => RDS_PROXY_HOST.startsWith(p.name))
    expect(proxy, `no RDS proxy matching ${RDS_PROXY_HOST}`).toBeTruthy()
    expect(
      proxy?.requireTls,
      `${proxy?.name} does not require TLS: the app may connect in the clear`,
    ).toBe(true)

    // `mariadb -N` prints "Ssl_cipher\t<value>"; an unencrypted session emits an
    // empty value, which trims down to the bare variable name and used to
    // satisfy both a /\S/ and a leading-character check.
    const value = runSql("SHOW STATUS LIKE 'Ssl_cipher'").split(/\t/)[1] ?? ""
    expect(
      value.trim(),
      "Ssl_cipher is empty: this session is not encrypted",
    ).toMatch(/^[A-Z0-9]/)
  })

  test("HEALTH-08 - the published-data EFS file system is available", () => {
    skipUnlessDeployed("820")

    const filesystems = awsJson<
      { name: string; state: string; encrypted: boolean; id: string }[]
    >(
      `efs describe-file-systems --query 'FileSystems[?Name==\`${ENV}-optinist-public-published-data\`]` +
        `.{name:Name,state:LifeCycleState,encrypted:Encrypted,id:FileSystemId}'`,
    )
    expect(filesystems, "public published-data EFS missing").toHaveLength(1)
    expect(filesystems[0].state).toBe("available")
    expect(filesystems[0].encrypted, "published data must be encrypted").toBe(
      true,
    )
  })

  test("HEALTH-09 - every bucket the database names really exists", () => {
    skipUnlessDeployed("BT-1111 / 1216")
    const reason = sqlSkipReason()
    expect(reason, "rows BT-1111 / 1216 need the deployed RDS over SSM").toBe(
      "",
    )

    // The backend silently falls back to the shared default bucket when a
    // per-user bucket is gone, so a named-but-absent bucket is data loss that
    // no API response would reveal.
    const declaredRows = runSql(
      "SELECT id, JSON_UNQUOTE(JSON_EXTRACT(attributes, '$.remote_bucket_name')) " +
        "FROM users WHERE active = 1 AND JSON_EXTRACT(attributes, " +
        "'$.remote_bucket_name') IS NOT NULL",
    )
      .split("\n")
      .map((line) => line.trim().split(/\s+/))
      .filter(([, name]) => name && name !== "NULL")
    const declared = declaredRows.map(([, name]) => name)
    expect(
      declared.length,
      "no active user names a remote bucket: the query or the schema moved",
    ).toBeGreaterThan(0)

    const live = new Set(
      awsJson<string[]>(
        `s3api list-buckets --query 'Buckets[?starts_with(Name, ` +
          `\`${BUCKET_PREFIX}\`)].Name'`,
      ),
    )
    expect(
      declared.length,
      `no declared bucket starts with ${BUCKET_PREFIX} - the prefix or the ` +
        `attributes key changed, and the contract below checks nothing`,
    ).toBeGreaterThan(0)
    const missing = declared.filter(
      (name) => !live.has(name) && !bucketExists(name),
    )
    expect(missing, "buckets named in the database but absent in S3").toEqual(
      [],
    )

    // The seeded admin account points at the shared app-storage bucket, so the
    // naming contract only binds the buckets that are per-user - and each of
    // those must carry its OWN user's id, or two users share a bucket.
    for (const [id, name] of declaredRows.filter(([, n]) =>
      n.startsWith(BUCKET_PREFIX),
    )) {
      expect(name, `user ${id}'s bucket naming contract`).toMatch(
        new RegExp(`^${BUCKET_PREFIX}${id}-[0-9a-f]{10}$`),
      )
    }
  })
})

test.describe("Alarms, logs and metrics", () => {
  test("HEALTH-10 - every alarm terraform declares still exists", () => {
    skipUnlessDeployed("1219")

    const names = awsJson<string[]>(
      `cloudwatch describe-alarms --alarm-name-prefix ${ENV}- ` +
        `--query 'MetricAlarms[].AlarmName'`,
    )
    expect(EXPECTED_ALARMS.filter((name) => !names.includes(name))).toEqual([])

    // An alarm whose source metric stops publishing sits in INSUFFICIENT_DATA
    // and still "exists", which is the deaf-alarm case existence alone misses.
    let blind = awsJson<{ name: string; state: string }[]>(
      `cloudwatch describe-alarms --alarm-name-prefix ${ENV}- ` +
        `--query 'MetricAlarms[?StateValue==\`INSUFFICIENT_DATA\`].` +
        `{name:AlarmName,state:StateValue}'`,
    ).filter((a) => EXPECTED_ALARMS.includes(a.name))
    // The premium service publishes no CPU/memory datapoint while its pool is
    // parked at zero tasks, so its two alarms sitting blind is the pool's
    // normal idle state, not a broken metric.
    const premiumIdle = new Set([
      `${ENV}-premium-cpu-high`,
      `${ENV}-premium-memory-high`,
    ])
    if (
      blind.some((a) => premiumIdle.has(a.name)) &&
      premiumDesiredCount() === 0
    ) {
      const parked = blind.filter((a) => premiumIdle.has(a.name))
      console.log(
        `premium desiredCount is 0: ${parked.map((a) => a.name).join(", ")} ` +
          `in INSUFFICIENT_DATA is the idle pool, not a dead metric`,
      )
      blind = blind.filter((a) => !premiumIdle.has(a.name))
    }
    expect(
      blind.map((a) => a.name),
      "alarms with no data to evaluate - their metric stopped publishing",
    ).toEqual([])
  })

  test("HEALTH-11 - no health alarm is in ALARM", () => {
    skipUnlessDeployed("BT-1106 / 1219")

    // The prefix is the environment, not "development-optinist": the
    // background, premium and public alarms live outside that narrower one and
    // an ALARM on any of them is exactly what this row is looking for.
    const alarms = awsJson<AlarmDetail[]>(
      `cloudwatch describe-alarms --alarm-name-prefix ${ENV}- ` +
        `--query 'MetricAlarms[].{name:AlarmName,state:StateValue,` +
        `actions:AlarmActions,namespace:Namespace,metric:MetricName,` +
        `dimensions:Dimensions,period:Period,statistic:Statistic,` +
        `extendedStatistic:ExtendedStatistic,threshold:Threshold,` +
        `operator:ComparisonOperator,evaluationPeriods:EvaluationPeriods}'`,
    )
    // Filtering server-side on ALARM would let a mistyped prefix answer with an
    // empty list, which reads as "nothing is firing" instead of "nothing was
    // looked at".
    expect(
      alarms.length,
      `no alarm matched the ${ENV}- prefix`,
    ).toBeGreaterThan(EXPECTED_ALARMS.length - 1)
    const inAlarm = alarms
      .filter((a) => a.state === "ALARM")
      .filter((a) => a.name !== COST_ALARM && !isScaleInTrigger(a))
    // An alarm whose metric has already recovered is reported, not failed: with
    // two evaluation periods the state trails the metric by minutes, so a run
    // that lands in that gap was describing the clock. The scheduled 08:00 JST
    // start-up is the routine case - the public tier boots with no healthy
    // target, and the alarm stays in ALARM for a few minutes after the targets
    // come back.
    const recovering = inAlarm.filter((a) => !stillBreaching(a))
    if (recovering.length) {
      console.log(
        `alarms in ALARM whose metric has already recovered (not treated as ` +
          `firing): ${recovering.map((a) => a.name).join(", ")}`,
      )
    }
    const firing = inAlarm
      .filter((a) => stillBreaching(a))
      .map((a) => `${a.name} (actions: ${a.actions.join(" ") || "none"})`)
    expect(
      firing,
      "alarms whose metric is breaching right now. If this names " +
        "public-tg-unhealthy-hosts within ~25 minutes of the scheduled 08:00 " +
        "JST start-up, the environment is still booting rather than broken",
    ).toEqual([])
  })

  // Row 824's first half, read-only. The alarm's own history records the
  // datapoints CloudWatch evaluated when it fired, so this asserts the alarm
  // fires because ALB-published data crossed the threshold - the half of the row
  // a `set-alarm-state` test can never prove, since a hand-written state carries
  // a caller-supplied reason and no evaluated datapoints. Costs nothing and
  // disturbs nothing: this environment produces a real transition every weekday
  // start-up. The row's remaining half, traffic continuing to the surviving
  // target, needs the outage itself (ASG-01).
  test("HEALTH-27 - the public TG alarm's last ALARM was driven by real datapoints", () => {
    skipUnlessDeployed("824")

    const name = `${ENV}-optinist-public-tg-unhealthy-hosts`
    const history = awsJson<{ summary: string; data: string }[]>(
      `cloudwatch describe-alarm-history --alarm-name ${name} ` +
        `--history-item-type StateUpdate --max-items 30 ` +
        `--query 'AlarmHistoryItems[].{summary:HistorySummary,data:HistoryData}'`,
    )
    const into = history.find((item) => item.summary.includes("to ALARM"))
    // Alarm history is retained for two weeks. An environment that has not been
    // unhealthy in that time has nothing to read, which is not a failure.
    test.skip(
      !into,
      `row 824: ${name} has no ALARM transition in its retained history`,
    )

    const record = JSON.parse(into!.data) as {
      newState: {
        stateValue: string
        stateReason: string
        stateReasonData: {
          threshold: number
          evaluatedDatapoints: { value?: number }[]
        }
      }
    }
    expect(record.newState.stateValue).toBe("ALARM")
    // CloudWatch's own wording for an evaluation. set-alarm-state puts the
    // caller's string here instead.
    expect(
      record.newState.stateReason,
      "the transition was written by hand, not evaluated from the metric",
    ).toContain("Threshold Crossed")

    const evaluated = record.newState.stateReasonData.evaluatedDatapoints
      .map((d) => d.value)
      .filter((v): v is number => typeof v === "number")
    expect(
      evaluated.length,
      "the ALARM transition carries no evaluated datapoints",
    ).toBeGreaterThan(0)
    expect(
      Math.max(...evaluated),
      "every evaluated datapoint was within the threshold, so the alarm " +
        "fired on something other than its metric",
    ).toBeGreaterThan(record.newState.stateReasonData.threshold)
  })

  test("HEALTH-12 - the free and public tiers log live and hit no fatal fault", () => {
    skipUnlessDeployed("BT-1107 / 1205")

    // Absence only means anything once a line from the same window proves
    // delivery is current; the ALB health check guarantees one on both of these
    // tiers every minute. The premium tier is excluded on purpose: it idles at
    // zero tasks, so it has no line to prove that with.
    const since = Date.now() - 5 * 60_000
    for (const group of [FREE_LOG, PUBLIC_LOG]) {
      expect(
        cloudwatchHas(group, "HTTP/1.1", since),
        `${group} delivered no line in the last 5 minutes`,
      ).toBe(true)
    }

    // Deliberately not "no ERROR lines": a shared test environment earns them
    // legitimately - declined-card webhooks, PUB-04's frontend error report,
    // cancelled workflow runs. These markers are different, and mean the tier
    // cannot serve rather than that a request failed.
    const fatal = [
      "connect to MySQL server",
      "OperationalError",
      "Cannot allocate memory",
      "Child process",
    ]
    const hour = Date.now() - 60 * 60_000
    for (const group of [FREE_LOG, PUBLIC_LOG]) {
      for (const marker of fatal) {
        expect(
          cloudwatchHas(group, marker, hour),
          `${group} logged "${marker}" in the last hour`,
        ).toBe(false)
      }
    }
  })

  test("HEALTH-13 - the background scheduler is running both of its jobs", () => {
    skipUnlessDeployed("1208 / 1209 / BT-1108")

    const { lastIngestion, text } = logTail(BACKGROUND_LOG)
    expect(
      Date.now() - lastIngestion,
      "background tier delivered no log line in the last 15 minutes",
    ).toBeLessThan(15 * 60_000)
    // Every 5 minutes, so the tail always holds one.
    expect(text, "the 5-minute sync job has not logged a run").toContain(
      "Starting published experiment validation job",
    )
    // Hourly, and the tail spans several hours of an idle environment.
    expect(text, "the hourly cleanup job has not logged a run").toContain(
      "Starting data cleanup job",
    )
    expect(text, "the background tier logged a traceback").not.toContain(
      "Traceback (most recent call last)",
    )
  })

  test("HEALTH-14 - the background namespace publishes every metric, and recently", () => {
    skipUnlessDeployed("BT-1108 / 1210")

    const published = awsJson<string[]>(
      `cloudwatch list-metrics --namespace ${METRIC_NAMESPACE} ` +
        `--query 'Metrics[].MetricName'`,
    )
    // Only the 5-minute sync job publishes unconditionally. The cleanup job
    // returns early when no user is eligible, without publishing, so on a quiet
    // environment its three metrics fall outside list-metrics' lookback and
    // their absence means nothing. HEALTH-13 is what proves that job alive.
    for (const name of ["ExperimentsSynced", "SyncErrors", "SyncErrorRate"]) {
      expect(published, `${name} absent from ${METRIC_NAMESPACE}`).toContain(
        name,
      )
    }

    const now = Math.floor(Date.now() / 1000)
    const recent = awsJson<unknown[]>(
      `cloudwatch get-metric-statistics --namespace ${METRIC_NAMESPACE} ` +
        `--metric-name ExperimentsSynced --statistics Sum --period 300 ` +
        `--start-time ${now - 900} --end-time ${now} --query 'Datapoints'`,
    )
    expect(
      recent.length,
      "ExperimentsSynced published no datapoint in the last 15 minutes",
    ).toBeGreaterThan(0)

    // Metric identity includes the dimension set, and this one is published per
    // experiment, so it has to be searched across all dimensioned series
    // rather than queried by name. Only ever present once something has failed
    // repeatedly, hence the guard.
    // Run unconditionally: the metric only exists once something has failed
    // repeatedly, so gating on its presence meant the check - and the shell
    // quoting it depends on - never ran in the state it was written for.
    {
      const search =
        `SUM(SEARCH('{${METRIC_NAMESPACE},ExperimentId,WorkspaceId} ` +
        `MetricName=\"PersistentSyncFailure\"', 'Sum', 300))`
      // The expression's own single quotes would close the shell argument, so
      // it is quoted rather than interpolated raw.
      const queries = JSON.stringify([
        { Id: "psf", Expression: search, Period: 300 },
      ]).replace(/'/g, `'\\''`)
      const failures = awsJson<{ Values: number[] }[]>(
        `cloudwatch get-metric-data --start-time ${now - 3600} ` +
          `--end-time ${now} --metric-data-queries '${queries}' ` +
          `--query 'MetricDataResults'`,
      )
      expect(
        failures.length,
        "the PersistentSyncFailure query returned no result",
      ).toBe(1)
      expect(
        failures[0].Values,
        "an experiment has been failing to sync repeatedly",
      ).toEqual([])
    }
  })

  test("HEALTH-15 - the publish sync is not failing", () => {
    skipUnlessDeployed("1212 / 1213")

    const errors = metricSum("SyncErrors", 24)
    const synced = metricSum("ExperimentsSynced", 24)
    // The job publishes (0, 0) on its "nothing pending" early return, so an
    // idle environment legitimately sums to zero and a value-based control
    // would be permanently red. What must hold is that it published at all:
    // no datapoints means the job stopped running or the namespace moved.
    expect(
      synced.points,
      "ExperimentsSynced published no datapoint in 24h",
    ).toBeGreaterThan(0)
    expect(
      errors.points,
      "SyncErrors published no datapoint in 24h",
    ).toBeGreaterThan(0)
    // This environment is not idle, and the e2e suite is what makes it noisy.
    // Three storage rows drive a published record into `error` on purpose:
    // `S3-04` takes its config out of S3 to reach the public error state,
    // `S3-07` does the same to cover the `pending -> error -> synced`
    // transitions of sheet rows 721/722, and `S3-08` provokes one whenever a
    // tick lands while it is holding a row pending. Each is a single error the
    // same row then repairs, so a small non-zero total is the storage lane's
    // signature rather than a fault, and a 24h window can hold more than one
    // run of it. What this row is looking for would not fit in that budget: a
    // sync job failing systematically puts an error on most of the 288 ticks
    // in the window, not on six of them.
    const SUITE_ERROR_BUDGET = 6
    expect(errors.sum, "SyncErrors over the last 24h").toBeLessThanOrEqual(
      SUITE_ERROR_BUDGET,
    )

    // The rate decides something only once the day's volume dwarfs those
    // injections. Below that the suite IS the denominator - one deliberate
    // error against a quiet day's ~14 validations reads as 7%, and the job's
    // own SyncErrorRate reads 100% for the single-experiment run that carried
    // it - so an ungated 5% threshold fails on a healthy day and says nothing
    // about a sick one. On a day with real traffic it is the row's own check
    // again. An error that never recovers is caught elsewhere regardless:
    // PersistentSyncFailure, asserted empty by HEALTH-14.
    const RATE_IS_MEANINGFUL_ABOVE = 50
    if (synced.sum >= RATE_IS_MEANINGFUL_ABOVE) {
      expect(
        errors.sum / synced.sum,
        "sync error rate over the last 24h",
      ).toBeLessThan(0.05)
    } else {
      console.log(
        `[17-aws-health] HEALTH-15 error rate not decided: ${synced.sum} ` +
          `validations in 24h is below the ${RATE_IS_MEANINGFUL_ABOVE} the ` +
          `ratio needs to outweigh the suite's own injected errors ` +
          `(${errors.sum} of them)`,
      )
    }
  })

  test("HEALTH-16 - every tier's log group exists with a retention policy", () => {
    skipUnlessDeployed("825")

    const groups = awsJson<{ name: string; retention: number | null }[]>(
      `logs describe-log-groups --log-group-name-prefix /ecs/${ENV} ` +
        `--query 'logGroups[].{name:logGroupName,retention:retentionInDays}'`,
    )
    for (const name of [FREE_LOG, PREMIUM_LOG, PUBLIC_LOG, BACKGROUND_LOG]) {
      const group = groups.find((g) => g.name === name)
      expect(group, `${name} missing`).toBeTruthy()
      // Never-expire is the failure this row is looking for: it is what turns
      // a log group into an unbounded bill.
      expect(group!.retention, `${name} retention`).toBeGreaterThan(0)
    }
  })

  test("HEALTH-17 - the published-data cleanup runs on its nightly schedule", () => {
    skipUnlessDeployed("821")

    const rules = awsJson<{ state: string; schedule: string }[]>(
      `events list-rules --name-prefix ${ENV}-public-cleanup-schedule ` +
        `--query 'Rules[].{state:State,schedule:ScheduleExpression}'`,
    )
    expect(rules, "public cleanup rule missing").toHaveLength(1)
    expect(rules[0].state).toBe("ENABLED")
    // 19:00 UTC is 04:00 JST, outside the working day the sheet specifies.
    expect(rules[0].schedule).toBe("cron(0 19 * * ? *)")
  })
})

test.describe("HTTP contract", () => {
  test("HEALTH-18 - the public tier serves what is open and guards what is not", async ({
    request,
  }) => {
    skipUnlessDeployed("BT-1101 / 800 / 801 / 808 / 2028")
    const base = process.env.BASE_URL!

    // The SPA shell and its assets reach a visitor with no session at all.
    for (const path of ["/", "/manifest.json", "/health"]) {
      const res = await request.get(`${base}${path}`)
      expect(res.status(), `GET ${path}`).toBe(200)
    }
    expect(
      (await request.get(`${base}/api/public/dataview?limit=5`)).status(),
    ).toBe(200)

    // A token the backend never issued must not buy access.
    const guarded = await request.get(`${base}/api/dataview?limit=5`, {
      headers: { Authorization: "Bearer invalid-token" },
    })
    expect([401, 403], `bad token got ${guarded.status()}`).toContain(
      guarded.status(),
    )

    // With no Authorization header the request falls to the ALB's default
    // action, which is the public tier, and that tier mounts no authenticated
    // router at all. So these routes do not exist there rather than refusing:
    // 404, not the 401 the bad token above earned from the free tier. The
    // difference is the row, and it is what proves the tier really is
    // running in public mode.
    for (const path of ["/experiments", "/experiments/1", "/workspaces"]) {
      const res = await request.get(`${base}${path}`, {
        headers: { Accept: "application/json" },
      })
      expect(res.status(), `unauthenticated ${path}`).toBe(404)
    }
  })

  test("HEALTH-19 - the environment's certificate is valid and not expiring", () => {
    skipUnlessDeployed("BT-1101 / 1221")
    const base = process.env.BASE_URL!
    // The dev ALB is served plain on :8080, so there is no certificate to read
    // there. On subscr, BASE_URL is the https production origin and this is
    // the row's TLS half.
    test.skip(
      !base.startsWith("https://"),
      `rows BT-1101 / 1221: ${base} is not https, so it terminates no TLS`,
    )
    const host = new URL(base).host.split(":")[0]

    const notAfter = execSync(
      `echo | openssl s_client -servername ${host} -connect ${host}:443 2>/dev/null | ` +
        `openssl x509 -noout -enddate`,
      { timeout: 40_000, shell: "/bin/bash" },
    )
      .toString()
      .replace("notAfter=", "")
      .trim()
    const daysLeft = (Date.parse(notAfter) - Date.now()) / 86_400_000
    expect(
      daysLeft,
      `${host} certificate expires in ${Math.round(daysLeft)} days (${notAfter})`,
    ).toBeGreaterThan(14)
  })

  test("HEALTH-25 - registration reaches the free tier and refuses a duplicate", async ({
    request,
  }) => {
    skipUnlessDeployed("804")
    skipWithoutCreds()
    const reason = sqlSkipReason()
    expect(reason, "row 804 counts the account's rows afterwards").toBe("")

    const email = FREE_USER.email
    const countRows = () =>
      Number(
        runSql(
          `SELECT COUNT(*) FROM users WHERE email = '${sqlLiteral(email)}' ` +
            "AND active = 1",
        ),
      )
    const before = countRows()
    expect(before, `${email} should have exactly one active row`).toBe(1)

    // 400 is the whole point: the public tier has no /api/register router and
    // would answer 405, so a refusal that names the duplicate proves the ALB
    // rule really forwarded this POST to the free tier.
    const res = await request.post(`${process.env.BASE_URL}/api/register`, {
      data: {
        name: "e2e duplicate probe",
        role_id: 20,
        email,
        password: "Probe#Pass123",
      },
      timeout: 30_000,
    })
    expect(res.status(), await res.text()).toBe(400)
    expect(await res.text()).toContain("already registered")

    // A refused registration must not have written anything.
    expect(countRows(), "rows after the refused registration").toBe(before)
  })

  test("HEALTH-26 - the public read API serves concurrent anonymous traffic", async ({
    request,
  }) => {
    skipUnlessDeployed("832")
    const base = process.env.BASE_URL!

    const started = Date.now()
    const responses = await Promise.all(
      Array.from({ length: 20 }, () =>
        request.get(`${base}/api/public/dataview?limit=5`, { timeout: 30_000 }),
      ),
    )
    const elapsed = (Date.now() - started) / 1000

    const bad = responses.map((r) => r.status()).filter((s) => s !== 200)
    expect(bad, "non-200 responses under concurrent load").toEqual([])
    // The sheet's floor. Generous on purpose: this is a smoke test for the
    // public tier serving in parallel, not a benchmark.
    expect(
      responses.length / elapsed,
      `throughput was ${(responses.length / elapsed).toFixed(1)}/s`,
    ).toBeGreaterThan(10)
  })

  // Row 801: the hashed bundle URLs are read out of the shell itself, so a
  // build or routing drift 404s here instead of passing on a hardcoded name.
  test("HEALTH-28 - every asset the shell references is served", async ({
    request,
  }) => {
    skipUnlessDeployed("801")
    const base = process.env.BASE_URL!

    const shell = await request.get(`${base}/`)
    expect(shell.status(), "GET /").toBe(200)
    const assets = [
      ...(await shell.text()).matchAll(
        /(?:src|href)="(\/[^"]+\.(?:js|css|ico|png|json))"/g,
      ),
    ].map((m) => m[1])
    expect(
      assets.some((a) => a.startsWith("/static/")),
      `the shell references no /static bundle, only: ${assets.join(", ")}`,
    ).toBe(true)

    for (const asset of new Set(assets)) {
      const res = await request.get(`${base}${asset}`)
      expect(res.status(), `GET ${asset}`).toBe(200)
      expect((await res.body()).length, `${asset} is empty`).toBeGreaterThan(0)
    }
  })

  // Row 2028's negative branch: an experiment that EXISTS but is unpublished
  // answers an anonymous reproduce with 404 - the published_only gate, not a
  // missing record.
  test("HEALTH-29 - an unpublished experiment is invisible to an anonymous reproduce", async ({
    request,
  }) => {
    skipUnlessDeployed("2028")
    const reason = sqlSkipReason()
    expect(reason, "row 2028 selects a private uid from the database").toBe("")

    const row = runSql(
      "SELECT workspace_id, uid FROM experiment_records " +
        "WHERE publish_status = 0 LIMIT 1",
    )
      .trim()
      .split(/\s+/)
    test.skip(
      row.length < 2,
      "row 2028: the environment holds no unpublished experiment to probe",
    )
    const [wsId, uid] = row

    const res = await request.get(
      `${process.env.BASE_URL}/api/public/dataview/workflow/reproduce/${wsId}/${uid}`,
      { failOnStatusCode: false },
    )
    expect(
      res.status(),
      `anonymous reproduce of private ${wsId}/${uid}: ${await res.text()}`,
    ).toBe(404)
  })
})

test.describe("Data integrity", () => {
  test.beforeEach(() => {
    skipUnlessDeployed(
      "2008 / 2009 / 2010 / 2012 / 2018 / 2023 / 2024 / 116 / 117 / 535",
    )
    const reason = sqlSkipReason()
    expect(reason, "the integrity rows need the deployed RDS over SSM").toBe("")
  })

  test("HEALTH-20 - no subscription row is orphaned, duplicated or on an unknown plan", () => {
    const count = (sql: string) => Number(runSql(sql))

    // Every check below is a COUNT(*) == 0, which a truncated table, a wrong
    // schema or a WHERE that stopped matching all satisfy. This is the control
    // that says the rows being vetted actually exist.
    expect(
      count("SELECT COUNT(*) FROM subscription_users"),
      "subscription_users is empty - the integrity checks vet nothing",
    ).toBeGreaterThan(0)

    expect(
      count(
        "SELECT COUNT(*) FROM subscription_users su " +
          "LEFT JOIN users u ON su.user_id = u.id WHERE u.id IS NULL",
      ),
      "subscription rows pointing at a user that no longer exists",
    ).toBe(0)
    expect(
      count(
        "SELECT COUNT(*) FROM (SELECT user_id FROM subscription_users " +
          "GROUP BY user_id HAVING COUNT(*) > 1) t",
      ),
      "users holding more than one subscription row",
    ).toBe(0)
    expect(
      count(
        "SELECT COUNT(*) FROM subscription_users su LEFT JOIN subscription_plans p " +
          "ON su.plan_id = p.id WHERE p.id IS NULL",
      ),
      "subscription rows on a plan that does not exist",
    ).toBe(0)
    expect(
      count(
        "SELECT COUNT(*) FROM subscription_users WHERE user_id IS NULL " +
          "OR plan_id IS NULL OR expiration IS NULL",
      ),
      "subscription rows missing a required value",
    ).toBe(0)
    expect(
      count(
        "SELECT COUNT(*) FROM free_user_assignments fa " +
          "LEFT JOIN users u ON fa.user_id = u.id WHERE u.id IS NULL",
      ),
      "instance assignments pointing at a user that no longer exists",
    ).toBe(0)
  })

  test("HEALTH-21 - no subscription row carries an impossible timestamp", () => {
    expect(
      Number(runSql("SELECT COUNT(*) FROM subscription_users")),
      "subscription_users is empty - the timestamp checks vet nothing",
    ).toBeGreaterThan(0)

    // Not asserted here: expiration earlier than created_at. The seeded
    // expired-premium fixtures backdate it deliberately, so it is fixture
    // shape rather than a timestamp the app got wrong.
    expect(
      Number(
        runSql(
          "SELECT COUNT(*) FROM subscription_users " +
            "WHERE created_at > NOW() OR updated_at > NOW()",
        ),
      ),
      "subscription rows stamped in the future",
    ).toBe(0)
    expect(
      Number(
        runSql(
          "SELECT COUNT(*) FROM subscription_users WHERE updated_at < created_at",
        ),
      ),
      "subscription rows last updated before they were created",
    ).toBe(0)
  })

  test("HEALTH-22 - the free test account's own rows describe a free account", () => {
    skipWithoutCreds()
    const email = sqlLiteral(FREE_USER.email)

    const user = runSql(
      `SELECT id, active FROM users WHERE email = '${email}' AND active = 1`,
    ).split(/\s+/)
    expect(user[0], `${FREE_USER.email} has no active users row`).toMatch(
      /^\d+$/,
    )

    // Plan 1 is Free. A missing row means the same thing: nothing was ever
    // purchased. Anything else means the account under test is not free, and
    // every free-tier assertion in the suite is running against the wrong
    // account.
    const plan = runSql(
      "SELECT su.plan_id FROM subscription_users su JOIN users u " +
        `ON su.user_id = u.id WHERE u.email = '${email}'`,
    )
    expect(
      plan === "" || plan === "1",
      `${FREE_USER.email} is on plan_id ${plan}, not the free tier`,
    ).toBe(true)
  })

  test("HEALTH-23 - every plan carries the Stripe ids it is sold under", () => {
    // The checkout session is built from these two ids, so a NULL here is an
    // upgrade that cannot start rather than a display problem.
    expect(
      Number(
        runSql(
          "SELECT COUNT(*) FROM subscription_plans WHERE stripe_product_id IS NULL " +
            "OR stripe_price_id IS NULL",
        ),
      ),
      "plans with no Stripe product or price id",
    ).toBe(0)
    expect(
      runSql(
        "SELECT stripe_product_id, stripe_price_id FROM subscription_plans " +
          "WHERE name = 'Premium'",
      ),
    ).toMatch(/^prod_\w+\s+price_\w+$/)
  })

  test("HEALTH-24 - the free test account holds exactly one instance assignment", () => {
    skipWithoutCreds()
    const email = sqlLiteral(FREE_USER.email)

    const rows = Number(
      runSql(
        "SELECT COUNT(*) FROM free_user_assignments fa JOIN users u " +
          `ON fa.user_id = u.id WHERE u.email = '${email}'`,
      ),
    )
    // The unique constraint should make two impossible; zero means the account
    // has never logged in against this environment, which makes the free-tier
    // routing rows unverified rather than passing.
    expect(rows, `${FREE_USER.email} instance assignments`).toBe(1)

    const instance = runSql(
      "SELECT fa.instance_id FROM free_user_assignments fa JOIN users u " +
        `ON fa.user_id = u.id WHERE u.email = '${email}'`,
    )
    expect(instance, "assignment names no instance").toMatch(/^i-[0-9a-f]+$/)
  })
})
