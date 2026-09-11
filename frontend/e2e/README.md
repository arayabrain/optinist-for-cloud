# E2E Release Tests (Playwright)

Automates the browser-testable part of release verification. Each test has a
stable ID (`AUTH-01`, `WF-04`, ...) grouped by feature area; the release test
sheets reference these IDs, so a green run checks off the matching rows and a
release tester only hand-verifies what the coverage maps list as manual.

- [Quick start](#quick-start)
- [Test environment](#test-environment)
- [Credentials and test accounts](#credentials-and-test-accounts)
- [Running the tests](#running-the-tests)
- [How the suite works](#how-the-suite-works)
- [Test groups](#test-groups)
- [Coverage maps](#coverage-maps)
- [Troubleshooting](#troubleshooting)

## Quick start

```bash
cd frontend
yarn install
npx playwright install chromium

# point the app at the backend — without REACT_APP_SERVER_* it derives the
# API host from window.location and calls itself, so the first request
# (GET /is_standalone) never answers. Both files are gitignored.
cp .env.example .env

# credentials (see below)
cat > e2e/.env <<'EOF'
TEST_USER_EMAIL=<free-plan test account email>
TEST_USER_PASSWORD=<password>
TEST_PREMIUM_EMAIL=<premium test account email>   # optional
TEST_PREMIUM_PASSWORD=<password>                  # optional
BASE_URL=http://localhost:3003
EOF

yarn test:e2e
```

## Tests that disturb the environment

Some invariants can only be observed by disturbing the thing under test: a
restart, a scale-to-zero, a stopped task, a full disk. Those tests cannot share
a run with anything else, because what they disturb is exactly what every other
spec depends on - and on the shared dev environment, what a colleague depends on
too.

Two mechanisms, and they compose:

**The `@disruptive` tag**, for anything that degrades the environment while it
runs. Filtered out of every run unless `RUN_DISRUPTIVE=1`, the same way `@slow`
is filtered on cost. Tag the test where it naturally lives rather than moving it
into a file of its own - the outage rows belong beside the public specs, the
stop-task rows beside the premium lane - and one variable governs the whole
family wherever it sits.

A tag alone only asks nicely, so `disruptiveSkipReason()` also asks the
environment: it reads `free_user_assignments` and `premium_user_assignments` for
any session active in the last 30 minutes belonging to an account that is not
one of ours, and skips naming those addresses if it finds one. Refusing to
disrupt is also the answer when the database cannot be reached at all, since not
knowing who is on is not the same as nobody being on.

**A file of its own**, for the narrower case where the disturbance breaks the
test runner's own fixtures rather than the environment's users - `20-boot`
restarts the backend container, which every other local spec is mid-conversation
with. Those keep their own variable (`RUN_RESTART=1`).

**PREM-06 needs the premium pool pre-staged**, and skips with its reason when it
is not. It wants two premium users holding two *distinct dedicated* instances,
and the cascade only grants that when both instances are running, carry a
premium ECS task, and have **no row at all** in `premium_user_assignments` - a
leftover standby row makes `try_reserve_instance` answer `already
reserved/assigned`, the second user falls through to the shared tier, and the
outcome half of rows 6221 / 6222 goes unverified. The dev pool parks itself at
one stopped standby after every lane, so staging is: delete the standby rows,
`aws lambda invoke --function-name development-premium-manager --payload
'{"action":"create_standby"}'`, wait for the new instance to actually reach
`stopped` (the Lambda returns its id and stops it a moment later, undoing a
start issued in between), start both instances, delete the standby rows again,
then `aws ecs update-service --service development-premium-optinist-cloud-service
--desired-count 2` and wait for a task on each.

The three lanes that predate the tag keep their own variables, because the sheets
cite them by marker in dozens of rows and re-citing those buys nothing:
`15-premium-aws` (`RUN_PREMIUM_AWS=1`, the `@prem` marker), `16-storage-aws`
(`RUN_S3_AWS=1`, the `S3-xx` markers) and `19-checkout-probe`
(`RUN_CHECKOUT_PROBE=1`). New disruptive work should use the tag.
`RUN_PREMIUM_AWS` is the one variable shared across lanes: it also switches on
`16-storage-aws`'s `S3-21..23`. `S3-22` and `S3-23` spend premium capacity
exactly as `15-premium-aws` does; `S3-21` is API-only and assigns nothing, and
rides the same flag only so that "may this run touch the premium account at
all?" stays one decision rather than two. Those rows carry their own IDs rather
than doubling up on `S3-01..03`: a run that forgot the variable skips them, and
a shared ID would let the free variant's pass tick the premium row off the
sheet. The 20s block is the premium block, paired with the free row on the last
digit (`S3-01` <-> `S3-21`), which leaves the 10s open for further free rows. A
sign-off run should also set `E2E_FAIL_ON_SKIP=1`, which turns that skip into a
failed run instead of a silent gap.

Whichever mechanism, three rules hold:

1. **Restore what you disturbed, or disturb only your own data.** A restart
   comes back; a per-user S3 object is the test account's own. Assert the
   restore rather than doing it best-effort - a spec that leaves the free tier
   scaled to zero has done more damage than a red test ever would.
2. **`--retries 0`.** A pass-on-retry hides real flakiness in something that
   mutated state; the second attempt no longer starts from the same place.
3. **Never in a default or scheduled lane.** `yarn test:e2e` must not restart a
   container or scale a service, however long anyone is prepared to wait.

**Open: `23-subscription-lifecycle` does not yet satisfy rule 3.** It writes plan
and expiry to the shared development RDS through `runSqlWriteOnDev` and carries
no opt-in flag or `@disruptive` tag, so an unfiltered `yarn test:e2e` against a
deployed `BASE_URL` will rewrite its account's subscription without being asked.
Before this change `runSqlWriteOnDev` had exactly one caller, `15-premium-aws`,
gated behind both `RUN_PREMIUM_AWS=1` and `RUN_SLOW=1`; this spec is the only
deployed-write caller in the default lane. What keeps it from being rule 1's
problem is that every write lands on one dedicated account that nothing else
uses, and the `afterAll` restores it to the free-plan shape registration
leaves - so the fabricated premium row exists only while the run does. What it
still needs is a gate, so the choice to write to a shared environment is made
deliberately rather than by default.

Sheet rows whose Action asks for a disruption that nothing covers yet stay
`MANUAL` with the reason in Notes rather than being quietly dropped. The
free-tier outage rows (system sheet 08) are now OUT-01..03, and the public EC2
termination row (827) is ASG-01 awaiting its first run. Still outstanding: the
premium disruption rows (sheet 06-2: `ecs stop-task` on a user's own premium
task, the `reconcile_instance` invoke, stop / terminate of a premium instance).
The ENOSPC row (2030) was adjudicated instead: real disk-full injection on the
shared ECS task risks every co-tenant workload, and the whole observable retry
contract is unit-asserted. Row 824's notification half is not outstanding but
unassertable here: no `development-` alarm carries an SNS action
(`critical_alerts_actions` is empty off production), so there is no notification
path to exercise, and HEALTH-27 asserts the alarm's own evaluation instead.

## Nothing that mutates ever runs against production

Two lanes can be pointed at production, and both are read-only: `17-aws-health`
and `18-stripe-audit`, under `HEALTH_ENV=subscr`. `runSql` sends every statement
off the local stack through `assertReadOnly`, which strips comments, refuses
multi-statement SQL and requires a leading `select` or `show`, and the Stripe
helper is GET-only and refuses a live key.

Every lane that writes refuses to start anywhere but development:

- **`12-admin` and `13-account`** hold administrative privileges - they promote a
  role with a direct `UPDATE`, delete an account and proxy-sign-in as another
  user. `localStackSkipReason()` skips them on any BASE_URL that is not
  localhost, so they cannot reach a deployed environment at all, production
  included. The one deletion destroys an account the run registered for the
  purpose.
- **`15-premium-aws`, `16-storage-aws` and `19-checkout-probe`** assert their
  BASE_URL contains `development-optinist` before touching anything, so a
  mispointed invocation fails on its first line rather than mutating the
  environment it was pointed at.
- **`22-disruptive`** additionally refuses when another account has been active
  in the last 30 minutes, and refuses when it cannot tell.
- **`runSqlWriteOnDev`** makes the same assertion for the one sanctioned SQL
  write path.

## Running a production release round

`E2E_TARGET=<name>` reads `e2e/.env.<name>` in place of `e2e/.env`, so a target is
chosen whole: origin, API, RDS selectors, Stripe selector and accounts together.
Half a swap points the browser at one environment and the API or the account at
another, which reads as a login failure rather than as a mistake. Both files are
gitignored; `e2e/.env.prod.example` is the committed template.

A target inverts the usual precedence: its values win over the shell, so a
one-off `TEST_USER_EMAIL=...` on the command line is overridden rather than
honoured, and the run prints which keys that happened to. A missing target file,
a target name that is not `[A-Za-z0-9_-]+`, or a file lacking `BASE_URL`,
`API_URL`, `HEALTH_ENV` or the `TEST_USER_*` pair all fail at start-up rather
than falling back to development.

```bash
cd frontend
E2E_TARGET=prod npx playwright test e2e/17-aws-health.spec.ts
E2E_TARGET=prod RUN_CLEANUP=1 npx playwright test e2e/99-cleanup.spec.ts
```

```bash
python3 infrastructure/scripts/manual_test_scan.py --check production --cases release \
  --user-email <the release premium account> -o scan-report.md
```

The scan requires a pinned target on production, either `--user-email` or
`--user-id`: unpinned it selects the newest premium account, which on production
is a paying customer, and reports their billing history as release results.

**What runs there.** The lanes that mutate infrastructure, Stripe or the database
refuse to start off development, and `12-admin` / `13-account` refuse anything but
localhost, so the guards make the choice. What is left is the browser specs, which
carry no environment gate: release sheets 01-05, 07, 08 and part of 10. They write
as the test account, and `globalSetup` skips its cleanup off development, hence the
sweep above; AUTH-04's registrations are outside it and need an admin.
`18-stripe-audit` runs too, minus AUDIT-09 and AUDIT-10, which skip on a live
key. AUDIT-10's question is answered by the scan, but AUDIT-09's is not: nothing
else compares the app's own invoice list against Stripe's or resolves the hosted
invoice and PDF links, so rows 244-250 are unverified on production rather than
covered elsewhere. Restoring them means letting the GET-only reads run against a
live key, as `manual_test_scan.py` already does under `--check production`.

**Accounts.** Production's Firebase project is not development's, so no
development account or password carries over; the addresses exist already and the
passwords live in the shared credential store. `TEST_PREMIUM_*` is a database
state an admin stages with `PUT /admin/users/{id}/subscription`, no payment
needed. `TEST_STRIPE_*` cannot be staged that way: its rows read Stripe's API, so
the account must own a real customer with a live subscription, and borrowing an
existing customer id fails the scan's own identity checks besides pointing the
cancel/reactivate rows at a real payer.

## Operator-run, not CI-enforced

Every lane above needs AWS credentials and a live environment, so none of them
runs on a pull request or in the weekly schedule. A row they cover is green as
of the last time an operator ran it, which is not the same as guarded on every
merge. What runs per-PR is the always-on local specs and `yarn typecheck:e2e`.

## Test environment

Tests run against any deployment of the app; pick one:

### A. Local stack (default)

Backend + DB in docker, frontend on the host (installing node_modules
through the docker bind mount is unusably slow, so don't use the
containerized frontend for e2e):

```bash
# from the repo root: db + backend
docker compose -f docker-compose.dev.multiuser.yml up -d db studio-dev-be

# frontend on the host — pick a free port and match BASE_URL
# (needs frontend/.env from the quick start, or the app calls itself for the API)
cd frontend && PORT=3003 BROWSER=none yarn start
```

First-time local DB setup: the `subscription_plans` table must have the
Free (id 1) and Premium (id 2) rows or registration 500s on a foreign key.
Seed with `infrastructure/scripts/seed_subscription_plans.py` (canonical,
reads `SUBSCRIPTION_PLANS_CONFIG`) or a minimal insert:

```sql
INSERT INTO subscription_plans (id, name, price, billing_cycle, features, currency, status)
VALUES (1, 'Free', 0, 1, JSON_OBJECT(), 1, 1), (2, 'Premium', 2000, 1, JSON_OBJECT(), 1, 1);
```

### B. Deployed environment

```
BASE_URL=https://<frontend-host>
API_URL=https://<backend-host>     # only if not BASE_URL with port 8000
```

The API is used by the global setup and test fixtures (workspace
find-or-create, cleanup). By default it's derived from `BASE_URL` by
swapping the port to 8000, which matches the local stack; deployed
environments usually need `API_URL` set explicitly.

**Warning:** tests create and delete workspaces named `e2e-*` and
copy/delete records inside them, and the global setup **deletes every
workspace whose name starts with `e2e-`** owned by the test account. Use a
dedicated test account; never point the suite at an account whose data you
care about.

## Credentials and test accounts

All credentials come from env vars, or `frontend/e2e/.env` (gitignored,
simple `KEY=VALUE` lines). Nothing is ever committed. `E2E_TARGET=<name>` reads
`e2e/.env.<name>` instead, which is how a production round selects its whole
environment at once; see [Running a production release
round](#running-a-production-release-round).

| Variable                                           | Required                          | Purpose                                                                                                                                                                                                                            |
| -------------------------------------------------- | --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TEST_USER_EMAIL` / `TEST_USER_PASSWORD`           | for logged-in tests               | free-plan account; without it only public/validation tests run, the rest skip                                                                                                                                                      |
| `TEST_PREMIUM_EMAIL` / `TEST_PREMIUM_PASSWORD`     | optional                          | enables SUB-04/05 (premium subscription state)                                                                                                                                                                                     |
| `TEST_LIFECYCLE_EMAIL` / `TEST_LIFECYCLE_PASSWORD` | optional                          | enables `11-lifecycle` (LC-01..05, LC-07..10, LC-14..15, LC-17..20, LC-24..31) and `23-subscription-lifecycle` (LC-06, LC-11..13, LC-16, LC-21..23). Both run on the local stack or against deployed dev, and both rewrite this account's plan, expiry and usage; LC-16 deletes a throwaway it registers itself - use a dedicated address, never a shared account. `11-lifecycle` registers and verifies the account itself on a local run; against a deployed environment that is not possible (forcing `email_verified` needs the Admin SDK), so create it once and verify it out of band |
| `TEST_PREMIUM_OVER_EMAIL` / `TEST_PREMIUM_OVER_PASSWORD` | deployed `11-lifecycle` only | provisioned account already over its own storage quota, with the S3 data to prove it. LC-03 rents that state as-is; LC-04 moves its **quota** (never its data) to land on 95%, and restores it. Without it LC-03/04/08/18..20 skip with a named reason |
| `TEST_GRACE_OVER_EMAIL` / `TEST_GRACE_OVER_PASSWORD` | deployed `11-lifecycle` only | provisioned expired-premium account holding more than the 5GiB free limit in real S3 data - the one state no quota dial can reach, because an expired premium is held to the hardcoded free limit whatever its quota column says. LC-18..20 re-stamp its `expiration` into the grace window per run and restore it |
| `TEST_FREE_DOWNGRADE_EMAIL` / `TEST_FREE_DOWNGRADE_PASSWORD` | deployed `11-lifecycle` only | a real formerly-paid account carrying more than the free limit. LC-08 drops its premium row so `determine_lifecycle` reports FREE - a genuine downgrade rather than a faked one - then restores plan and quota |
| `TEST_ADMIN_EMAIL` / `TEST_ADMIN_PASSWORD`         | optional, local stack only        | enables ADMIN-01..22 (Account Manager). Defaults to `e2e_ci_admin@test.com` and the free user's password. The spec registers this account itself and promotes it to admin with one `user_roles` UPDATE, because registration always lands as an operator — use a dedicated address |
| `BASE_URL`                                         | default `http://localhost:3000`   | frontend under test                                                                                                                                                                                                                |
| `API_URL`                                          | default `BASE_URL` with port 8000 | backend, for setup/cleanup API calls                                                                                                                                                                                               |
| `RUN_SLOW`                                         | optional                          | include the `@slow` workflow-run tests                                                                                                                                                                                             |
| `RUN_PREMIUM_AWS`                                  | optional, deployed env only       | opt into the `15-premium-aws` lane (PREM-01..09): real premium assignments, real AWS asserts; run with `--retries 0`. Also required, alongside `RUN_S3_AWS=1`, for `16-storage-aws`'s premium rows S3-21..23                       |
| `RUN_S3_AWS`                                       | optional, deployed env only       | opt into the `16-storage-aws` lane: real-S3 asserts on the per-user buckets. On its own it runs S3-01..05, the free rows, and touches no premium capacity; add `RUN_PREMIUM_AWS=1` for the premium rows S3-21..23. Run with `--retries 0`                                           |
| `RUN_RESTART`                                      | optional, local stack only        | opt into the `20-boot` lane (BOOT-01): restarts the backend container to observe a real boot; nothing else may share the stack while it runs                                                                                        |
| `RUN_DISRUPTIVE`                                   | optional                          | include tests tagged `@disruptive`, which degrade the shared environment while they run. They additionally refuse to start if the database shows another user active in the last 30 minutes                                          |
| `RUN_STRIPE_WRITE`                                 | optional, deployed env only       | opt into `21-stripe-roundtrip` (STRIPE-01): cancels a real Stripe subscription at period end and reactivates it; run with `--retries 0` and nothing else reading that account                                                        |
| `FIREBASE_ADMIN_PYTHON`                            | optional, deployed env only       | interpreter that can `import firebase_admin`, used by `23-subscription-lifecycle` LC-16 to force `email_verified` on the throwaway it registers. `helpers.ts` does this inside the backend container, which a deployed run has no local copy of; Firebase is the same shared dev project either way. Defaults to `python3`, and LC-16 skips with a named reason rather than failing when the import is unavailable |
| `TEST_STRIPE_EMAIL` / `TEST_STRIPE_PASSWORD`       | optional, deployed env only       | an account whose subscription is really backed by Stripe, i.e. one that owns a Stripe customer with a live subscription. **Not** `TEST_PREMIUM_*` or `TEST_PREMIUM2_*`: those are premium in the database only and own no Stripe customer at all, so every Stripe-side assertion about them would pass against nothing. Enables AUDIT-09 and STRIPE-01, which otherwise skip - and the skip reason distinguishes "unset" from "set, but that account has no Stripe customer". This account does **not** need to be premium-routable: expect `/users/me/premium/status` to report `is_premium: false` for it, because a premium subscription expiring within 24 hours reads as "Limit Grace" (integer-day truncation in `crud_users.py`) and dev bills Premium daily. That is a known app bug, not a broken account - don't "fix" this slot by pointing it at an e2e account with no Stripe data |
| `TEST_PREMIUM2_EMAIL` / `TEST_PREMIUM2_PASSWORD`   | optional                          | second premium account, needed only by PREM-06 (two-user scale-down)                                                                                                                                                               |
| `E2E_TARGET`                                       | optional                          | reads `e2e/.env.<name>` in place of `e2e/.env`, and its values win over the shell; see [Running a production release round](#running-a-production-release-round)                                                     |
| `HEALTH_ENV`                                       | default `development`             | which deployed environment the AWS-reading lanes name: `17-aws-health`'s resources, the log groups `helpers.ts` exports, the Stripe secret `stripeKey()` fetches, and `18-stripe-audit`'s `--check`. `subscr` is production |
| `STRIPE_SECRET_ENV`                                | default `<HEALTH_ENV>-optinist`   | overrides which `<env>/stripe/config` secret is read, when the Stripe account is not named after `HEALTH_ENV`                                                                                                       |
| `RDS_PROXY_HOST` / `RDS_SECRET_ID` / `RDS_SSM_INSTANCE_NAME` | development defaults    | where the SQL-over-SSM rows read. The defaults name development, so a run against another environment must set all three or it reports development's data as that environment's                                     |

The account must exist in **both** Firebase (email/password sign-in,
email verified) and the target environment's DB. Note: the `test_users` in
`development.tfvars` have no passwords recorded — those accounts
authenticate via Admin-SDK-minted tokens in the load-test scripts and can't
be used for UI login as-is.

### Bootstrapping accounts for a local stack

Register through the API, then verify the email with the Firebase Admin SDK
(the backend container has the service-account key):

```bash
# 1. register (note role_id is required)
curl -X POST http://localhost:8000/api/register -H "Content-Type: application/json" \
  -d '{"name":"E2E Free","email":"<email>","password":"<password>","role_id":20}'
# → note the returned "uid"

# 2. mark the email verified
docker exec <backend-container> python -c "
import firebase_admin
from firebase_admin import auth, credentials
cred = credentials.Certificate('studio/config/auth/firebase_private.json')
firebase_admin.initialize_app(cred)
auth.update_user('<uid>', email_verified=True)"
```

For a premium account, additionally upgrade the plan **and** the storage
quota (two tables — forgetting the second leaves the account showing a 5GB
quota):

```sql
UPDATE subscription_users SET plan_id = 2,
  expiration = DATE_ADD(NOW(), INTERVAL 1 MONTH), scheduled_downgrade = 0
  WHERE user_id = <id>;
UPDATE user_storage_usage SET storage_quota_bytes = 214748364800  -- 200GB
  WHERE user_id = <id>;
```

On a local stack (no ECS), premium login shows a "Premium assignment
issue ... Falling back to shared resources" snackbar — expected.

The lifecycle spec (LC-01..23) additionally needs `SKIP_STORAGE_CHECKS=false`
in `studio/config/.env` (restart the backend after changing it). The
local-testing default of `true` disables all storage lookups backend-side —
deployed environments run with `false` — and the spec skips with a clear
reason while it's on.

## CI (GitHub Actions) secrets

The scheduled workflow (`.github/workflows/e2e.yml`) runs the local docker
stack inside the runner, so it needs every credential the local stack reads,
supplied as repo secrets. If any is unset the workflow still starts but writes
an empty config file and the backend fails to boot — the run then crawls to the
90-minute timeout instead of failing fast. Required:

| Secret / variable                                      | Written to                                 | Notes                                      |
| ------------------------------------------------------ | ------------------------------------------ | ------------------------------------------ |
| `E2E_FIREBASE_PRIVATE_JSON`                            | `studio/config/auth/firebase_private.json` | Firebase service account                   |
| `E2E_FIREBASE_CONFIG_JSON`                             | `studio/config/auth/firebase_config.json`  | Firebase web config                        |
| `E2E_STUDIO_ENV`                                       | `studio/config/.env`                       | backend/DB config; strip personal AWS keys |
| `E2E_TEST_USER_EMAIL` / `E2E_TEST_USER_PASSWORD`       | `TEST_USER_*` env                          | free-plan CI user (bootstrap registers it) |
| `E2E_TEST_PREMIUM_EMAIL` / `E2E_TEST_PREMIUM_PASSWORD` | `TEST_PREMIUM_*` env                       | premium CI user                            |
| (none)                                                 | `TEST_LIFECYCLE_*`, `TEST_ADMIN_*` env     | fixed addresses reusing `E2E_TEST_USER_PASSWORD`; both specs bootstrap their own account, so no new secret is needed |
| `SUBSCRIPTION_PLANS_CONFIG` (variable, not secret)     | plan-seed step                             | pulled from the dev task definition        |

The lifecycle user needs no secret — the workflow hardcodes
`e2e_ci_lifecycle@test.com` and reuses `E2E_TEST_USER_PASSWORD`. Values and the
one-time `gh secret set` commands are in the internal drive doc linked from the
PR description.

## The weekly regression, and running it on a branch

Nothing opt-in runs per PR. `.github/workflows/e2e.yml` gathers all of it into
one Monday 00:00 UTC job: the Playwright suite with `RUN_SLOW=1`, the two
real-database pytest lanes (`make premium_lock_it`, `make workflow_count_it`),
and a re-run of the per-PR unit lanes. A sheet row whose e2e citation ends in
`@slow` is checked off by that run, not by a green PR.

The schedule only ever fires from the default branch. To exercise a feature
branch's version of the workflow before it merges, dispatch it against that
branch by name. A PR number is not a ref:

```bash
gh workflow run e2e.yml --ref <branch>
gh run list --workflow e2e.yml -L 3
```

`--ref` picks both the workflow definition and the checked-out code, so a job
that exists only on the branch still runs. Locally the same lanes are
`RUN_SLOW=1 yarn test:e2e`, `make premium_lock_it` and `make workflow_count_it`.

## Running the tests

```bash
yarn test:e2e                    # everything except @slow (~14 min, see below)
RUN_SLOW=1 yarn test:e2e         # everything including workflow runs
RUN_SLOW=1 yarn test:e2e --grep @slow   # only the workflow runs
yarn test:e2e 01-auth            # one group
yarn test:e2e -g "WS-06"         # one test case
yarn test:e2e:headed             # watch the browser
yarn test:e2e:report             # open the last HTML report
yarn test:e2e:cleanup            # delete the run's e2e-* data, on demand

# read-only AWS + RDS health lane; needs a deployed BASE_URL and AWS creds
yarn test:e2e e2e/17-aws-health.spec.ts --retries 0
# same lane against production. The RDS and Stripe selectors are mandatory off
# development - their defaults point at development, and the lane refuses to run
# rather than report development's data as production's.
HEALTH_ENV=subscr BASE_URL=https://www.araya-optinist.com \
  RDS_PROXY_HOST=... RDS_SECRET_ID=... RDS_SSM_INSTANCE_NAME=... \
  STRIPE_SECRET_ENV=subscr-optinist \
  yarn test:e2e e2e/17-aws-health.spec.ts --retries 0
# read-only Stripe + DB audit lane (runs manual_test_scan.py and asserts it)
yarn test:e2e e2e/18-stripe-audit.spec.ts --retries 0
# real Stripe checkout hand-off, no card entered (writes to Stripe: opt-in)
RUN_CHECKOUT_PROBE=1 yarn test:e2e e2e/19-checkout-probe.spec.ts --retries 0
# restarts the local backend to watch it boot; nothing else may share the stack
RUN_RESTART=1 yarn test:e2e e2e/20-boot.spec.ts --retries 0
# type-check the e2e specs (the app tsconfig only covers src/)
yarn typecheck:e2e
```

`yarn test:e2e` vs `npx playwright test`:

- `yarn test:e2e` is just `playwright test` (see `package.json`), and Yarn 1
  forwards any trailing arguments and flags straight through — so
  `yarn test:e2e -g "WS-06"` is equivalent to `npx playwright test -g "WS-06"`,
  and `--grep`, `--retries 0`, a spec path, etc. all work after `yarn test:e2e`.
- Prefer the `yarn` form: it always runs the project-local Playwright.
- Reserve `npx playwright …` for Playwright's own subcommands that have no yarn
  script (e.g. `npx playwright install chromium`); a *global* `npx playwright`
  can resolve a different version and fail with `Cannot find module` (see
  [Troubleshooting](#troubleshooting)).

Notes:

- Tests run sequentially in one worker (they share account state).
- Local runs get 1 automatic retry (CRA dev-server hydration occasionally
  swallows an early click); CI gets 2. A test listed as "flaky" passed on
  retry.
- `@slow` = anything that performs a real workflow execution (5–10 min each;
  slower on an ARM Mac where the backend image is amd64-emulated). Keep the
  machine awake for these — `caffeinate -i RUN_SLOW=1 yarn test:e2e
--grep @slow` — sleep mid-run is the #1 cause of bogus failures. These groups
  are in this lane:
  - WF-04/05/06, the tutorial runs, which are the thing being tested.
  - REC-07, which downloads an NWB file. One exists only after a completed run,
    and global setup deletes the workspace at the start of every run.
  - the whole `Private Dataview` group of `06-dataview`, tagged on the describe.
    Only success records reach the dataview and the sample data ships no
    computed outputs, so its first test mints them with a real run.
  - VIS-02, whose `cell_roi` overlay is a suite2p_roi node output. The other
    VIS tests plot the sample TIFF, which the import does ship.
  - PUB-05/06 in `14-public`, which publish records and read their input data
    back anonymously on the public page. Tutorial1 carries the CSV and TIFF
    input nodes, Tutorial4 the HDF5 and MAT pair; minting either costs a real
    run when the records don't already exist.
- **The default lane runs no workflows**, which is what keeps it under 15
  minutes. The trade is that a default run leaves 23 release-sheet rows and 27
  system-sheet rows unchecked; their e2e citations are marked `@slow` in the
  sheets rather than counted as covered by a green default run.

## How the suite works

Understanding these makes failures much easier to read:

- **One login per run** (`global-setup.ts`): logs in through the UI once and
  saves storage state to `e2e/.auth/free.json` (gitignored); all authed
  specs reuse it via `test.use({ storageState })`. This exists because
  per-test Firebase logins hit rate limits. The auth spec (login/logout
  tests) and the premium describe still log in for real — that's what they
  test.
- **Startup cleanup** (`global-setup.ts`): deletes the test account's
  `e2e-*` workspaces via the API so leftovers can't push rows out of the
  virtualized workspace grid. There is deliberately no teardown: a run's data
  survives until the next run, so it can be inspected. To drop it sooner, run
  the cleanup group (`99-cleanup`) — same `deleteE2eWorkspaces` helper, opt-in
  via `RUN_CLEANUP` so an ordinary run can never delete data mid-inspection.
  The `99-` prefix is load-bearing: workers are serial and files run in name
  order, so any earlier prefix deletes `e2e-data` out from under the groups
  that follow it.
  `DELETE /workspace/{id}` removes the workspace's input and output data with
  it, in S3 too when remote storage is on.
- **API-based setup, UI-based assertions** (`helpers.ts`): specs that need a
  workspace find-or-create it via the API (`ensureWorkspaceId`, reading the
  app's Bearer token from localStorage) and navigate straight to
  `/workspaces/{id}`. Only the workspace spec drives the grid UI, because
  the grid is what it tests.
- **Shared data workspace**: the workflow/record/file/dataview specs share
  one workspace named `e2e-data`; `ensureTutorialRecords` imports the sample
  data on first need, so the ~1 min import happens once per run and any spec
  can run standalone.
- **Data preconditions are asserted, not skipped**: the record and dataview
  specs used to open with `test.skip(!hasData)` over a probe that swallowed its
  own timeout, so an empty grid produced a skip — and a skip reads as a pass in
  the summary the release sheets are signed off against. They now wait for the
  rows they need and fail if they never arrive. Where the precondition costs a
  real workflow run, the test is `@slow` rather than silently paying for it on
  every default run.
- **A local run of `11-lifecycle`, `12-admin` or `13-account` fails rather than
  skips.** These groups are the only coverage several LC, ADMIN and ACC rows
  have, so on a local BASE_URL every reason they cannot execute (missing
  credentials, unreachable docker DB, `SKIP_STORAGE_CHECKS=true`) is a broken
  environment and is raised as an error naming the rows it leaves unverified.
  `12-admin` and `13-account` still skip off a local BASE_URL; `11-lifecycle`
  no longer does - all 23 of its rows run against deployed dev too.
- **`11-lifecycle` rows must not claim a real premium instance.** A login by an
  account whose plan is premium triggers a genuine assignment on deployed dev:
  a `t3.large`, a per-user ALB target group and a listener rule, none of which
  the run releases. Rows whose subject is the premium plan itself mock the
  assignment endpoints (`mockPremiumAssignment`); rows whose subject is a quota
  or a log line stay on the free plan, because the effective quota for a free
  account is just the quota column and the gate under test reads the same value
  either way. Neither is cosmetic - an unmocked premium login costs money and
  holds pool capacity until the 2h inactivity sweep reclaims it. An
  expired premium is exempt: assignment is gated on the derived status,
  not the plan column, so the grace and overdue rows log in unmocked.
- **The cleanup rows' synthetic `instance_id` is not a cloak.** It hides
  the seeded row from `_get_users_for_cleanup`, which every real worker
  runs instance-scoped, so backdating `logged_out_at` cannot get the
  account's data deleted. The orphan sweep is not instance-scoped and
  reads an id EC2 cannot resolve as a terminated instance, so it deletes
  the seed row itself (DB row only, never files). LC-26/27 re-check the
  seed before each selection so a sweep landing mid-run fails as itself
  rather than as a cleanup-selection bug.

## Test groups

Each spec's own header comment carries the detail - what it asserts, why it is
gated and how to run it. This table is the map, not the documentation.

| Spec file          | IDs         | Covers                                                                                                      |
| ------------------ | ----------- | ----------------------------------------------------------------------------------------------------------- |
| `01-auth`          | AUTH-01..19 | login, logout, session persistence, unverified email and resend, header nav, registration validation, the logged-out guard on protected routes |
| `02-workspace`     | WS-01..07   | workspace create, list, navigate, storage reload, one refresh per session, delete                           |
| `03-workflow`      | WF-01..09   | sample data import, reproduce, tutorial runs (`@slow`), run validation, tabs                                |
| `04-record`        | REC-01..10  | record list, parameters, copy, delete, workflow/Snakemake/NWB downloads                                     |
| `05-file-handling` | FILE-01..06 | file tree dialog, wildcard filter, check-all, sidebar toggle, sync progress indicators (file tree and CSV settings) |
| `06-dataview`      | DV-01..20   | table, filters (incl. workspace, private and public), sort order, pagination, dialogs, public access, thumbnails, publish, concurrent public reads, rapid-toggle last-action-wins and concurrent-publish version integrity. DV-01..08 and DV-12..20 are `@slow` (DV-20 additionally needs the local docker DB and skips elsewhere); DV-09/10/11/18 need no records and run by default |
| `07-subscription`  | SUB-01..20  | free and premium plan UI, per-card feature lists, responsive widths, `/thanks` guard, invoice page, cancel / reactivate, checkout and portal hand-offs, browser-Back out of checkout, the upgrade click-storm guard, two-tab premium consistency |
| `08-storage`       | STO-01..09  | under-quota login, the over-quota modal, dedicated / shared / still-scaling premium assignment snackbars, storage-bar colours by threshold, warning-dismissal persistence and its logout reset, the reload button's in-flight state |
| `09-visualize`     | VIS-01..05  | sidebar info, Cell-ROI plot, frame playback, second plot type, ROI editor                                   |
| `10-uploads`       | UPL-01..08  | CSV, HDF5 and MAT node dialogs (each structure tree asserted by the datasets, types, shapes and sizes the sample files actually carry, and a data path selected in both, which requires moving the selection off the one the tutorial arrived with), image / HDF5 / MAT upload                                                   |
| `11-lifecycle`     | LC-01..05, LC-07..10, LC-14..15, LC-17..20, LC-24..31 | quota and inactivity lifecycle (including the dead-session Stay Active 401, LC-31), plus free-logout DB bookkeeping and its re-login reset, and the cleanup job's own selection asked of the real MySQL: the 60-minute grace boundary (LC-26) and a stale `active_workflow_count` blocking collection until `recover_stale_workflow_counts()` clears it (LC-27). The deletion the job then performs is left to `test_cleanup_job.py` - running it would delete the test account's own files. Runs on the local stack **and** against deployed dev: locally the storage rows dial a real sparse ballast file inside the backend container, while deployed the same recalculation reads S3 object sizes, where nothing is sparse - so those rows either rent a provisioned fixture's data (`TEST_PREMIUM_OVER_*`, `TEST_GRACE_OVER_*`, `TEST_FREE_DOWNGRADE_*`) or import sample data the account owns and dial the quota against what was measured. LC-26/27 reach the job's own Python through `runInDeployedBackend`, seeding against a synthetic `instance_id` no real worker resolves, which is what keeps a backdated `logged_out_at` from handing the account to the live `cleanup_worker`. The subscription-state tests live in `23-subscription-lifecycle` |
| `12-admin`         | ADMIN-01..22 | admin Account Manager: access gating (drawer entry and dashboard tile), user list columns, sort and rows-per-page, edit / add / delete / proxy-signin / subscription modals and their Cancel paths, the create / edit / role-change / demotion happy paths and their validation, the subscription and storage columns against the DB, one real deletion of a throwaway account, and re-registration of the deleted address. All mutations land on disposable per-run accounts. Local stack only |
| `13-account`       | ACC-01..06  | Account Profile self-service: change-password modal (validation, wrong current password, a real change verified at login) and the inline name edit, on a disposable per-run account. Local stack only |
| `14-public`        | PUB-01..07  | public-instance behaviour: deep-link SPA shell and client routing, `/health`, chunk-load auto-reload, frontend error reporting and the report's arrival in the public tier's log group (PUB-07), and anonymous public-page loads of published HDF5 / MAT / CSV / TIFF input data. PUB-05/06 are `@slow` (they mint and publish real records) |
| `15-premium-aws`   | PREM-01..09 | real premium assignment against deployed dev: a real login assigns a real tier with the ECS scale-up asserted on the live cluster, the assign / hard-release / reassign round-trip with its per-user ALB target group, reload adoption, the browser-close beacon's soft release, the idle scale-down floor, and a real tutorial run on the dedicated instance with per-user S3 outputs. `@slow` plus `RUN_PREMIUM_AWS=1`; the sheets cite it as `@prem` |
| `16-storage-aws`   | S3-01..08, S3-21..23 | real-S3 truth for the storage rows: the per-user bucket and an uploaded object read back from S3 rather than from the API, which swallows S3 failures; the merged listing and sync round-trip; sample import and workspace delete against the real prefixes; an anonymous public read of a freshly published run; the public sync-error state and its Retry recovery; and the publish-time repair of a broken local config with a five-record batch draining in one sync run. S3-06..08 add the publish sync's own state machine, whose verdict is a DB transition plus the tier's own log line rather than anything a browser can see: an `error` row self-healing to `synced` on the first public read (attributed to the endpoint, not to the 5-minute job, by the version bump only the endpoint makes); the job carrying a publish `pending -> synced`, failing it to `error` once its required S3 configs are gone and retrying it back when they return - through to the public reproduce answering 200 again, which is what makes the retry a recovery rather than a DB flag; and the 202 `pending_sync` the sheet calls unprovokable, staged deterministically by removing only `experiment.yaml` after publishing right behind a fresh sync tick. `@slow` plus `RUN_S3_AWS=1`. Rows 01..03 are registered once per tier from one body — the sheets ask the same S3 question of the free and the premium account, and the only differences are which account signs in and which table carries `active_workflow_count` — but they keep separate IDs, `S3-01..03` free and `S3-21..23` premium (the 20s block, paired on the last digit), so a run that omitted `RUN_PREMIUM_AWS` cannot tick the premium row off the free variant's pass. `S3-21..23` additionally need `RUN_PREMIUM_AWS=1`. Of the three only `S3-22` and `S3-23` hold a real assignment: they wait for the per-user target group to report a healthy target before driving anything through the ALB (a dedicated assignment goes live before its task serves, and work sent too early answers 502), pin their import and their run to the premium instance by reading `x-user-tier: premium` off the wire, release the assignment in an `afterEach` and assert the lane holds nothing in an `afterAll`, and skip with a reason — rather than fail — when the dev pool has no capacity to place or cannot keep a premium task serving. `S3-21` goes through the API alone, never mounts the dashboard and so assigns nothing; expect no assignment evidence from it. Their helper API calls go unrouted, as PREM-07's do: nothing this lane asks of the API needs the premium instance, and routing it there would expose every call to that same 502. S3-04..08 stay free-only, each saying why in the spec |
| `17-aws-health`    | HEALTH-01..27 | read-only truth for the AWS Monitoring sheets and System 20's integrity rows: every tier's ECS service and tasks, the target groups, the ALB's per-tier routing rules, both ASGs, RDS, EFS, the buckets the database names, every declared alarm, the four log groups, the background metric namespace, the public HTTP contract, and the subscription tables read over SSM. HEALTH-27 proves the public unhealthy-hosts alarm last fired on evaluated datapoints rather than a written state. No opt-in flag, ~3 min; `HEALTH_ENV=subscr` points it at production, which is how a release round health-checks prod |
| `18-stripe-audit`  | AUDIT-01..10 | read-only truth for sheet 09's Stripe rows and the Stripe-side half of sheet 20: catalogue, JP tax, the webhook endpoint, the premium account's single customer and subscription, the invoice and event timeline, and the stored ids and dates matching Stripe's own. Runs `infrastructure/scripts/manual_test_scan.py` once and asserts its per-row verdicts rather than reimplementing its forty checks. No opt-in flag, ~30s; `HEALTH_ENV` as above. AUDIT-09 needs `TEST_STRIPE_*` |
| `19-checkout-probe` | CHECKOUT-01..04 | the real Stripe-hosted checkout hand-off, no card ever entered: the endpoint mints a live `cs_test_` session that two clicks never share, the hosted page renders our own amounts and the `Sandbox` badge, and abandoning by Back or by closing the tab leaves the account free - read from the deployed database, not the API. `RUN_CHECKOUT_PROBE=1`, test mode, guarded to `development-optinist`. Completing a payment is not automatable: the submit is CAPTCHA-gated |
| `20-boot`          | BOOT-01     | the background scheduler's first run after a real boot (system row 127), asserted from APScheduler's own scheduling fields rather than wall clock: the first fire is `INITIAL_RUN_DELAY_SECONDS` after the scheduler starts, the next a full interval later, and the orphan-sweep warm-up skip is logged exactly once. Local `BASE_URL` only and `RUN_RESTART=1` - see [Lanes that own a resource](#lanes-that-own-a-resource) |
| `21-stripe-roundtrip` | STRIPE-01 | cancel at period end through our own API, then reactivate, read back from Stripe rather than a mock: `cancel_at_period_end`, `cancel_at`, the `customer.subscription.updated` event, and our own `scheduled_downgrade` without a plan downgrade. The undo is in a `finally` and asserted. `RUN_STRIPE_WRITE=1` and `TEST_STRIPE_*`, since the `TEST_PREMIUM_*` accounts own no Stripe customer to cancel |
| `22-disruptive`    | OUT-01..05, ASG-01 | the rows that can only be observed by breaking something, against deployed dev: with the free service at zero the public tier still serves the shell, `/auth/login` and an authenticated client error report (OUT-01); a rolling public deployment keeps serving and published data survives the task replacement, 200 rather than the 202 that would mean it re-synced from S3 (OUT-02); a premium user keeps their instance through the outage (OUT-03); the assigned premium instance stopped (OUT-04) or terminated (OUT-05) out from under the user, with the DEGRADED snackbar, the row reconciliation and the instance-lost re-trigger's recovery asserted, and the standby pool healed before the run ends; and terminating a public instance gets it replaced, with an ECS task really placed on the replacement and no request dropped (ASG-01). Row 824's alarm is logged rather than asserted - whether a terminating target is ever counted `unhealthy` or goes straight to `draining` is undocumented, so HEALTH-27 carries that row instead |
| `23-subscription-lifecycle` | LC-06, LC-11..13, LC-16, LC-21..23 | the subscription-state half of the lifecycle rows, split out of `11-lifecycle` so it can run against deployed dev: the expiry captions (Renew / Expires / Expired), the expired-premium grace warning, the downgrade confirmation and its retention notice, the cancelled banner, the profile after cancellation, a renewal writing only a later period end, the post-grace Expired status with the 403 the assign route answers, and one real account deletion on a per-run throwaway. Plan and expiry are staged one statement at a time through `runSqlWriteOnDev`, and an `afterAll` restores the account to exactly what registration leaves, so no fabricated premium row outlives the run. Needs `TEST_LIFECYCLE_*` pointing at a dedicated account (LC-16 deletes the account it runs on) and, for LC-16 only, `FIREBASE_ADMIN_PYTHON`. **Writes to the shared dev RDS and currently runs in the default lane** - see the note below |
| `99-cleanup`       | CLEAN-01    | deletion of the account's `e2e-*` workspaces. Skipped unless `RUN_CLEANUP=1`; runs last so the groups above keep their fixtures |

## Coverage maps

Which test sheet rows this suite checks off, and what stays manual, is tracked
outside this README so the two sheet families stay separate:

| Document                                                                                                               | Covers                                                                                                                               |
| ---------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| [`infrastructure/documentation/RELEASE_TEST_COVERAGE.md`](../../infrastructure/documentation/RELEASE_TEST_COVERAGE.md) | `Araya-OptiNiSt Release Test Cases Template` (`BT-1xx` .. `BT-11xx`) - row by row, and what each spec group leaves manual            |
| [`infrastructure/documentation/SYSTEM_TEST_COVERAGE.md`](../../infrastructure/documentation/SYSTEM_TEST_COVERAGE.md)   | `Araya-Optinist System Test Cases Template` - a separate, larger scheme, mostly covered by jest and pytest rather than by this suite |

The release map is also where per-test data preconditions are written down (how
the dataview and Visualize tests get their records, and why that puts most of
them in the `@slow` lane).

## Troubleshooting

| Symptom                                                                    | Cause / fix                                                                                                                                                                                                                                                                                                              |
| -------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Nearly everything skips with "TEST_USER_EMAIL ... not set"                 | `e2e/.env` missing or not readable; env vars beat the file                                                                                                                                                                                                                                                               |
| `global-setup` login times out                                             | Dev server recompiling after idle/wake — retry; it attempts 3 logins with 60s each. Check `BASE_URL` actually serves the login form                                                                                                                                                                                      |
| Tests hang for hours / half the suite flakes at once                       | The machine slept mid-run. Use `caffeinate -i`, re-run                                                                                                                                                                                                                                                                   |
| Registration/login 500s on a fresh local DB                                | `subscription_plans` not seeded — see [Test environment](#test-environment)                                                                                                                                                                                                                                              |
| `Cannot find module` from a global npx playwright                          | `frontend/node_modules` was pruned (e.g. `yarn install` after a lockfile change) — `yarn install && npx playwright install chromium`                                                                                                                                                                                     |
| Premium account shows Free quota (5GB)                                     | `user_storage_usage.storage_quota_bytes` not updated — see the premium bootstrap SQL                                                                                                                                                                                                                                     |
| "Import sample data" does nothing, or the click times out on the menu `ul` | Two causes, both handled by `importSampleData`: the item is disabled off the Record tab, and a disabled MUI item passes pointer events to its parent list, so switch to the Record tab first; and the menu silently no-ops until `GET /workspace/{id}` has populated the store, so wait for the workspace name to render |
| Downloads never fire in new record tests                                   | `snakemake-download-link` / `nwb-download-link` testids are on hidden anchors — click the `IconButton` in the same table cell instead                                                                                                                                                                                    |
