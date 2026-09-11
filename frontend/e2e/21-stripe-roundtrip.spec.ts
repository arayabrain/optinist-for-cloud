import { test, expect } from "@playwright/test"

import {
  STRIPE_USER,
  apiLogin,
  isLocalBaseUrl,
  login,
  runSql,
  stripeAccountSkipReason,
  sqlLiteral,
  sqlSkipReason,
  stripeGet,
  stripeSubscriptionFor,
} from "./helpers"

// The reversible half of the subscription lifecycle, against the real Stripe
// test account: cancel at period end, then undo it. Every other subscription
// spec mocks this, which proves our gesture but not that Stripe recorded it -
// and the rows this closes are specifically about what Stripe holds afterwards.
//
//   RUN_STRIPE_WRITE=1 npx playwright test e2e/21-stripe-roundtrip.spec.ts --retries 0
//
// Opt-in because it writes: it flips the premium test account's
// cancel_at_period_end and back. Nothing else may be reading that account while
// it runs - a scheduled cancellation changes what the subscription page renders,
// so SUB-04/05 would see the cancelled banner mid-flight. The undo is in a
// finally block and is asserted, because leaving the shared account cancelled
// is the one outcome worse than the test failing.
//
// Rows: 267 / 268 (cancel_at_period_end and the cancel timestamps), 269 / 274
// (the customer.subscription.updated events), 273 (the reactivation flips it
// back), BT-919 / BT-921; STRIPE-02 adds 2021 / BT-915 / BT-916 / BT-920, the
// UI legs on real Stripe state.

const premiumUserId = `(SELECT id FROM users WHERE email = '${sqlLiteral(
  STRIPE_USER.email || "",
)}')`

function scheduledDowngrade(): string {
  return runSql(
    `SELECT scheduled_downgrade FROM subscription_users
       WHERE user_id = ${premiumUserId};`,
  )
}

// The end of the current billing period. Newer Stripe API versions carry this
// on the subscription item, not on the subscription, so reading it off the top
// level silently yields undefined.
function periodEnd(subscription: Record<string, any>): number {
  const items = (subscription.items?.data ?? []) as Record<string, any>[]
  const ends = items
    .map((item) => item.current_period_end as number)
    .filter((end) => typeof end === "number")
  expect(
    ends.length,
    "the subscription has no item with a period end",
  ).toBeGreaterThan(0)
  return Math.max(...ends)
}

// Update events for this subscription raised at or after `since` (epoch
// seconds). Scoped by time rather than counted inside a fixed page: the
// environment bills daily across several accounts, so a new event of ours can
// push an older one of ours out of any fixed window and leave a count
// unchanged.
function updateEventsSince(
  subscriptionId: string,
  since: number,
): Record<string, any>[] {
  const events = stripeGet("/v1/events", {
    type: "customer.subscription.updated",
    "created[gte]": since,
    limit: 100,
  }).data as Record<string, any>[]
  return events.filter((e) => e.data?.object?.id === subscriptionId)
}

test.describe("Stripe cancel / reactivate round-trip", () => {
  test.skip(
    isLocalBaseUrl(),
    "reads and writes the deployed environment's Stripe account; BASE_URL is local",
  )
  test.skip(
    !process.env.RUN_STRIPE_WRITE,
    "writes to a real Stripe subscription; opt in with RUN_STRIPE_WRITE=1",
  )
  // Cancelling runs through the deployed backend with that environment's own
  // Stripe key, which the repo's sk_live refusal does not cover.
  test.beforeAll(() => {
    expect(
      process.env.BASE_URL || "",
      "this lane only runs against the development environment",
    ).toContain("development-optinist")
  })

  test("STRIPE-01 - Cancelling schedules it in Stripe and reactivating clears it", async () => {
    const accountReason = stripeAccountSkipReason()
    test.skip(!!accountReason, accountReason)
    const sqlReason = sqlSkipReason()
    expect(
      sqlReason,
      `the deployed database must be readable: ${sqlReason}`,
    ).toBe("")
    test.setTimeout(180_000)

    const before = stripeSubscriptionFor(STRIPE_USER.email)
    // A subscription already scheduled for cancellation makes every assertion
    // below vacuous, and the undo would not be an undo.
    expect(
      before.cancel_at_period_end,
      "the account must start with no scheduled cancellation",
    ).toBe(false)
    expect(
      scheduledDowngrade(),
      "the database agrees it is not cancelled",
    ).toBe("0")
    // A second early, so an event raised in the same second still counts
    const cancelFrom = Math.floor(Date.now() / 1000) - 1

    const { api, headers } = await apiLogin(
      STRIPE_USER.email,
      STRIPE_USER.password,
    )
    const me = await api.get("/users/me", { headers })
    expect(me.ok()).toBeTruthy()
    const userId = (await me.json()).id

    try {
      const cancel = await api.delete("/api/subsc/mgmts/cancel", { headers })
      expect(cancel.ok(), `DELETE cancel: ${await cancel.text()}`).toBeTruthy()

      // Stripe's own record, not ours: the row is about what Stripe holds
      const cancelled = stripeSubscriptionFor(STRIPE_USER.email)
      expect(cancelled.id, "the same subscription, not a new one").toBe(
        before.id,
      )
      expect(
        cancelled.cancel_at_period_end,
        "Stripe has the cancellation scheduled",
      ).toBe(true)
      // cancel_at is when access ends; canceled_at is when the request landed
      expect(cancelled.cancel_at, "cancel_at is set").toBeTruthy()
      expect(cancelled.canceled_at, "canceled_at is set").toBeTruthy()
      // Stripe moved current_period_end off the subscription and onto its
      // items, so the period end is read from the item rather than the top
      // level, where it is simply absent.
      expect(
        cancelled.cancel_at,
        "access ends at the period end, not immediately",
      ).toBe(periodEnd(cancelled))
      expect(
        cancelled.status,
        "the subscription is still active until the period ends",
      ).toBe("active")

      // Our side agrees, and the plan is not rewritten to Free
      expect(scheduledDowngrade(), "the database recorded the schedule").toBe(
        "1",
      )
      expect(
        runSql(`SELECT plan_id FROM subscription_users
                  WHERE user_id = ${premiumUserId};`),
        "a scheduled cancellation must not downgrade the row",
      ).toBe("2")

      await expect
        .poll(() => updateEventsSince(before.id, cancelFrom).length, {
          timeout: 60_000,
          intervals: [5_000],
          message:
            "Stripe logged no customer.subscription.updated event for this " +
            "subscription after the cancel",
        })
        .toBeGreaterThan(0)
    } finally {
      const undo = await api.post(`/api/subsc/mgmts/reactivate/${userId}`, {
        headers,
      })
      // Asserted, not best-effort: a silent failure here leaves the shared
      // premium account scheduled for cancellation.
      expect(undo.ok(), `POST reactivate: ${await undo.text()}`).toBeTruthy()
      await api.dispose()
    }

    const restored = stripeSubscriptionFor(STRIPE_USER.email)
    expect(
      restored.cancel_at_period_end,
      "reactivating cleared the schedule in Stripe",
    ).toBe(false)
    expect(restored.cancel_at, "cancel_at was cleared").toBeNull()
    expect(scheduledDowngrade(), "and in the database").toBe("0")
  })

  // The subscription pages on the REAL account: what the UI renders must be
  // Stripe's own values, and the cancelled state's Continue Plan button must
  // really clear the schedule. Rows 2021 (the card last4 leg), BT-915 (the
  // sections on real data), BT-916 (a real invoice row), BT-920 (the button,
  // clicked). SUB-08..11 assert the same rendering against mocks; what this
  // adds is that the mocks tell the truth.
  test("STRIPE-02 - The manage page shows Stripe's own values and Continue Plan really reactivates", async ({
    page,
  }) => {
    const accountReason = stripeAccountSkipReason()
    test.skip(!!accountReason, accountReason)
    const sqlReason = sqlSkipReason()
    expect(
      sqlReason,
      `the deployed database must be readable: ${sqlReason}`,
    ).toBe("")
    test.setTimeout(300_000)

    const before = stripeSubscriptionFor(STRIPE_USER.email)
    expect(
      before.cancel_at_period_end,
      "the account must start with no scheduled cancellation",
    ).toBe(false)
    expect(
      scheduledDowngrade(),
      "the database agrees it is not cancelled",
    ).toBe("0")

    // Stripe's side of every claim the UI is about to make, fetched first
    const customer = before.customer as string
    const detail = stripeGet(`/v1/customers/${customer}`) as Record<string, any>
    // Every type, not just cards: this account's default is a Stripe Link
    // method, which owns no brand or last4 at all. Filtering to cards made
    // `find` miss the default, fall back to an older non-default Visa, and
    // demand a card the page was right not to be showing.
    const pms = stripeGet("/v1/payment_methods", {
      customer,
      limit: 20,
    }).data as Record<string, any>[]
    const defaultPm = detail.invoice_settings?.default_payment_method as string
    const method = pms.find((p) => p.id === defaultPm) ?? pms[0]
    expect(method, "the account has no payment method on file").toBeTruthy()
    // What the manage page renders for that method, per its own branch: Link
    // has no digits to show, a card shows brand plus last4. The last4 is the
    // identity row 2021 is really about, so it is what gets asserted - the
    // brand's display name ("amex" renders as "American Express") is the
    // page's own mapping and asserting it here would only restate the code.
    const rendered =
      method.type === "link" ? "Stripe Link" : `••••••${method.card?.last4}`
    expect(
      method.type === "link" || method.card?.last4,
      `Stripe's default payment method ${method.id} is a ${method.type} with ` +
        `no last4 to render`,
    ).toBeTruthy()
    const invoices = stripeGet("/v1/invoices", { customer, limit: 10 })
      .data as Record<string, any>[]
    const newest = invoices.filter((i) => i.status === "paid")[0]
    expect(newest, "no paid invoice to compare the UI against").toBeTruthy()

    await login(page, STRIPE_USER.email, STRIPE_USER.password)

    // BT-915, by its own action: Manage from the account profile
    await page.goto("/account")
    await page.locator('button:has-text("Manage")').click()
    await expect(page).toHaveURL(/\/subscription\/manage$/, { timeout: 15_000 })

    // The sections, on real data: plan, payment method, invoice list
    await expect(
      page.getByText("Premium", { exact: true }).first(),
    ).toBeVisible({ timeout: 30_000 })
    await expect(
      page.getByText(rendered).first(),
      `the rendered payment method is Stripe's own default ${method.id} ` +
        `(${method.type}), shown as "${rendered}"`,
    ).toBeVisible({ timeout: 30_000 })

    // BT-916: the newest paid invoice's own values in the top table row -
    // its date as the page formats it, its status, and its total's digits
    const row = page.locator("tbody tr").first()
    await expect(row).toBeVisible({ timeout: 30_000 })
    // Formatted in the browser's own timezone, not the host's. The backend
    // sends a UTC-aware ISO string and the page renders it wherever the
    // browser lives, which the config pins to UTC - formatting here in the
    // machine's zone instead expected an "August 25" that only a JST Node
    // process ever saw, for an invoice stamped 23:32 UTC on the 24th.
    await expect(row).toContainText(
      new Date(newest.created * 1000).toLocaleDateString("en-US", {
        year: "numeric",
        month: "long",
        day: "numeric",
        timeZone: test.info().project.use.timezoneId ?? "UTC",
      }),
    )
    await expect(row).toContainText(/paid/i)
    // The amount exactly as the backend formats it (cents to dollars, two
    // decimals). Stripping the row down to its digits instead would let an
    // unrelated number in the date satisfy it: the digits of "August 25, 2026
    // $22.00" run together as ...2026 2200..., which contains "2200" whatever
    // the amount actually is.
    await expect(row).toContainText(`$${(newest.total / 100).toFixed(2)}`)

    // BT-920: cancel for real, then the banner's own button undoes it
    const { api, headers } = await apiLogin(
      STRIPE_USER.email,
      STRIPE_USER.password,
    )
    let reactivated = false
    try {
      const me = await api.get("/users/me", { headers })
      expect(me.ok()).toBeTruthy()
      const userId = (await me.json()).id

      const cancel = await api.delete("/api/subsc/mgmts/cancel", { headers })
      expect(cancel.ok(), `DELETE cancel: ${await cancel.text()}`).toBeTruthy()
      expect(
        stripeSubscriptionFor(STRIPE_USER.email).cancel_at_period_end,
        "Stripe has the cancellation scheduled",
      ).toBe(true)

      try {
        await page.goto("/subscription")
        await expect(
          page.locator("text=Subscription Canceled:").first(),
        ).toBeVisible({ timeout: 30_000 })
        const continueButton = page
          .locator('button:has-text("Continue Plan")')
          .first()
        await expect(continueButton).toBeVisible()
        const undone = page.waitForResponse(
          (r) =>
            r.url().includes("/api/subsc/mgmts/reactivate/") &&
            r.request().method() === "POST",
          { timeout: 30_000 },
        )
        await continueButton.click()
        expect(
          (await undone).status(),
          "the reactivate call Continue Plan fired",
        ).toBe(200)
        reactivated = true
      } finally {
        // The click is the undo; this is the fallback for a failure before it
        if (!reactivated) {
          const undo = await api.post(`/api/subsc/mgmts/reactivate/${userId}`, {
            headers,
          })
          expect(undo.ok(), await undo.text()).toBeTruthy()
        }
      }
    } finally {
      await api.dispose()
    }

    // The click really cleared it, on both sides
    const restored = stripeSubscriptionFor(STRIPE_USER.email)
    expect(
      restored.cancel_at_period_end,
      "Continue Plan cleared the schedule in Stripe",
    ).toBe(false)
    expect(scheduledDowngrade(), "and in the database").toBe("0")
  })
})
