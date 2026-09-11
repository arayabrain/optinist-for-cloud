# Release Test Sheets: Automated Coverage Map

## Executive Summary

- **Maps every row** of the `Araya-OptiNiSt Release Test Cases Template` sheets (`BT-1xx` .. `BT-11xx`) to the automated test that covers it, so a release tester only hand-verifies the rows marked manual
- **143 of 145 rows automated** - a green run checks off exactly the non-manual rows, provided the opt-in lanes its citations name were run
- **Mostly Playwright** - these sheets are the browser-testable release checklist, so nearly every entry is an e2e ID from `frontend/e2e/`; a few premium rows are covered by jest instead
- **Release sheets only** - the `Araya-Optinist System Test Cases Template` sheets are a separate, much larger scheme mapped in `infrastructure/documentation/SYSTEM_TEST_COVERAGE.md`
- **The two schemes do not correspond by trailing digits** - `BT-604` is "Premium profile display", not the System sheet's `6204` concurrency race
- **No sheet is fully manual.** 11 AWS Monitoring is automated by the `17-aws-health` e2e lane, which asserts against the live environment (`HEALTH_ENV=development|subscr`) rather than printing rows for review; the Stripe-dashboard tails of 08 Subscription and 09 Subscription Registration are covered by the `18-stripe-audit` lane

---

## How to read the tables

| Notation                     | Means                                                                                                                                                            |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `AUTH-01`, `DV-14`, `LC-12`  | a Playwright e2e ID from `frontend/e2e/`; run with `yarn test:e2e` (see that README for setup)                                                                   |
| `*.test.ts` / `*.test.tsx`   | a jest suite under `frontend/src/`, run by `make test_frontend`                                                                                                   |
| `test_*.py`, `TestSomeClass` | pytest under `studio/tests/`, run by `make test_backend` / `make test_lambda`                                                                                    |
| `@slow`                      | excluded unless `RUN_SLOW=1`; these are real workflow executions (5-10 min each)                                                                                  |
| `@prem`                      | the opt-in `15-premium-aws.spec.ts` lane (`RUN_PREMIUM_AWS=1`): real premium assignments against deployed dev                                                     |
| `S3-xx`                      | the opt-in `16-storage-aws.spec.ts` lane (`RUN_S3_AWS=1`): real-S3 asserts against deployed dev, no premium capacity                                              |
| `@disruptive`                | the opt-in `22-disruptive.spec.ts` lane (`RUN_DISRUPTIVE=1`): degrades the shared environment on purpose                                                          |
| (partial)                    | the test covers one side of the row only, with the S3 / Stripe / AWS / DB half still needing a deployed environment                                               |
| manual                       | no automated counterpart; follow the sheet's own Action / Expected columns                                                                                       |

Setup, credentials, and troubleshooting for the Playwright suite live in
`frontend/e2e/README.md`.

The CSV sheets carry `Tests: e2e`, `Tests: unit` and `Coverage` columns, and they
are the source of truth for the counts below: `FULL` and `PARTIAL` are automated
here, `MANUAL` is not. An e2e citation ending in `@slow` is gated behind
`RUN_SLOW=1`, so a default run does not check that row off, whatever its
`Coverage` label says. Re-derive from the sheets rather
than adjusting a total by hand.

The opt-in lanes never run per PR. `@slow` runs in the Monday `Weekly
Regression` (`gh workflow run e2e.yml --ref <branch>`); `@prem`, `@disruptive`
and the S3 lane are excluded even from that, because each performs genuine work
against the deployed dev environment, costs money and mutates shared
infrastructure, so running one is a deliberate manual call. A citation from
those lanes counts as automated coverage only for a round in which the lane was
actually run.

---

## Coverage labels

Every row in the CSV sheets carries a `Coverage` label, and that label - not this
document - is the source of truth. These are its exact meanings.

| Label     | Exact meaning                                                                                                                                                                                                                                                     | Counts as automated |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| `FULL`    | Every step of the row's Action and every clause of its Expected Result is asserted by a named automated test. A release tester runs nothing by hand for this row.                                                                                                  | Yes                 |
| `PARTIAL` | A named test asserts part of the row; the rest needs a surface no lane can reach - a Stripe-hosted page, a real inbox, real S3 / AWS / RDS state. The row's Notes cell says which half is left, and its `Test` cell in the map below is tagged `partial`.           | Yes                 |
| `MANUAL`  | No automated test names this row. A tester follows the sheet's own Action / Expected columns by hand.                                                                                                                                                              | No                  |

`PARTIAL` counting as automated is deliberate: the label narrows *what* is
covered, it does not withdraw the row, and a `PARTIAL` row still turns a lane red
if the covered half regresses. So `Automated` below is `FULL` + `PARTIAL`, and
`Manual` is `MANUAL`. A `Coverage` label says nothing about whether a default run
exercises the row: a `FULL` row whose citation ends in `@slow`, `@prem`,
`@disruptive` or an `S3-xx` id is checked off only by a round that ran that lane.

Every `FULL` row in these sheets carries a `Tests: e2e` citation, which is the
strongest grade a row can hold: it is proven through the same surface a release
tester would use, so the control, the route and the render are all covered. That
is a property of this scheme rather than an achievement - these sheets are the
browser-testable checklist by construction. The system sheets also carry `FULL`
rows proven below the browser;
[`SYSTEM_TEST_COVERAGE.md`](SYSTEM_TEST_COVERAGE.md) explains when that is the
right grade and when it leaves a blind spot.

Independently of that label, a row is either **open** or **decided**. This says
whether anyone has finished arguing the row, not how much of it is automated:

| State       | How it is set                                                                        | Means                                                                                              |
| ----------- | -------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| **Open**    | `Coverage` is `MANUAL` or `PARTIAL`, and the Notes cell carries no adjudication line  | Backlog. A test could plausibly be written, and nobody has written it.                             |
| **Decided** | The Notes cell carries an `Adjudicated`, `Re-graded` or `CONFIRMED-IMPOSSIBLE` line   | Somebody read the row, attempted it and retired it. The argument is in that row's own Notes cell.  |

A `FULL` row is neither: it is done. Which rows of a sheet are decided, and under
which cause, is in that sheet's own **Notes** section below. Three of the six
causes occur in these sheets:

| Cause                        | Why no lane can close it                                                                                                                                                                                                            |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CAPTCHA-gated                | The stimulus is a fresh hosted-checkout submit, which is CAPTCHA-gated (system sheet 02 row 231 is the confirmed-impossible anchor). The invariants each row checked are asserted continuously instead by the `18-stripe-audit` lane |
| Third-party surface          | The field, its validation and its rendering belong to Stripe's own hosted page or dashboard, so a test would assert Stripe's UI rather than ours                                                                                     |
| State the app cannot produce | No API or setup path reaches the state, or the paint has no deterministic window to assert in                                                                                                                                       |

The full six-cause vocabulary, and the backlog script that re-derives all of
this, are in [`SYSTEM_TEST_COVERAGE.md`](SYSTEM_TEST_COVERAGE.md).

---

## Coverage by sheet

Counted by the `Coverage` labels defined above. Re-derive from the sheets rather
than adjusting a total by hand.

| Sheet                        | Rows    | Automated | Manual |
| ---------------------------- | ------- | --------- | ------ |
| 01 Login & Auth              | 8       | 8         | 0      |
| 02 Workspace                 | 6       | 6         | 0      |
| 03 Workflow Execution        | 16      | 16        | 0      |
| 04 Visualize                 | 7       | 7         | 0      |
| 05 Record Management         | 9       | 9         | 0      |
| 06 Premium Features          | 15      | 15        | 0      |
| 07 Dataview                  | 21      | 21        | 0      |
| 08 Subscription              | 11      | 11        | 0      |
| 09 Subscription Registration | 25      | 23        | 2      |
| 10 Storage                   | 16      | 16        | 0      |
| 11 AWS Monitoring            | 11      | 11        | 0      |
| **Total**                    | **145** | **143**   | **2**  |

43 rows across all 19 sheets cite an `@slow` e2e test, 23 of them here: sheet 03's
BT-304..306 (the tutorial runs, `WF-04`..`WF-06`), BT-509 (`REC-07`, the NWB
download), BT-403 (`VIS-02`, the ROI overlay), BT-406 (`DV-12`) and the whole of
sheet 07 except BT-707, BT-708, BT-718 and BT-719. All of them need `RUN_SLOW=1`,
which only the weekly `e2e.yml` sets; dispatch it against a branch with `gh workflow run e2e.yml --ref <branch>`.
The common cause is one real workflow run: the sample data ships input files and
workflow YAML but no computed node outputs, and only success records reach the
dataview.

Both `Manual` rows are sheet 09's `BT-904` and `BT-905`, decided as
CONFIRMED-IMPOSSIBLE; see that sheet's Notes below.

---

## Suite groups: what each spec file automates

| Group (spec file)                            | IDs         | Automated                                                                                                                                                                                                                                                                                                                                                                                           | Stays manual                                                                                                                                                                           |
| -------------------------------------------- | ----------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Auth (`01-auth`)                             | AUTH-01..18 | login, logout, session persistence, unverified-email flow, header navigation, registration validation, registration DB rows (AUTH-18, local stack only)                                                                                                                                                                                                                                                                                               | -                                                                                                                                                                                      |
| Workspace (`02-workspace`)                   | WS-01..07   | create (with the new workspace's 0 Bytes), list columns and the listed row's own id and name, navigation to the id it clicked, storage reload with the refreshed count from the response, one refresh per session, delete                                                                                                                                                                             | -                                                                                                                                                                                      |
| Workflow (`03-workflow`)                     | WF-01..09   | sample data import, reproduce, tutorial runs (`@slow`), both run-validation messages (WF-07 uploads a real fixture so the algorithm-nodes branch is reachable), tab navigation judged on each panel's own content                                                                                                                                                                                     | the run-POST debounce end to end (its length is pinned by `RunButtons.test.tsx`)                                                                                                        |
| Record (`04-record`)                         | REC-01..09  | list, expand parameters, copy, delete (single and multi-select), workflow/snakemake/NWB downloads                                                                                                                                                                                                                                                                                                   | -                                                                                                                                                                                      |
| File handling (`05-file-handling`)           | FILE-01..04 | file tree dialog, wildcard filter, check-all, sidebar toggle                                                                                                                                                                                                                                                                                                                                        | -                                                                                                                                                                                      |
| Uploads & node dialogs (`10-uploads`)        | UPL-01..07 | CSV param dialog, HDF5 and MAT structure dialogs, image/HDF5/MAT upload appears in inputs, selecting a data path inside a MAT file | S3-side verification |
| Dataview (`06-dataview`)                     | DV-01..18 | table display, ID/name column-menu filters, sort, pagination, inputs/outputs/details dialogs, public access, public/private API auth, image/ROI thumbnails asserted per cell by alt text, publish/unpublish + public listing, bulk publish/unpublish with confirmation, the public workspace filter (DV-17), concurrent public reads returning identical payloads (DV-18)                                                                                                                                                            | DB/S3 sync verification (the sync-status UI is covered by `SyncStatusView.test.tsx`) |
| Subscription (`07-subscription`)             | SUB-01..15 | free and premium plan UI state, per-card feature lists against a mocked catalogue (SUB-15), `/thanks` access guard, invoice page sections and two differing invoice rows, cancel and reactivate, the checkout and billing-portal hand-offs | Stripe-hosted checkout and portal pages, DB/Stripe dashboard verification |
| Storage (`08-storage`)                       | STO-01..04 | no-warning login under quota (asserted on the limit-warning response, not just an absent modal), the over-quota modal from a fulfilled alert (STO-04), dedicated and shared premium assignment on login | S3 verification, over-quota states (see the lifecycle group), auto-refresh tracing |
| Visualize (`09-visualize`)                   | VIS-01..05  | sidebar workspace/workflow info, Cell-ROI image plot, frame playback, second plot type, ROI editor open/cancel                                                                                                                                                                                                                                                                                      | Edit ROI commit (OK mutates ROI data and starts a processing run)                                                                                                                      |
| Lifecycle (`11-lifecycle`, local stack only) | LC-01..23 | free baseline, upgrade, over-quota warning modal (110%), usage-high indicator (95%), storage reload reset, expired-premium grace warning, overdue acknowledgment modal, downgraded-free over-quota warning, run blocked/warned at quota, expiration captions, cancel-subscription dialog, cancelled banner, inactivity warning + Stay Active (fake clock), 2h auto-release beacon, account deletion | real Stripe upgrade/downgrade, real S3 usage, reactivation/cancel API calls (the spec drives plan/expiry/usage in the docker DB and mocks premium assignment for the inactivity tests) |
| Admin (`12-admin`, local stack only)         | ADMIN-01..12 | admin login reaching the Account Manager, the eight list columns, the menu entry's presence for an admin and absence for an operator, the non-admin redirect, Edit / Add / Delete modals opening on the right values, every Cancel path asserted as no write request, the own-row Delete and Proxy SignIn suppression, the Proxy SignIn and Edit Subscription confirmations, the dashboard's own admin tile, the list's sort and rows-per-page controls, and a confirmed deletion of a throwaway account through the grid | completing a proxy sign-in (it would leave the worker signed in as another user), the DataGrid filter panel, creating a user for real (the create path is `test_users_admin.py`), and the per-step writes a deletion makes (`test_user_deletion.py`) |

Not automated at all (out of browser-test scope): premium instance
provisioning, AWS monitoring.

---

## Row-by-row map

Maps every row of the "Araya-OptiNiSt Release Test Cases Template" to its
automated test. Subjects are included so rows stay findable if the sheet is
renumbered.

AUTH-09/10/11 (registration empty fields / password mismatch / password
complexity) have no release-sheet row - they cover the System-sheet
registration validation cases.

---

## 01 Login & Auth (BT-101..108)

| Sheet row | Subject                      | Test    |
| --------- | ---------------------------- | ------- |
| BT-101    | Successful login             | AUTH-01 |
| BT-102    | Invalid credentials          | AUTH-02 |
| BT-103    | Empty fields validation      | AUTH-03 |
| BT-104    | Unverified email login       | AUTH-04 |
| BT-105    | Successful logout            | AUTH-05 |
| BT-106    | Session persistence          | AUTH-06 |
| BT-107    | Logo navigation              | AUTH-07 |
| BT-108    | Dashboard button (logged in) | AUTH-08 |

### Notes

**Decided rows: none.** All 8 rows of this sheet are `FULL`.

---

## 02 Workspace (BT-201..206)

| Sheet row | Subject                | Test  |
| --------- | ---------------------- | ----- |
| BT-201    | Create new workspace   | WS-01 |
| BT-202    | Workspace list display | WS-02 |
| BT-203    | Access workspace       | WS-03 |
| BT-204    | Storage refresh        | WS-04 |
| BT-205    | Dataview access        | WS-05 |
| BT-206    | Delete workspace       | WS-06 |

### Notes

**Decided rows: none.** All 6 rows of this sheet are `FULL`.

---

## 03 Workflow Execution (BT-301..316)

| Sheet row | Subject                        | Test                                                                    |
| --------- | ------------------------------ | ----------------------------------------------------------------------- |
| BT-301    | Access workflow page           | WF-01                                                                   |
| BT-302    | Import sample data             | WF-02                                                                   |
| BT-303    | Reproduce workflow from record | WF-03                                                                   |
| BT-304    | Run Tutorial 1 workflow        | WF-04 `@slow` (by-uid RUN)                                              |
| BT-305    | Run Tutorial 2 workflow        | WF-05 `@slow` (RUN ALL, full compute)                                   |
| BT-306    | Run Tutorial 3 workflow        | WF-06 `@slow` (RUN ALL, full compute)                                   |
| BT-307    | Run without algorithm nodes    | WF-07 (message asserted verbatim); `RunButtons.test.tsx` |
| BT-308    | Run without input file         | WF-07; WF-08; `RunButtons.test.tsx`                                     |
| BT-309    | Run button cooldown            | `RunButtons.test.tsx` (cooldown + debounce pinned); WF-08 (snackbar dedupe) |
| BT-310    | Tab navigation                 | WF-09                                                                   |
| BT-311    | File tree display              | FILE-01 (the sample file by name, with the shape read off it)            |
| BT-312    | File filter with wildcards     | FILE-02                                                                 |
| BT-313    | Check all / uncheck all        | FILE-03                                                                 |
| BT-314    | Sidebar toggle                 | FILE-04                                                                 |
| BT-315    | HDF5 file dialog               | UPL-02 (tree contents) + UPL-08 (a dataset path is selectable)           |
| BT-316    | CSV parameter dialog           | UPL-01                                                                  |

### Notes

**Decided rows: none.** All 16 rows of this sheet are `FULL`.

**Validation-message ordering.** Both validations assign the same variable and the input-file one is
assigned last, so on a fresh workspace it always wins. WF-07 therefore uploads
`sample_data/dev_mouse2p_short_image.tiff` into the default image node first,
which clears that branch and makes the algorithm-nodes message the one under
test. It used to accept either message with a regex, which BT-307 and BT-308
could both satisfy without the algorithm-nodes copy existing at all.

---

## 04 Visualize (BT-401..407)

| Sheet row | Subject                          | Test                                                                           |
| --------- | -------------------------------- | ------------------------------------------------------------------------------ |
| BT-401    | Open Visualize tab               | VIS-01                                                                         |
| BT-402    | Confirm workflow info in sidebar | VIS-01                                                                         |
| BT-403    | Add Cell ROI plot                | VIS-02                                                                         |
| BT-404    | Play visualization image         | VIS-03                                                                         |
| BT-405    | Add additional plot type         | VIS-04                                                                         |
| BT-406    | Image thumbnail display          | DV-12                                                                          |
| BT-407    | Run Edit ROI                     | VIS-05 (editor open + Cancel); VIS-06 `@slow` (the OK commit) |

### Notes

**Decided rows: none.** All 7 rows of this sheet are `FULL`.

**The data-backed tests need a real run, so they are `@slow`.**
`sample_data/tutorial` ships the input files plus workflow metadata
(`experiment/workflow/snakemake.yaml`) only, with no computed node outputs, and
the dataview lists success records only. So VIS-03/04 need no run, because they
plot the shipped `sample_mouse2p_image.tiff`, but anything reading a node output
does: VIS-02 and VIS-05 select `cell_roi` (a `suite2p_roi` output), and the
dataview needs a success record plus thumbnails, which only a completed run
writes. `ensureCompletedTutorialRun` mints that state once per run by rerunning
the imported Tutorial1 by uid, which is why `06-dataview`'s Private Dataview
group and `REC-07` are tagged `@slow`. Two gotchas the helper absorbs: loading a
finished experiment fires a phantom "Workflow finished" snackbar (it anchors on
the run POST instead), and the success record is written shortly AFTER the
finished signal (DV-12 reload-polls the grid).

---

## 05 Record Management (BT-501..509)

| Sheet row | Subject                 | Test   |
| --------- | ----------------------- | ------ |
| BT-501    | Access record page      | REC-01 |
| BT-502    | View workflow details   | REC-02 |
| BT-503    | Copy single record      | REC-03 |
| BT-504    | Copy multiple records   | REC-08 |
| BT-505    | Delete single record    | REC-04 |
| BT-506    | Delete multiple records | REC-09 |
| BT-507    | Download workflow file  | REC-05 (the payload, not just the download event) |
| BT-508    | Download Snakemake file | REC-06 (the payload; a Snakemake *config*, not a Snakefile) |
| BT-509    | Download NWB file       | REC-07 |

### Notes

**Decided rows: none.** All 9 rows of this sheet are `FULL`.

**BT-509 costs a real run, so it is `@slow`.** An NWB file exists only after
a completed workflow, and global setup deletes the `e2e-*` workspaces at the
start of every run, so REC-07 calls `ensureCompletedTutorialRun` itself rather
than skipping on the resulting 404. That is a real
snakemake execution, so the row's citation is `@slow` and checked off by
`RUN_SLOW=1` runs only.

---

## 06 Premium Features (BT-601..615)

| Sheet row                                   | Subject                                               | Test                                                                                                                                                                                                                                                           |
| ------------------------------------------- | ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| BT-604                                      | Premium profile display                               | SUB-05                                                                                                                                                                                                                                                         |
| BT-605                                      | Premium subscription page                             | SUB-04 (status + Downgrade offered); LC-12 (the downgrade dialog) |
| BT-611                                      | Inactivity warning                                    | LC-14 (frontend lifecycle via fake clock; backend heartbeat/CloudWatch stays manual)                                                                                                                                                                           |
| BT-613                                      | Auto-release after 2h inactivity                      | LC-15 (frontend half); `TestCheckPremiumUserInactivity`; `TestCleanupStaleAssignments`; real AWS stays manual |
| BT-615                                      | Instance release on browser close                     | `PremiumLifecycleIntegration.test.tsx`; `TestSoftReleaseUserAssignment`; `TestFinalizeExpiredPendingReleases`; PREM-04 / PREM-02 (**@prem**). LC-15 contributory only |
| BT-612                                      | Stay Active button                                    | LC-14 (dismiss + timer reset; DB heartbeat verification stays manual)                                                                                                                                                                                          |
| BT-601/602                                  | assignment snackbars                                  | `PremiumNotificationManager.test.tsx`; STO-02; STO-09; PREM-01 (**@prem**) |
| Lifecycle chain (assign, release, reassign) | end-to-end premium routing lifecycle                  | LC-17 (fake-clock companion to `PremiumLifecycleIntegration`); PREM-02 (**@prem**: the real assign / release / reassign chain)                                                                                                                                                    |
| BT-614                                      | Instance release on logout                            | `useLogout.test.ts`; LC-17; PREM-02 (**@prem**, release line from CloudWatch) |
| BT-607, 608, 609                            | premium workspace, sample import, run on the dedicated instance | PREM-07 (**@prem**: workspace, sample import, RUN ALL on the dedicated instance) |
| BT-603                                      | two premium users assigned concurrently               | PREM-06 (**@prem**; partial - needs a round granting two dedicated instances) |
| BT-606                                      | subscription row in the deployed RDS                  | PREM-09 (**@prem**: the sheet's own query over SSM against the real RDS) |
| BT-610                                      | concurrent workflows on one dedicated instance         | PREM-08 (**@prem**: three concurrent runs on one dedicated instance) |

### Notes

**Decided rows: none.** All 15 rows of this sheet are `FULL`.

**Numbering.** The `BT-6xx` rows above are the release
sheet, a separate scheme from the System test sheet. The System sheet's
`600-x` cases correspond to the CSV `62xx` cases (`600-4` <-> `6204`
concurrency); `BT-6xx` does NOT line up by trailing digits (`BT-604` is
"Premium profile display", not the `6204` concurrency race). Those `600-x` /
`62xx` cases are mapped, with their L1/L2/contract/L3 levels, in
[`infrastructure/documentation/SYSTEM_TEST_COVERAGE.md`](../../infrastructure/documentation/SYSTEM_TEST_COVERAGE.md).

---

## 07 Dataview (BT-701..721)

| Sheet row | Subject                                | Test                                                                                                                                              |
| --------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| BT-701    | Private Dataview Table Display         | DV-01                                                                                                                                             |
| BT-702    | Publish Toggle Display                 | DV-02                                                                                                                                             |
| BT-703    | Publish Experiment                     | DV-14 (toggle + public listing; DB/S3 sync verification manual)                                                                                   |
| BT-704    | Unpublish Experiment                   | DV-14                                                                                                                                             |
| BT-705    | Bulk Publish                           | DV-15                                                                                                                                             |
| BT-706    | Bulk Unpublish                         | DV-15                                                                                                                                             |
| BT-707    | Public Dataview Table Display          | DV-09                                                                                                                                             |
| BT-708    | Public Dataview Unauthenticated Access | DV-10                                                                                                                                             |
| BT-709    | UID Filter                             | DV-03 (column-menu filter)                                                                                                                        |
| BT-710    | Name Filter                            | DV-13 (column-menu filter)                                                                                                                        |
| BT-711    | Workspace Filter (Public Only)         | DV-17 (filters `/public` by workspace, plus the empty case) |
| BT-712    | Sort by Column Header                  | DV-04                                                                                                                                             |
| BT-713    | Change Page Size                       | DV-05                                                                                                                                             |
| BT-714    | Inputs Dialog Display                  | DV-06                                                                                                                                             |
| BT-715    | Outputs Dialog Display                 | DV-07                                                                                                                                             |
| BT-716    | Workflow Details Dialog Display        | DV-08                                                                                                                                             |
| BT-717    | Close Dialog                           | DV-08                                                                                                                                             |
| BT-718    | Pending Sync Status Display            | `SyncStatusView.test.tsx` (status branches + retry ceiling; the S3 sync stays manual) |
| BT-719    | Manual Retry from Sync Error           | `SyncStatusView.test.tsx` (Retry re-fires the fetch); S3-04 `@slow` (RUN_S3_AWS=1, real S3 error state) |
| BT-720    | Image Thumbnail Display                | DV-12                                                                                                                                             |
| BT-721    | ROI Thumbnail Display                  | DV-12                                                                                                                                             |

### Notes

**Decided rows: 1 of 21.** Each was read, attempted and retired with the
reason in its own Notes cell in the sheet. Causes are defined under *Coverage
labels* above.

| Cause | Rows |
| ----- | ---- |
| State the app cannot produce | BT-718 |

**Dataview data preconditions.** The records the data-dependent tests
need are minted once per run before any of them execute - a fast no-op rerun
of the imported Tutorial1 plus a record copy of it (~2 min; only Tutorial1's
rerun is a reliable no-op, Tutorial2's recomputes CaImAn locally and fails).
Publishing requires a cloud bucket on the account, so on a local stack the
suite sets a placeholder `remote_bucket_name` attribute on the test user
(deployed users have real buckets; the S3 sync itself stays manual).

---

## 08 Subscription (BT-801..811)

| Sheet row   | Subject                            | Test                                                                                  |
| ----------- | ---------------------------------- | ------------------------------------------------------------------------------------- |
| BT-801      | Free Plan card display             | SUB-01 (each assertion scoped to its plan card; tax caption on Premium only) |
| BT-802      | Free account status display        | SUB-02 (status read via `account-plan-name`; no expiry caption) |
| BT-803      | No invoice for Free user           | SUB-03                                                                                |
| BT-804      | Premium plan status display        | SUB-04                                                                                |
| BT-805      | Premium account status display     | SUB-05                                                                                |
| BT-806      | Expiration date text               | LC-11 (exact caption per state; sheet says "renews on" but the UI text is "Renew on") |
| BT-807      | Verify Premium in the DB           | `test_subscription_state_transitions.py::TestSuccessfulCheckoutWritesPremium` (partial - not the deployed RDS) |
| BT-810      | Stripe ID registered in the DB     | `test_checkout_session_tax_config.py` (partial - the docker DB); AUDIT-01 / AUDIT-07 |
| BT-808, BT-809, BT-811 | Stripe dashboard verification | AUDIT-01 / AUDIT-05 |

### Notes

**Decided rows: none.** All 11 rows of this sheet are `FULL`.

---

## 09 Subscription Registration (BT-901..925)

| Sheet row                                 | Subject                                                              | Test                                                                         |
| ----------------------------------------- | -------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| BT-901                                    | Upgrade transitions to checkout                                      | SUB-13 (route-mocked; the hosted page stays manual); CHECKOUT-01 (**opt-in**, `RUN_CHECKOUT_PROBE=1`) |
| BT-906                                    | Prevent direct access to /thanks                                     | SUB-06                                                                       |
| BT-907                                    | Subscription page updated to Premium                                 | SUB-04 (standing premium account)                                            |
| BT-908                                    | Account Profile updated to Premium                                   | SUB-05 (standing premium account)                                            |
| BT-915 / BT-916                           | Invoice page sections; invoice row data                              | SUB-08 / SUB-09 (mocked billing data; a real invoice from a real checkout stays manual) |
| BT-917                                    | Initiate downgrade                                                   | LC-12 (confirmation modal + 30-day retention notice)                         |
| BT-918                                    | Cancel downgrade (click No)                                          | LC-12                                                                        |
| BT-919 / BT-921                           | Confirm cancellation; execute reactivation                           | SUB-11 / SUB-12 (the banner and its clearing, cancel/reactivate APIs mocked; the real Stripe side stays manual) |
| BT-920                                    | Reactivation option                                                  | LC-13 (banner + Continue Plan visible; clicking it is Stripe-backed, manual) |
| BT-922                                    | Expired premium user buttons                                         | LC-06 (Upgrade + Manage both visible)                                        |
| BT-925                                    | Delete test user account                                             | LC-16 (per-run throwaway account; active=0 + deletion records completed)     |
| BT-909..914, BT-923                       | DB / Stripe verification after a purchase                            | AUDIT-01..08; run the scan with `--cases release` for the BT-keyed report |
| BT-902                                    | The hosted checkout page loads its form                              | CHECKOUT-02 (**opt-in**: the real hosted page, its fields and our amounts) |
| BT-903..905, 924                          | completing a card payment on the hosted page                         | manual - Stripe gates the submit behind a CAPTCHA |

### Notes

**Decided rows: 10 of 25.** Each was read, attempted and retired with the
reason in its own Notes cell in the sheet. Causes are defined under *Coverage
labels* above.

| Cause | Rows |
| ----- | ---- |
| CAPTCHA-gated | BT-904, BT-905, BT-909..BT-914, BT-924 |
| Third-party surface | BT-923 |

---

## 10 Storage (BT-1001..1016)

| Sheet row                 | Subject                                 | Test                                                                                       |
| ------------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------ |
| BT-1001                   | Free user login - no warning            | STO-01                                                                                     |
| BT-1005                   | Upload image data                       | UPL-03 (UI half - file appears in inputs; S3 verification manual)                          |
| BT-1006                   | Upload HDF5 file                        | UPL-04 (UI half - file appears in inputs; S3 verification manual)                          |
| BT-1007                   | Premium user login                      | STO-02                                                                                     |
| BT-1010                   | Storage limit exceeded warning on login | LC-03 (premium) / LC-08 (free)                                                             |
| BT-1011                   | Handle Later button                     | LC-03 (dismisses, stays on dashboard)                                                      |
| BT-1012                   | Manage Files button                     | LC-08 (redirects to /workspaces)                                                           |
| BT-1013                   | Cannot run workflow when over limit     | LC-09                                                                                      |
| BT-1014                   | Storage 90-99% warning on RUN           | LC-10 (snackbar + run not blocked)                                                         |
| BT-1015                   | Manual storage refresh                  | WS-04                                                                                      |
| BT-1016                   | Storage values update after delete      | LC-05 (delete ballast -> Reload clears warning)                                             |
| BT-1002..1004, 1008, 1009 | S3-side verification                    | S3-01 / S3-02 / S3-03 (**RUN_S3_AWS=1**) for the free account, and their premium twins S3-21 / S3-22 / S3-23, the same bodies behind `RUN_PREMIUM_AWS=1` — S3-22 and S3-23 hold a real assignment and read `x-user-tier: premium` off their own import and run requests, while S3-21 is API-only and assigns nothing; PREM-07 (**@prem**) covers the premium run's outputs from the premium lane's own side |

### Notes

**Decided rows: none.** All 16 rows of this sheet are `FULL`.

---

## 11 AWS Monitoring (BT-1101..1111)

| Sheet row           | Subject                             | Test                                                |
| ------------------- | ----------------------------------- | --------------------------------------------------- |
| BT-1110             | Public Dataview access + auth guard | DV-11 (API half); HEALTH-18 (the same contract through the deployed ALB) |
| BT-1109             | premium assign/release CloudWatch lines | PREM-01 / PREM-02 (**@prem**; partial - checks 1-2 from CloudWatch) |
| BT-1101..1108, 1111 | AWS CLI / console probes            | `HEALTH-01`..`HEALTH-19` (read-only; `HEALTH_ENV`, `BASE_URL`). BT-1107 asserts fatal markers, not any ERROR; BT-1101's TLS half needs an https `BASE_URL` |

### Notes

**Decided rows: none.** 10 of the 11 rows are `FULL`.

**Open: `BT-1107`.** It is `PARTIAL` with no adjudication line in its Notes
cell, so the backlog script still counts it as open. Its system-sheet twin,
`1205`, is adjudicated on exactly the same argument: asserting the absence of
ERROR lines on a shared environment would be permanently red.
