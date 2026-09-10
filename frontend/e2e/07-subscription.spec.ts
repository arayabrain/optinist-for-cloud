import { test, expect, Page } from "@playwright/test"

import {
  apiLogin,
  ERROR_RED,
  FREE_USER,
  freeStorageState,
  gotoDashboard,
  login,
  PREMIUM_USER,
  routeGate,
  skipWithoutCreds,
} from "./helpers"

// Subscription UI state for free and premium users.
// DB/Stripe dashboard verification stays manual.

// The features JSON is deployment configuration (the local DB seeds it empty),
// so the catalogue is mocked in the shape the tfvars declare. Two strings are
// on both plans, which is what makes the per-plan counts below a real check.

const SHARED_FEATURES = [
  "Basic compute access with fair-use limitations",
  "Standard support through documentation and community",
]
const FREE_ONLY_FEATURES = [
  "Basic data storage of 5GB",
  "Standard processing speed",
]
const PREMIUM_ONLY_FEATURES = [
  "Priority compute access with guaranteed allocation",
  "Upgraded data storage of 200GB",
  "Enhanced support including direct assistance",
  "Advanced features like extended job history",
]

async function mockPlanCatalogue(page: Page) {
  const plan = (id: number, name: string, price: number, texts: string[]) => ({
    id,
    name,
    price,
    billing_cycle: 1,
    currency: 1,
    status: true,
    features: {
      [name]: texts.map((text) => ({
        text,
        isPremium: PREMIUM_ONLY_FEATURES.includes(text),
      })),
    },
  })
  await page.route("**/api/subsc/mgmts/plans", (route) =>
    route.fulfill({
      json: [
        plan(1, "Free", 0, [...SHARED_FEATURES, ...FREE_ONLY_FEATURES]),
        plan(2, "Premium", 2000, [
          ...SHARED_FEATURES,
          ...PREMIUM_ONLY_FEATURES,
        ]),
      ],
    }),
  )
}

test.describe("Free plan state", () => {
  test.use({ storageState: freeStorageState() })

  test.beforeEach(async ({ page }) => {
    skipWithoutCreds()
    await gotoDashboard(page)
  })

  test("SUB-01 - Subscription page shows Free as current plan", async ({
    page,
  }) => {
    await page.goto("/subscription")

    const free = page.getByTestId("plan-card-Free")
    const premium = page.getByTestId("plan-card-Premium")
    await expect(free).toBeVisible({ timeout: 30_000 })
    await expect(premium).toBeVisible()

    // Scoped to its own card. Page-wide, a $20 rendered anywhere - including on
    // the Free card - satisfied this row, and so did an Upgrade button sitting
    // on the wrong plan.
    await expect(free.locator('button:has-text("Current Plan")')).toBeVisible()
    await expect(free.locator('button:has-text("Upgrade")')).toHaveCount(0)
    await expect(premium.locator('button:has-text("Upgrade")')).toBeVisible()
    await expect(
      premium.locator('button:has-text("Current Plan")'),
    ).toHaveCount(0)

    // The billing cycle differs by environment (dev bills daily), so the
    // assertion is the amount and its tax caption, not the period word.
    await expect(premium).toContainText("$20")
    await expect(premium).toContainText("+ applicable taxes")
    await expect(free).not.toContainText("+ applicable taxes")

    // Row 121: the only action offered to a free user has to be usable.
    const upgrade = premium.locator('button:has-text("Upgrade")')
    await expect(upgrade).toBeEnabled()
    await expect(free.locator('button:has-text("Current Plan")')).toBeDisabled()
  })

  test("SUB-02 - Account profile shows Free status", async ({ page }) => {
    await page.goto("/account")
    await expect(page.locator('h2:has-text("Account Profile")')).toBeVisible({
      timeout: 15_000,
    })

    // The status field itself, read exactly: "text=Free" matched the word
    // anywhere on the page, so a Premium account would have satisfied it too.
    await expect(page.getByTestId("account-plan-name")).toHaveText("Free")
    // A free account has no billing period, so it must carry no expiry caption.
    // Renew is included deliberately: it is the branch a free account wrongly
    // marked SUBSCRIBED would actually render, and the other two never fire for
    // one, so leaving it out made this a 0 == 0 by construction.
    await expect(page.getByText(/\((Expires|Expired|Renew) on /)).toHaveCount(0)
    await expect(page.locator('button:has-text("Upgrade")')).toBeVisible()
    await expect(page.locator('button:has-text("Manage")')).toBeHidden()
  })

  test("SUB-03 - Invoice page shows no invoices for free user", async ({
    page,
  }) => {
    await page.goto("/subscription/manage")

    await expect(page.locator("text=Free Plan").first()).toBeVisible({
      timeout: 30_000,
    })
    await expect(page.locator("text=No Invoices Found").first()).toBeVisible()
  })

  test("SUB-15 - Each plan card lists its own features", async ({ page }) => {
    await mockPlanCatalogue(page)
    await page.goto("/subscription")

    // One occurrence per card for the shared strings, one for the plan-specific
    // ones: a card that dropped its list, or rendered the other plan's, fails
    for (const text of SHARED_FEATURES) {
      await expect(page.getByText(text, { exact: true })).toHaveCount(2, {
        timeout: 30_000,
      })
    }
    for (const text of [...FREE_ONLY_FEATURES, ...PREMIUM_ONLY_FEATURES]) {
      await expect(page.getByText(text, { exact: true })).toHaveCount(1)
    }

    // The Premium card's action is the only Upgrade on the page and it has to
    // be usable; the Free card is the signed-in user's current plan
    const upgrade = page.locator('button:has-text("Upgrade")')
    await expect(upgrade).toHaveCount(1)
    await expect(upgrade).toBeEnabled()
    await expect(page.locator('button:has-text("Current Plan")')).toBeDisabled()
  })

  test("SUB-16 - Subscription and account pages fit every viewport width", async ({
    page,
  }) => {
    // The machine-checkable half of the responsive row: the page still renders
    // its own landmark, and nothing spills sideways. Overlap and legibility
    // stay a human read.
    const pages: [string, string][] = [
      // Not the "Current Plan:" status line - that renders for paid plans only,
      // so a free user never has it at any width
      ["/subscription", 'h3:has-text("Subscription Plans")'],
      ["/account", 'h2:has-text("Account Profile")'],
      ["/subscription/manage", "text=Free Plan"],
    ]
    const viewports = [
      { width: 375, height: 812 },
      { width: 768, height: 1024 },
      { width: 1280, height: 800 },
    ]

    for (const viewport of viewports) {
      await page.setViewportSize(viewport)
      for (const [url, landmark] of pages) {
        await page.goto(url)
        await expect(page.locator(landmark).first()).toBeVisible({
          timeout: 30_000,
        })
        // Sub-pixel layout rounding makes an exact comparison flaky
        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - window.innerWidth,
        )
        expect(
          overflow,
          `${url} at ${viewport.width}px overflows by ${overflow}px`,
        ).toBeLessThanOrEqual(1)
      }

      // Row 298's overlap half. A scan of every element would be noise - a
      // child always overlaps its parent - so the assertion is the pair that
      // actually collides when the flex row refuses to wrap: the plan cards.
      await page.goto("/subscription")
      const cards = page.getByTestId(/^plan-card-/)
      await expect(cards.first()).toBeVisible({ timeout: 30_000 })
      const boxes = []
      for (const card of await cards.all()) {
        const box = await card.boundingBox()
        if (box) boxes.push(box)
      }
      expect(boxes.length, "plan cards found").toBeGreaterThan(1)
      // Overlap was the original check, but the cards are flex siblings with a
      // gap and nothing short of absolute positioning makes those overlap. What
      // a row that refuses to wrap really does is push a card off-screen.
      for (const [i, box] of boxes.entries()) {
        expect(
          box.x >= -1 && box.x + box.width <= viewport.width + 1,
          `plan card ${i} is outside the ${viewport.width}px viewport ` +
            `(x=${box.x}, width=${box.width})`,
        ).toBe(true)
        expect(box.width, `plan card ${i} collapsed`).toBeGreaterThan(80)
      }
    }
  })

  test("SUB-06 - Payment success page is guarded without a checkout session", async ({
    page,
  }) => {
    // The real success page is /subscription/thanks (reached from Stripe with
    // ?session_id=...). Opening it directly with no session must be blocked:
    // validateSession redirects to /subscription.
    await page.goto("/subscription/thanks")
    await expect(page).toHaveURL(/\/subscription$/, { timeout: 15_000 })
    await expect(page).not.toHaveURL(/thanks/)
  })
})

// There is no Stripe locally, so our own /api/subsc/* responses are mocked and
// the assertions stay on what we render and what we send. The server-side writes
// behind these transitions are asserted separately in pytest.
const PREMIUM_SUBSCRIPTION = {
  id: 12,
  plan_id: 2,
  user_id: 1,
  expiration: "2027-06-07T12:34:56",
  is_expired: false,
  scheduled_downgrade: false,
  status: 2,
  plan_name: "Premium",
  plan_price: 20,
}

const PAID_INVOICE = {
  id: "in_e2e_1",
  date: "2026-05-07T00:00:00Z",
  total: "$20.00",
  status: "Paid",
  invoice_url: "https://invoice.stripe.com/i/e2e-hosted-invoice",
}

// A second, deliberately different row: with one fixture the assertions were
// satisfied by any hardcoded "$20.00 / Paid" render
const OPEN_INVOICE = {
  id: "in_e2e_2",
  date: "2026-06-08T00:00:00Z",
  total: "$99.00",
  status: "Open",
  invoice_url: "https://invoice.stripe.com/i/e2e-hosted-invoice-2",
}

async function mockPremiumBilling(
  page: Page,
  overrides: Partial<typeof PREMIUM_SUBSCRIPTION> = {},
) {
  const subscription = { ...PREMIUM_SUBSCRIPTION, ...overrides }
  await page.route("**/api/subsc/mgmts", (route) =>
    route.fulfill({ json: subscription }),
  )
  await page.route("**/api/subsc/payment-methods/default", (route) =>
    route.fulfill({
      json: {
        id: "pm_e2e",
        type: "card",
        brand: "visa",
        last4: "4242",
        exp_month: 12,
        exp_year: 2030,
        is_default: true,
      },
    }),
  )
  await page.route("**/api/subsc/invoices/**", (route) =>
    route.fulfill({ json: [PAID_INVOICE, OPEN_INVOICE] }),
  )
  // Cancel and reactivate both mutate Stripe. Mocking the round trip is enough
  // for the UI: the slice flips scheduled_downgrade on fulfillment and the page
  // never re-reads /mgmts.
  await page.route("**/api/subsc/mgmts/cancel", (route) =>
    route.fulfill({ json: { success: true } }),
  )
  await page.route("**/api/subsc/mgmts/reactivate/**", (route) =>
    route.fulfill({ json: { success: true } }),
  )
}

test.describe("Invoice page and subscription transitions (mocked billing)", () => {
  test.use({ storageState: freeStorageState() })

  test.beforeEach(async ({ page }) => {
    skipWithoutCreds()
    await gotoDashboard(page)
  })

  test("SUB-07 - Manage on the account profile opens the invoice page", async ({
    page,
  }) => {
    await mockPremiumBilling(page)
    await page.goto("/account")
    await expect(page.locator('h2:has-text("Account Profile")')).toBeVisible({
      timeout: 15_000,
    })

    await page.locator('button:has-text("Manage")').click()
    await expect(page).toHaveURL(/\/subscription\/manage$/, { timeout: 15_000 })
    await expect(page.locator("text=Payment Method").first()).toBeVisible({
      timeout: 30_000,
    })
  })

  test("SUB-08 - Invoice page renders every section", async ({ page }) => {
    await mockPremiumBilling(page)
    await page.goto("/subscription/manage")

    // Subscription info: the plan name and the renewal wording, which is what
    // distinguishes an active subscription from a cancelled or expired one
    await expect(
      page.getByText(PREMIUM_SUBSCRIPTION.plan_name, { exact: true }).first(),
    ).toBeVisible({ timeout: 30_000 })
    await expect(
      page.locator("text=/Your subscription will renew on/"),
    ).toBeVisible()

    // Payment method: the brand and last four come from the response
    await expect(page.locator("text=/Visa .*4242/")).toBeVisible()
    await expect(
      page.locator('button:has-text("Manage Billing")'),
    ).toBeVisible()
  })

  test("SUB-09 - Each invoice row shows its own date, amount and status", async ({
    page,
  }) => {
    await mockPremiumBilling(page)
    await page.goto("/subscription/manage")

    const rows = page.locator("tbody tr")
    await expect(rows).toHaveCount(2, { timeout: 30_000 })
    // The page renders the ISO date in the reader's locale, not raw, and each
    // row carries its own values rather than the first row's
    await expect(rows.nth(0)).toContainText("May 7, 2026")
    await expect(rows.nth(0)).toContainText(PAID_INVOICE.total)
    await expect(rows.nth(0)).toContainText(PAID_INVOICE.status)
    await expect(rows.nth(1)).toContainText("June 8, 2026")
    await expect(rows.nth(1)).toContainText(OPEN_INVOICE.total)
    await expect(rows.nth(1)).toContainText(OPEN_INVOICE.status)
  })

  test("SUB-10 - View opens the hosted invoice", async ({ page }) => {
    await mockPremiumBilling(page)
    // Stub the destination so a run never actually reaches Stripe
    await page.route("https://invoice.stripe.com/**", (route) =>
      route.fulfill({ contentType: "text/html", body: "<h1>invoice</h1>" }),
    )
    await page.goto("/subscription/manage")
    await expect(page.locator("tbody tr").first()).toBeVisible({
      timeout: 30_000,
    })

    // The detail view is Stripe's hosted invoice, opened in a new tab; assert
    // the target and stop there rather than driving Stripe's page
    const popup = page.waitForEvent("popup")
    await page.locator('button:has-text("View")').first().click()
    expect((await popup).url()).toBe(PAID_INVOICE.invoice_url)
  })

  test("SUB-11 - Confirming cancellation shows the cancelled banner", async ({
    page,
  }) => {
    await mockPremiumBilling(page)
    await page.goto("/subscription")
    await expect(
      page.locator('button:has-text("Downgrade")').first(),
    ).toBeVisible({ timeout: 30_000 })

    await page.locator('button:has-text("Downgrade")').first().click()
    const confirm = page.locator(
      '[role="dialog"]:has-text("Cancel Subscription")',
    )
    await expect(confirm).toBeVisible({ timeout: 15_000 })
    // The date access actually ends, taken from the subscription rather than
    // written into the test
    const endsOn = new Date(PREMIUM_SUBSCRIPTION.expiration).toLocaleDateString(
      "en-US",
    )
    await expect(
      confirm.getByText(`Your subscription will be canceled at ${endsOn}.`),
    ).toBeVisible()
    // Two choices, and the destructive one is the one that looks destructive: a
    // safe-looking confirm is how a cancellation gets clicked by accident.
    const yes = confirm.getByRole("button", {
      name: "Yes, Cancel Subscription",
    })
    const no = confirm.getByRole("button", { name: "No" })
    await expect(confirm.getByRole("button")).toHaveCount(2)
    await expect(yes).toBeEnabled()
    await expect(no).toBeEnabled()
    await expect(yes).toHaveCSS("background-color", ERROR_RED)

    const cancelRequest = page.waitForRequest(
      (r) =>
        r.url().includes("/api/subsc/mgmts/cancel") && r.method() === "DELETE",
    )
    await yes.click()
    await cancelRequest

    await expect(confirm).toBeHidden({ timeout: 15_000 })
    const banner = page.locator("text=Subscription Canceled:").first()
    await expect(banner).toBeVisible({ timeout: 15_000 })
    // The banner has to name the date access actually ends
    await expect(page.locator("text=/will remain active until/")).toBeVisible()
    await expect(
      page.locator('button:has-text("Continue Plan")').first(),
    ).toBeVisible()
  })

  test("SUB-12 - Continue Plan clears the cancellation", async ({ page }) => {
    await mockPremiumBilling(page, { scheduled_downgrade: true })
    await page.goto("/subscription")
    const banner = page.locator("text=Subscription Canceled:").first()
    await expect(banner).toBeVisible({ timeout: 30_000 })

    // A scheduled downgrade does not take premium access away yet: the status
    // line still names Premium and the only offer is reactivation
    await expect(page.getByText(/^Current Plan:/).first()).toContainText(
      "Premium",
    )
    await expect(page.locator('button:has-text("Upgrade")')).toHaveCount(0)

    // The route is per-user and refuses an id that is not the caller's, which is
    // asserted server-side; here the point is that we POST to it at all
    const reactivateRequest = page.waitForRequest(
      (r) =>
        r.method() === "POST" &&
        /\/api\/subsc\/mgmts\/reactivate\/\d+$/.test(r.url()),
    )
    await page.locator('button:has-text("Continue Plan")').first().click()
    await reactivateRequest

    await expect(banner).toBeHidden({ timeout: 15_000 })
    await expect(
      page.locator('button:has-text("Current Plan")').first(),
    ).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('button:has-text("Continue Plan")')).toBeHidden()
  })

  test("SUB-13 - Upgrade creates a checkout session and leaves for Stripe", async ({
    page,
  }) => {
    // Stripe's own checkout is out of scope (its UI, not ours), so the
    // assertion is our request and the redirect target. The destination is
    // stubbed so the run never actually reaches Stripe.
    const checkoutUrl = "https://checkout.stripe.com/c/pay/e2e-session"
    await page.route("**/api/subsc/checkout/create-checkout-session", (route) =>
      route.fulfill({ json: { checkout_url: checkoutUrl } }),
    )
    await page.route("https://checkout.stripe.com/**", (route) =>
      route.fulfill({ contentType: "text/html", body: "<h1>stripe</h1>" }),
    )

    await page.goto("/subscription")
    const upgrade = page.locator('button:has-text("Upgrade")').first()
    await expect(upgrade).toBeVisible({ timeout: 30_000 })

    const sessionRequest = page.waitForRequest(
      (r) =>
        r.url().includes("/api/subsc/checkout/create-checkout-session") &&
        r.method() === "POST",
    )
    await upgrade.click()
    await sessionRequest
    await expect(page).toHaveURL(checkoutUrl, { timeout: 30_000 })
  })

  test("SUB-14 - Manage Billing opens the Stripe customer portal", async ({
    page,
  }) => {
    await mockPremiumBilling(page)
    await page.route("https://billing.stripe.com/**", (route) =>
      route.fulfill({ contentType: "text/html", body: "<h1>portal</h1>" }),
    )
    await page.goto("/subscription/manage")
    const manageBilling = page.locator('button:has-text("Manage Billing")')
    await expect(manageBilling).toBeVisible({ timeout: 30_000 })

    // The button opens Stripe's hosted login link directly - no portal session
    // is created, so the assertion is the destination host
    const popup = page.waitForEvent("popup")
    await manageBilling.click()
    expect((await popup).url()).toContain("billing.stripe.com")
  })

  test("SUB-17 - Browser Back out of checkout returns to a working page", async ({
    page,
  }) => {
    // The no-charge and no-webhook halves stay with
    // test_subscription_state_transitions.py; what the browser can prove is
    // the gesture: Back lands on /subscription with the plan unchanged, and a
    // retry mints a fresh session, not a reused one
    let sessions = 0
    await page.route(
      "**/api/subsc/checkout/create-checkout-session",
      (route) => {
        sessions++
        route.fulfill({
          json: {
            checkout_url: `https://checkout.stripe.com/c/pay/e2e-${sessions}`,
          },
        })
      },
    )
    await page.route("https://checkout.stripe.com/**", (route) =>
      route.fulfill({ contentType: "text/html", body: "<h1>stripe</h1>" }),
    )

    await page.goto("/subscription")
    const upgrade = page.locator('button:has-text("Upgrade")').first()
    await expect(upgrade).toBeVisible({ timeout: 30_000 })
    await upgrade.click()
    await expect(page).toHaveURL(/checkout\.stripe\.com\/c\/pay\/e2e-1/, {
      timeout: 30_000,
    })

    await page.goBack()
    await expect(page).toHaveURL(/\/subscription/, { timeout: 30_000 })
    // Still on the Free plan: the Free card reads Current Plan, the Premium
    // card still offers Upgrade, and clicking it creates a second session
    // and leaves again without an error
    await expect(
      page.locator('button:has-text("Current Plan")').first(),
    ).toBeVisible({ timeout: 30_000 })
    await expect(upgrade).toBeVisible({ timeout: 30_000 })
    await upgrade.click()
    await expect(page).toHaveURL(/checkout\.stripe\.com\/c\/pay\/e2e-2/, {
      timeout: 30_000,
    })
  })

  test("SUB-18 - A click storm on Upgrade creates one checkout session", async ({
    page,
  }) => {
    let sessions = 0
    const checkout = routeGate()
    await page.route(
      "**/api/subsc/checkout/create-checkout-session",
      async (route) => {
        sessions++
        // Held open so every later click lands while the first is in flight
        await checkout.held
        await route.fulfill({
          json: { checkout_url: "https://checkout.stripe.com/c/pay/e2e-storm" },
        })
      },
    )
    await page.route("https://checkout.stripe.com/**", (route) =>
      route.fulfill({ contentType: "text/html", body: "<h1>stripe</h1>" }),
    )

    await page.goto("/subscription")
    const upgrade = page.locator('button:has-text("Upgrade")').first()
    await expect(upgrade).toBeVisible({ timeout: 30_000 })
    await upgrade.click()
    // The guard itself: the button relabels to Processing and disables for
    // the whole held round trip
    await expect(
      page.getByRole("button", { name: /Processing/ }),
    ).toBeDisabled()
    // The rest of the storm fires synchronously in-page, guaranteed to land
    // while the response is still held (a Playwright click loop can lose that
    // race to the redirect). A disabled button swallows these clicks.
    await page.evaluate(() => {
      const target = Array.from(document.querySelectorAll("button")).find(
        (button) => /Upgrade|Processing/.test(button.textContent ?? ""),
      )
      for (let i = 0; i < 4; i++) target?.click()
    })
    checkout.release()

    await expect(page).toHaveURL(/checkout\.stripe\.com\/c\/pay\/e2e-storm/, {
      timeout: 30_000,
    })
    expect(sessions).toBe(1)
  })

  test("SUB-19 - A second tab shows Premium after an upgrade in the first", async ({
    page,
  }) => {
    // Both tabs share one session. Tab B opens on Free before the upgrade;
    // after Tab A leaves for checkout and the plan flips (mocked at the
    // context, standing in for the webhook write), a plain refresh in Tab B
    // must re-read /mgmts with the shared token and render Premium - no
    // re-login, no stale Free state.
    const tabB = await page.context().newPage()
    await tabB.goto("/subscription")
    await expect(
      tabB.locator('button:has-text("Upgrade")').first(),
    ).toBeVisible({ timeout: 30_000 })

    await page.route("**/api/subsc/checkout/create-checkout-session", (route) =>
      route.fulfill({
        json: { checkout_url: "https://checkout.stripe.com/c/pay/e2e-tabs" },
      }),
    )
    await page.route("https://checkout.stripe.com/**", (route) =>
      route.fulfill({ contentType: "text/html", body: "<h1>stripe</h1>" }),
    )
    await page.goto("/subscription")
    const upgrade = page.locator('button:has-text("Upgrade")').first()
    await expect(upgrade).toBeVisible({ timeout: 30_000 })
    await upgrade.click()
    await expect(page).toHaveURL(/checkout\.stripe\.com/, { timeout: 30_000 })

    let mgmtsReads = 0
    await page.context().route("**/api/subsc/mgmts", (route) => {
      mgmtsReads++
      route.fulfill({ json: PREMIUM_SUBSCRIPTION })
    })

    await tabB.reload()
    const status = tabB.getByText(/^Current Plan:/).first()
    await expect(status).toBeVisible({ timeout: 30_000 })
    await expect(status).toContainText("Premium")
    await expect(tabB).not.toHaveURL(/\/login/)
    expect(mgmtsReads).toBeGreaterThan(0)
    await tabB.close()
  })
})

test.describe("Premium plan state", () => {
  test.beforeEach(async ({ page }) => {
    skipWithoutCreds(PREMIUM_USER, "TEST_PREMIUM_EMAIL/TEST_PREMIUM_PASSWORD")
    await login(page, PREMIUM_USER.email, PREMIUM_USER.password)
  })

  test("SUB-04 - Subscription page shows Premium as current plan", async ({
    page,
  }) => {
    await page.goto("/subscription")

    // A Current Plan button is what the FREE card renders for a free user, so
    // a premium user wrongly shown as Free passed that on its own. The status
    // line names the plan, and only a premium user is offered a downgrade.
    const status = page.getByText(/^Current Plan:/).first()
    await expect(status).toBeVisible({ timeout: 30_000 })
    await expect(status).toContainText("Premium")
    await expect(
      page.locator('button:has-text("Downgrade")').first(),
    ).toBeVisible()
    // Expiration/renewal date is displayed for premium users
    await expect(
      page.locator("text=/renews? on|expires? on|expired on/i").first(),
    ).toBeVisible()
  })

  test("SUB-05 - Account profile shows Premium with Manage button", async ({
    page,
  }) => {
    await page.goto("/account")
    await expect(page.locator('h2:has-text("Account Profile")')).toBeVisible({
      timeout: 15_000,
    })

    await expect(page.locator("text=Premium").first()).toBeVisible()
    await expect(page.locator('button:has-text("Manage")')).toBeVisible()
    await expect(page.locator('button:has-text("Upgrade")')).toBeHidden()
    await expect(
      page.locator("text=/renews? on|expires? on|expired on/i").first(),
    ).toBeVisible()
  })
})

// Row 2015: GET /api/subsc/mgmts takes no user id at all - it reads the caller
// out of the token - so the isolation the row asks about is a property of the
// route, not of a filter someone could get wrong. Two real accounts asking the
// same URL is what proves it: each gets its own row and neither can name the
// other's.
test.describe("Subscription API isolation", () => {
  test("SUB-20 - The subscription route answers with the caller's own row only", async () => {
    // Premium first: CI registers that account in bootstrap, while the
    // lifecycle account only exists once 11-lifecycle has run, and specs run in
    // file order.
    const second = process.env.TEST_PREMIUM_EMAIL
      ? {
          email: process.env.TEST_PREMIUM_EMAIL,
          password: process.env.TEST_PREMIUM_PASSWORD || "",
        }
      : {
          email: process.env.TEST_LIFECYCLE_EMAIL || "",
          password: process.env.TEST_LIFECYCLE_PASSWORD || "",
        }
    skipWithoutCreds()
    test.skip(
      !second.email || !second.password,
      "needs a second account: TEST_PREMIUM_* or TEST_LIFECYCLE_*",
    )

    const seen: number[] = []
    let rows = 0
    for (const who of [FREE_USER, second]) {
      const { api, headers } = await apiLogin(who.email, who.password)
      try {
        const me = await api.get("/users/me", { headers })
        expect(me.ok(), `GET /users/me for ${who.email}`).toBeTruthy()
        const myId = (await me.json()).id

        const res = await api.get("/api/subsc/mgmts", { headers })
        expect(res.ok(), "GET /api/subsc/mgmts").toBeTruthy()
        const body = await res.json()
        // null is the right answer for an account with no subscription object -
        // a free account gets one, and receiving somebody else's row instead is
        // exactly the leak this row is about.
        if (body !== null) {
          expect(
            body.user_id,
            "the row returned belongs to the caller, not to whoever asked last",
          ).toBe(myId)
          rows += 1
        }
        seen.push(myId)
      } finally {
        await api.dispose()
      }
    }
    // Two different callers, and at least one real row between them, or the
    // assertion above proves nothing
    expect(new Set(seen).size, "the two accounts are distinct users").toBe(2)
    expect(
      rows,
      "neither account returned a row, so nothing was compared",
    ).toBeGreaterThan(0)
  })
})
