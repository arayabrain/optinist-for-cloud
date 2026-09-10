import { execFileSync } from "child_process"
import * as fs from "fs"
import * as os from "os"
import * as path from "path"

import { test, expect, request } from "@playwright/test"

import {
  FREE_USER,
  REPO_ROOT,
  STRIPE_USER,
  apiLogin,
  isLocalBaseUrl,
  liveStripeSkipReason,
  skipWithoutCreds,
  stripeAccountSkipReason,
  stripeGet,
  stripeSubscriptionFor,
} from "./helpers"

// The Stripe catalogue / customer / subscription rows of system sheet 09, and
// the Stripe-side half of sheet 20's integrity rows. Every check is a GET
// against Stripe plus a read-only SQL audit, so like 17-aws-health this lane
// mutates nothing and needs no opt-in flag.
//
//   BASE_URL=<deployed> npx playwright test e2e/18-stripe-audit.spec.ts
//   HEALTH_ENV=subscr selects production, as it does for 17-aws-health.
//
// The checks themselves live in infrastructure/scripts/manual_test_scan.py,
// which already implements all forty of them and fetches the environment's own
// Stripe key from Secrets Manager (never a live key without --allow-live).
// Rewriting them here would duplicate a thousand proven lines, so the lane runs
// the scan once and asserts its per-row verdicts. What it adds over reading the
// report by hand is that a regression fails a test rather than waiting to be
// noticed.

const SCAN = path.join(
  REPO_ROOT,
  "infrastructure",
  "scripts",
  "manual_test_scan.py",
)
const SCAN_TIMEOUT_MS = 240_000
// Same selector 17-aws-health reads: hardcoding development audited dev's Stripe during a prod release round.
const CHECK = process.env.HEALTH_ENV === "subscr" ? "production" : "development"

// endsWith("stripe.com") alone accepts evilstripe.com, so the bare domain and a
// real subdomain are the only two shapes allowed.
function isStripeHost(hostname: string): boolean {
  return hostname === "stripe.com" || hostname.endsWith(".stripe.com")
}

type Verdict = { sheet: string; status: string; evidence: string }

let verdicts: Record<string, Verdict> = {}
let scanError = ""
let scanSkipReason = ""

test.beforeAll(() => {
  if (isLocalBaseUrl()) return
  // The scan refuses to choose its own target on production, and this lane's
  // target is TEST_STRIPE_*. Unset, there is no account to audit - which is
  // environment-shaped inability, not a failure to report.
  if (CHECK === "production" && !STRIPE_USER.email) {
    scanSkipReason =
      "TEST_STRIPE_EMAIL is not set, and --check production will not select " +
      "its own target"
    return
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "e2e-scan-"))
  try {
    // Pinned to the account this lane is about. Unpinned, the scan targets
    // whichever premium subscription was updated last, so an unrelated fixture
    // account being touched swings every row below onto it.
    const target = STRIPE_USER.email ? ["--user-email", STRIPE_USER.email] : []
    // execFileSync, not execSync: the scan path and the pinned address both come
    // from the environment, and argv never reaches a shell.
    execFileSync(
      "python3",
      [
        SCAN,
        "--check",
        CHECK,
        ...target,
        "-o",
        path.join(dir, "report.md"),
        "--json",
        path.join(dir, "scan.json"),
      ],
      {
        cwd: REPO_ROOT,
        timeout: SCAN_TIMEOUT_MS,
        stdio: ["pipe", "pipe", "pipe"],
      },
    )
  } catch (e) {
    // A non-zero exit means the scan itself found a FAIL row, which the
    // per-row assertions below are about to name. Only a missing result file
    // is a broken run. The child's stderr rides alongside the message because
    // execFileSync's first line is only the command that was run, which on its
    // own says nothing about why the scan stopped.
    const err = e as Error & { stderr?: Buffer | string }
    scanError = [err.message.split("\n")[0], String(err.stderr ?? "").trim()]
      .filter(Boolean)
      .join(" -- ")
  }
  const jsonPath = path.join(dir, "scan.json")
  if (fs.existsSync(jsonPath)) {
    verdicts = JSON.parse(fs.readFileSync(jsonPath, "utf-8"))
    scanError = ""
  }
  fs.rmSync(dir, { recursive: true, force: true })
})

function skipUnlessDeployed(rows: string) {
  test.skip(
    isLocalBaseUrl(),
    `rows ${rows}: reads the deployed environment's Stripe account and RDS; BASE_URL is local`,
  )
  test.skip(!!scanSkipReason, `rows ${rows}: ${scanSkipReason}`)
  expect(scanError, `the scan did not produce a result: ${scanError}`).toBe("")
  // Without this a scan that silently produced nothing would pass every test
  // below by vacuously finding no bad row.
  expect(
    Object.keys(verdicts).length,
    "the scan reported no rows at all",
  ).toBeGreaterThan(30)
}

// Asserts each row was actually scanned before judging it, so a row the scan
// stops emitting fails here instead of quietly leaving the sheet uncovered.
function expectPass(rows: string[]) {
  const missing = rows.filter((row) => !(row in verdicts))
  expect(missing, "rows the scan did not report on").toEqual([])
  const bad = rows
    .filter((row) => verdicts[row].status !== "PASS")
    .map((row) => `${row}: ${verdicts[row].status} - ${verdicts[row].evidence}`)
  expect(bad).toEqual([])
}

// Stripe keeps events for 30 days. An account whose last change is older than
// that reports INFO rather than PASS, which is the retention window talking, not
// a fault - so these rows accept it while still failing on FAIL.
function expectPassOrInfo(rows: string[]) {
  const missing = rows.filter((row) => !(row in verdicts))
  expect(missing, "rows the scan did not report on").toEqual([])
  const bad = rows
    .filter((row) => !["PASS", "INFO"].includes(verdicts[row].status))
    .map((row) => `${row}: ${verdicts[row].status} - ${verdicts[row].evidence}`)
  expect(bad).toEqual([])
}

test.describe("Stripe catalogue", () => {
  test("AUDIT-01 - the product catalogue matches what the database sells", () => {
    skipUnlessDeployed("901 / 902 / 903 / 904")
    // 903 is the one that matters most: a plan whose stripe_price_id no longer
    // resolves is an upgrade button that 500s at checkout.
    expectPass(["901", "902", "903", "904"])
  })

  test("AUDIT-02 - tax is registered and configured on the session", () => {
    skipUnlessDeployed("907 / 908 / 921 / 923 / 924 / 925")
    expectPass(["907", "908", "921", "923", "924", "925"])
  })

  test("AUDIT-03 - the webhook endpoint is live and subscribed", () => {
    skipUnlessDeployed("920")
    expectPass(["920"])
  })
})

test.describe("Stripe customer and subscription state", () => {
  test("AUDIT-04 - the premium account has exactly one customer and one subscription", () => {
    skipUnlessDeployed("927 / 928 / 929 / 914 / 919 / 2014")
    // 914 / 919: the billing address Stripe was told to collect (row 910) has to
    // reach the customer, or the tax it charged rests on nothing.
    expectPass(["927", "928", "929", "914", "919", "2014"])
  })

  test("AUDIT-05 - the live subscription, its price and its billing dates read back", () => {
    skipUnlessDeployed("930 / 931 / 932 / 933")
    expectPass(["930", "931", "933"])
    // 932 reports INFO on development while the DB lags a webhook Stripe has
    // yet to redeliver after the nightly stop.
    expectPassOrInfo(["932"])
  })

  test("AUDIT-06 - invoices, payments and the event timeline are consistent", () => {
    skipUnlessDeployed("926 / 934 / 935 / 936")
    expectPass(["926", "934", "935"])
    // 936 is the event timeline, which ages out of Stripe's 30-day retention.
    expectPassOrInfo(["936"])
  })
})

test.describe("Database and Stripe agree", () => {
  test("AUDIT-07 - no duplicate customer, and the stored ids match Stripe", () => {
    skipUnlessDeployed("2013 / 2016")
    expectPass(["2013", "2016"])
  })

  test("AUDIT-08 - the stored billing dates match Stripe's", () => {
    skipUnlessDeployed("2017")
    // Drift here means the app is showing a renewal date Stripe will not
    // honour; INFO is development lagging a webhook redelivery, not drift.
    expectPassOrInfo(["2017"])
  })
})

// The rows above are the scan's; this one is not, because it needs a token. The
// app's invoice list is Stripe data passed through our own serialiser, so the
// only way to know it is faithful is to fetch both and compare - and the links
// it hands the user are worth resolving, since a dead invoice URL looks fine in
// the UI right up to the click.
//
// Rows 244 / 245 (the real invoice row's values), 246 (the hosted URL opens),
// 247 / 249 (a valid PDF and receipt download), 250 (field-by-field agreement).
test.describe("The app's invoice list against Stripe's", () => {
  test("AUDIT-09 - every invoice the app reports matches Stripe and its links resolve", async () => {
    test.skip(
      isLocalBaseUrl(),
      "rows 244-250: reads the deployed app and its Stripe account; BASE_URL is local",
    )
    // Before stripeAccountSkipReason, which reads Stripe itself and would throw
    // on the live key this skip exists to avoid.
    const liveKey = liveStripeSkipReason()
    test.skip(!!liveKey, `rows 244-250: ${liveKey}`)
    const reason = stripeAccountSkipReason()
    test.skip(!!reason, `rows 244-250: ${reason}`)
    test.setTimeout(180_000)

    const { api, headers } = await apiLogin(
      STRIPE_USER.email,
      STRIPE_USER.password,
    )
    let ours: Record<string, any>[]
    let theirs: Record<string, any>[]
    try {
      const me = await api.get("/users/me", { headers })
      expect(me.ok()).toBeTruthy()
      const userId = (await me.json()).id

      const res = await api.get(`/api/subsc/invoices/${userId}`, { headers })
      expect(res.ok(), `GET invoices: ${await res.text()}`).toBeTruthy()
      ours = await res.json()
      const sub = stripeSubscriptionFor(STRIPE_USER.email)
      theirs = stripeGet("/v1/invoices", {
        customer: sub.customer as string,
        limit: 100,
      }).data as Record<string, any>[]
    } finally {
      await api.dispose()
    }

    // An empty list would make every comparison below vacuous
    expect(ours.length, "the app reported no invoices at all").toBeGreaterThan(
      0,
    )

    const byId = new Map(theirs.map((i) => [i.id, i]))
    let periodsCompared = 0
    for (const mine of ours) {
      const theirI = byId.get(mine.id)
      expect(
        theirI,
        `the app reported invoice ${mine.id}, Stripe does not`,
      ).toBeTruthy()
      const it = theirI as Record<string, any>
      expect(mine.amount_paid, `${mine.id} amount_paid`).toBe(it.amount_paid)
      expect(mine.amount_due, `${mine.id} amount_due`).toBe(it.amount_due)
      expect(mine.currency.toLowerCase(), `${mine.id} currency`).toBe(
        it.currency,
      )
      expect(mine.status.toLowerCase(), `${mine.id} status`).toBe(it.status)
      // "$22.00" and "\u00a52000" both reduce to Stripe's minor-unit total: a
      // two-decimal currency drops its point, and a zero-decimal one has none
      expect(mine.total.replace(/\D/g, ""), `${mine.id} formatted total`).toBe(
        String(it.total),
      )
      expect(
        new Date(mine.date).toISOString().slice(0, 10),
        `${mine.id} date`,
      ).toBe(new Date(it.created * 1000).toISOString().slice(0, 10))
      if (mine.period_start) {
        periodsCompared += 1
        expect(
          new Date(mine.period_start).toISOString().slice(0, 10),
          `${mine.id} period_start`,
        ).toBe(new Date(it.period_start * 1000).toISOString().slice(0, 10))
      }
    }
    // A serialiser that drops period_start would otherwise make the comparison
    // above disappear rather than fail.
    expect(
      periodsCompared,
      "no invoice carried period_start - the field stopped being serialised",
    ).toBeGreaterThan(0)

    // The links, on the newest invoice: the hosted page the app links to, and
    // the PDF Stripe generates for it. Host-checked before fetching, so a
    // redirect somewhere unexpected is a failure rather than a request we make.
    const newest = theirs.sort((a, b) => b.created - a.created)[0]
    const mine = ours.find((i) => i.id === newest.id)
    expect(
      mine,
      "the newest Stripe invoice is missing from the app's list",
    ).toBeTruthy()
    const pages = [
      (mine as Record<string, any>).invoice_url as string,
      newest.hosted_invoice_url as string,
    ].filter(Boolean)
    const links = [...pages, newest.invoice_pdf as string].filter(Boolean)
    expect(
      pages.length,
      "the invoice carries no hosted page to open",
    ).toBeGreaterThan(0)
    expect(newest.invoice_pdf, "the invoice carries no PDF link").toBeTruthy()

    const anon = await request.newContext()
    try {
      for (const link of links) {
        expect(
          isStripeHost(new URL(link).hostname),
          `${link} is a Stripe URL`,
        ).toBe(true)
      }
      // The pages the app links to: these are Stripe-hosted HTML and answer
      // directly.
      for (const page of pages) {
        const got = await anon.get(page, { failOnStatusCode: false })
        expect(got.status(), `GET ${page.split("?")[0]}`).toBe(200)
      }
      // The PDF is served by a 302 to Stripe's own file store on S3, with a
      // presigned URL. The redirect is the assertion: following it downloads a
      // binary from a host outside stripe.com, which is both beside the point
      // and where this reliably ECONNRESETs.
      const pdf = await anon.get(newest.invoice_pdf as string, {
        maxRedirects: 0,
        failOnStatusCode: false,
      })
      expect(pdf.status(), "the invoice PDF endpoint redirects").toBe(302)
      const location = pdf.headers()["location"] || ""
      expect(
        /^https:\/\/[a-z0-9.-]*stripe[a-z0-9.-]*\.(s3\.[a-z0-9-]+\.)?amazonaws\.com\//.test(
          location,
        ) || isStripeHost(new URL(location || "https://x.invalid").hostname),
        `the PDF redirect goes to a Stripe-owned file store, not ${location.split("?")[0]}`,
      ).toBe(true)
    } finally {
      await anon.dispose()
    }
  })
})

// Rows 118 / 228: the mirror image of AUDIT-04. A free account must have no
// active subscription in Stripe - either no customer at all, or one carrying
// nothing live. This is what catches a webhook that credited the wrong
// customer, which from our own database's point of view looks like nothing
// happened.
test.describe("A free account has nothing live in Stripe", () => {
  test("AUDIT-10 - the free test account holds no active Stripe subscription", async () => {
    test.skip(
      isLocalBaseUrl(),
      "rows 118 / 228: reads the deployed environment's Stripe account; BASE_URL is local",
    )
    skipWithoutCreds()
    const liveKey = liveStripeSkipReason()
    test.skip(!!liveKey, `rows 118 / 228: ${liveKey}`)

    const customers = stripeGet("/v1/customers", {
      email: FREE_USER.email,
      limit: 10,
    }).data as Record<string, any>[]

    // Without this the loop below never runs and the row passes by finding
    // nothing - which is also what reading the wrong Stripe account looks like.
    expect(
      customers.length,
      `${FREE_USER.email} owns no Stripe customer - wrong account, or the ` +
        `address was edited in Stripe`,
    ).toBeGreaterThan(0)

    const live: string[] = []
    for (const customer of customers) {
      const subs = stripeGet("/v1/subscriptions", {
        customer: customer.id,
        status: "all",
        limit: 10,
      }).data as Record<string, any>[]
      live.push(
        ...subs
          .filter((sub) =>
            ["active", "trialing", "past_due"].includes(sub.status),
          )
          .map((sub) => `${customer.id}/${sub.id} (${sub.status})`),
      )
    }
    expect(
      live,
      `${FREE_USER.email} is a free account and must own no live subscription`,
    ).toEqual([])
  })
})
