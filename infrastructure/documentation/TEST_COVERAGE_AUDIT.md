# Test Coverage Audit: Red-Team Review of the Sheet Mapping

**Date:** 2026-08-03
**Scope:** all 291 rows across the 19 manual test-case CSVs whose "Test exists" column named a test
**Method:** six independent adversarial reviewers, one per sheet group, each instructed to assume the mapping over-claims. Every reviewer read the row's Action/Expected, then the cited test body, and judged existence, semantic match, and regression value.

> **Status, 2026-08-06.** This is a dated snapshot, kept for the reasoning rather
> than the row list. Since it was written the system-test automation stack
> (#788 -> #796 -> #797 -> #801) has closed almost all of it. What is resolved:
> both false-positive tests and the `nwb.py` 200-on-missing-file bug behind
> BT-509; `SyncStatusView.test.tsx`; the `MagicMock` DB-state rows (pattern 4)
> now compile their statements and assert the predicates; the skip gate
> (recommendation 5) landed as assertions replacing the `hasDataRows` guards,
> plus a hard failure when `11-lifecycle` or `12-admin` cannot run locally; and
> the MEDIUM relabel backlog is applied as the sheets' `PARTIAL` verdict, each
> with its uncovered half stated. What is **not**: the probe-ladder timings
> behind 6236, and the e2e half of 336/340. Recommendation 7 is moot, the PDR was
> removed once its work packages shipped. Current counts live in
> `SYSTEM_TEST_COVERAGE.md` and `RELEASE_TEST_COVERAGE.md`, which are re-derived
> from the sheets, not from this document.

## Executive Summary

- **Every cited test exists.** Across 291 rows there was not one phantom file, class, ID, or function. The mapping's problem is not fabrication
- **41 rows were materially wrong** (HIGH): the cited test asserts something different, asserts nothing, or cannot fail if the behavior regresses
- **85 rows over-claim** (MEDIUM): coverage is real but narrower than the row, usually missing a `(partial)` label
- **34 rows have been corrected to `No`** as a result. System coverage falls from 207/409 to **173/409**; Release from 82/104 to **75/104**
- **Two test suites have zero regression value and should not be cited anywhere** - they re-implement the logic under test inside the test file
- **The audit found two false-positive tests** - tests that pass while the product is broken. These are worse than no test, because they suppress the manual check as well
- **Five failure patterns recur across reviewers who never saw each other's work**, which suggests systemic habits rather than isolated mistakes

---

## The five recurring patterns

### 1. Tests that assert their own mocks or constants

The most common and most dangerous class. The test never imports the production
code path it claims to cover.

| Test                                                                 | What it actually does                                                                                                                                                                                                |
| -------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PremiumHeartbeatRetry.test.ts`                                      | Imported nothing but `@jest/globals`. Declared its own `HEARTBEAT_MAX_RETRIES = 3` and re-implemented the retry loop inside the test, then asserted against those copies. Deleting the real retry loop left it green |
| `PremiumSleepDetection.test.ts`                                      | Defines a private `SleepDetectionSimulator` reimplementing `useSleepDetection`, then asserts a `jest.fn()` declared in the test was called                                                                           |
| `test_user_deletion.py::test_contract_firebase_deleted_blocks_login` | Never attempts a login. Asserts `firebase_deleted is True` and that the patched mock was called                                                                                                                      |
| `test_main_unit_startup.py::TestStartupSyncLeaderElection`           | The test body _is_ the production branch (`with lock as acquired: if acquired: await run_startup_sync()`). The real lifespan is never invoked                                                                        |
| `test_workflow_tracking.py` (most cases)                             | Asserts `session.execute.called` / `commit.assert_called_once()`. `_get_user_tier` is patched out - which is the free-vs-premium logic rows 540-544 exist to cover                                                   |

**Recommendation:** delete or rewrite `PremiumHeartbeatRetry.test.ts` and
`PremiumSleepDetection.test.ts` against the real context. A suite that cannot
fail is a maintenance liability that also inflates the suite count. **Done
2026-08-03** for the two premium suites: the retry suite now drives the real
provider, and the sleep suite was deleted in favour of `useSleepDetection.test.ts`
plus a provider-level wake-wiring test.

### 2. Category errors - the cited component is not the one that implements the behavior

Rows 717/718 and BT-718/719 describe a sync-status icon, error alert, and Retry
button. That is `SyncStatusView.tsx` plus `useSyncRetry.ts`, consumed by
`WorkflowDetailsView` and `BaseNodesView`. **Neither has any test.** The cited
`ImagePlotSimple.test.tsx` covers a per-plot download button in a different
component fed by a different redux slice.

Similarly, `InactivityWarning.tsx` had **no test file at all**, yet rows 6228,
6229 and 6230 all asserted its behavior (`Stay Active`, `recordActivity()`,
`performLogout()`, the "Session Expired" copy). Closed 2026-08-03 by
`InactivityWarning.test.tsx` plus the cross-tab dismiss case in
`PremiumInactivityActivity.test.tsx`.

### 3. Wrong scenario cited

| Row            | Claim                                                                                              | Reality                                                                                                                                                                                                                                                                                           |
| -------------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| BT-601, 06/601 | `STO-02` covers the "Please wait while your dedicated premium resource is being prepared" snackbar | `STO-02` asserts `Premium instance assigned successfully` - which is row **602**'s string. Two reviewers found this independently. The 601 string appears in exactly one place in the repo and no test greped for it. Closed by WP16 - `PremiumNotificationManager.test.tsx` now asserts the copy |
| 04/430         | `LC-08` covers "Handle later returns to dashboard"                                                 | `LC-08` clicks **Manage Files** and asserts `/workspaces`. The Handle-later assertion is in `LC-03`                                                                                                                                                                                               |
| 04/429         | `LC-08` covers expired-premium-in-grace over quota                                                 | `LC-08` sets `plan_id = 1`, so it lands in the free-over-quota branch, not the grace branch                                                                                                                                                                                                       |
| 6208           | `LC-14` covers tab-close then reopen within the 120s grace                                         | `LC-14` is a fake-clock inactivity-warning test. Zero overlap. Closed by WP16 - `TestRestorePendingReleaseTransaction` plus the FE re-login case                                                                                                                                                  |
| 6219           | the sweep job covers "the assignment is left dangling"                                             | the sweep job asserts `release_premium_user(hard=True)` - the opposite of the dangle. It is the sweep's grace window that leaves the row alone; closed by WP16 - `TestExpiryLeavesTheAssignmentDangling`                                                                                          |
| 02/288         | `TestPaymentFailureTracking` covers Stripe-side past-due state                                     | the test asserts our local `sync_status`; Stripe is never called                                                                                                                                                                                                                                  |
| 01/126         | `TestRecoverStaleWorkflowCounts` covers the row's cleanup assertions                               | it tests the **Lambda** implementation; the row invokes `studio/app/.../workflow_count_recovery.py`, which has zero test references                                                                                                                                                               |

### 4. `MagicMock` databases make DB-state assertions unobservable

Rows asserting `publish_status = 1`, `local_sync_status = 'pending'`, `version`
incremented, or `scheduled_downgrade = 0` cite tests that build the `update()`
statement against a `MagicMock` and never inspect it. Any SQL at all satisfies
`execute.called`. Affected: 07/704, 07/719, 07/726, 02/265, 02/272, 01/122,
03/336, 6234.

The repo already has the right pattern for this -
`test_cleanup_job.py::TestGetUsersForCleanupInstanceFilter` compiles the
statement and asserts on its text. Reuse it.

### 5. Non-running lanes presented as coverage

A skip is indistinguishable from a pass in a summary line.

| Lane                                        | Why it does not gate a PR                                                                                                                                       |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_crud_users_context.py`                | **12 of 14 tests** are `@pytest.mark.skip("Requires integration test with real DB")`, covering every subscription-tier and storage state                        |
| `test_checkout.py::TestCheckoutIntegration` | `check_api_running` fixture calls `pytest.skip()` unless a live API answers `/docs`. This is why the routers lane reports exactly 3 skips                       |
| `test_premium_lock_integration.py`          | `skipif` on `RUN_PREMIUM_LOCK_IT`, which is set only in `docker-compose.premium-lock-it.yml`, never in `tests.yml`                                              |
| All Playwright IDs                          | `e2e.yml` runs on a **weekly cron and manual dispatch only** - never on a PR                                                                                    |
| `WF-04` / `WF-05` / `WF-06`                 | `@slow`, excluded unless `RUN_SLOW=1`. **Correction (2026-08-06):** `e2e.yml` does set it, so these run on that workflow's weekly cron and manual dispatch, never on a PR. The sheets mark those rows `OPT-IN` rather than automated |
| `REC-07` and `06-dataview`'s Private Dataview group | Also `@slow` as of 2026-08-06. Both need a success record, which costs a real snakemake run, and keeping them in the default lane made every `yarn test:e2e` a 30-minute-plus compute. Their 21 release rows and 13 system rows are `OPT-IN` |
| ~20 e2e rows                                | Guarded by `test.skip(!(await hasDataRows(page)))` or `skipWithoutCreds()`. A regression that empties the dataview converts eight mapped rows from FAIL to SKIP. **Closed 2026-08-06:** `hasDataRows` and `goToWorkspacesWithData` are deleted and those specs assert their preconditions |

---

## False-positive tests (highest priority)

Two tests pass while the product is broken. These are worse than no coverage,
because the row also stops being hand-checked.

1. **BT-509 / REC-07, NWB download.** `nwb.py` returns **HTTP 200 with body
   `false`** when no `.nwb` exists. The button is enabled because `hasNWB: true`
   ships in `sample_data/tutorial/output/tutorial1/experiment.yaml`, while no
   `.nwb` file ships at all, and no DB `ExperimentRecord` exists for imported
   samples so the "DB is authoritative" override never fires. The test receives a
   download event for a 5-byte `false` blob and passes. **This is a product bug
   as well as a test bug** - the endpoint should 404.
2. **BT-403 / VIS-02, Cell-ROI plot.** `addImagePlot` already awaited
   `.js-plotly-plot` before the ROI is selected, so re-asserting it proves
   nothing, and `text=cell_roi` is the select's own displayed value. A failed
   `getRoiData` does not hide the plot - `ImagePlot` gates rendering on the image
   error only. "Cell ROI displayed correctly" is unverified.

## A documented precondition that is factually false

`RELEASE_TEST_COVERAGE.md` and `helpers.ts` both state that the sample-data
import ships with pre-computed outputs, so the dataview and Visualize tests need
no `@slow` run. `git ls-files sample_data` shows the tutorial outputs are
metadata YAML only - no node output dirs, no JSON, no NWB. `import_sample_data`
copies those YAMLs, and global setup deletes the `e2e-*` workspace each run, so
snakemake must recompute.

Consequences: `WF-04` is not the documented "seconds" no-op;
`06-dataview`'s `beforeEach` budgets 600s while the inner `runTutorial` waits
840s, so the data mint times out on a real compute; and `VIS-02..05` plus `DV-12`
silently depend on an earlier mint rather than shipped outputs, so running
`-g VIS` in isolation has no outputs at all.

**This claim originated in PR #727 and I propagated it into the coverage doc
without verifying it.** **Closed 2026-08-06:** both places now say the run is
real, the `beforeEach` budget is 900s, and the tests that depend on the mint are
tagged `@slow` instead of paying for it on every default run.

---

## Corrections applied

34 rows moved to `No`, plus 8 citation repairs. Applied to the CSVs and to both
coverage documents.

| Sheet               | Rows set to `No`                         |
| ------------------- | ---------------------------------------- |
| Release 03 Workflow | BT-307, BT-309                           |
| Release 05 Record   | BT-509                                   |
| Release 06 Premium  | BT-601, BT-614                           |
| Release 07 Dataview | BT-718, BT-719                           |
| System 01           | 126                                      |
| System 02           | 272, 288, 299                            |
| System 03           | 338                                      |
| System 04           | 413, 423, 429, 430                       |
| System 05           | 516, 528, 538, 539, 540, 543, 544        |
| System 06           | 601, 603, 608                            |
| System 06-2         | 6203, 6208, 6209, 6213, 6219, 6228, 6230 |
| System 07           | 717, 718, 721, 722, 724                  |
| System 08           | 817, 818, 822                            |

Citation repairs: BT-602 gains `STO-02 (mocked)`; BT-613 gains
`TestCheckPremiumUserInactivity`; BT-615 relabelled "beacon fired only";
BT-403/406/407 relabelled `(partial)`; 03/339 re-pointed to
`test_delete_user_success`; 08/815 and 08/816 re-pointed to
`test_structured_outputs.py`; 08/823 drops the tautological citation and keeps
`test_startup_leader.py`; 08/812 and 08/829 gain `test_instance_mode_routers.py`;
6204 annotated "opt-in, NOT run in CI"; 6232 gains `unreachableMachine.test.ts`.

## Not yet applied: the MEDIUM relabel backlog

85 rows need a `(partial)` label or a narrowed claim. They are real coverage, so
they are not urgent in the way the HIGH rows were, and applying them is a
judgement call per row rather than a mechanical edit. The per-row detail is in
the six reviewer transcripts. The clusters:

- **Release:** BT-301, 305, 306, 308, 315, 402, 703/704, 707, 712, 713, 714, 715, 717, 721, 801, 802, 803, 805, 1110
- **System 01/02:** 122, 125, 202, 203, 204, 205, 209, 210, 211, 213, 233, 235, 254, 255, 256, 258, 261, 262, 265, 287, 300, 302, 304
- **System 03/04/05:** 336, 339, 401, 418, 419, 422, 426, 436, 437, 439, 442, 448, 507, 510, 519, 533, 534, 535, 536, 537, 541, 542
- **System 06/06-2:** 604, 6201, 6205, 6206, 6207, 6210, 6212, 6218, 6221, 6227, 6229, 6232, 6234
- **System 07/08/09:** 701, 702, 703, 705, 707, 708, 709, 710, 711, 715, 716, 719, 720, 723, 725, 726, 812, 813, 828, 829, 830, 920

## Uncited tests that should be mapped

The audit found existing tests that cover rows better than the cited ones:

| Row              | Better test                                                                                                   |
| ---------------- | ------------------------------------------------------------------------------------------------------------- |
| 6203, 6208, 6210 | `test_premium_manager.py::TestHeartbeatRestoresPendingRelease` - the actual pending-release restore mechanism |
| 6232             | `unreachableMachine.test.ts` "does not poll when tab is not leader" - the actual non-leader gate. **Superseded 2026-08-06** by `PremiumNonLeaderTab.test.tsx`, which runs the provider itself as leader and as follower; the row now cites both |
| 08/815, 08/816   | `test_structured_outputs.py` on-demand-sync cases (PR #650's own regression tests)                            |
| 07/702           | `DataviewRecords.test.tsx` owner-column and publish-button private/public cases                               |
| 04/448           | `test_s3_storage_monitor.py` critical/danger threshold cases, for the alert half                              |
| BT-613           | `test_common_user_manager.py::TestCheckPremiumUserInactivity` - the DB + ALB + alarm teardown                 |

## Sheet defects found

Beyond the four already corrected:

1. **08/823's Expected is wrong about the product.** `__main_unit__.py` logs
   "Startup sync task scheduled" unconditionally for every public task, outside
   the lock. Only "Startup sync deferred to leader task" is leader-dependent.
2. **05/530's Subject says "Column resizing"** while its Action and Expected
   describe the sidebar toggle.
3. **02/203 contradicts 01/104.** Both cover the name-minimum-length rule; 104 is
   honestly `No` while 203 claimed coverage.
4. **02/256 expects a warning emoji** that does not exist - the component uses a
   MUI `Alert severity="warning"`.
5. **Pytest node IDs in the sheets omit the class segment** (for example
   `test_webhook.py::test_subscription_updated_mirrors_scheduled_downgrade`), so
   they cannot be pasted into `pytest` as written.
6. **6219's `[FLAG: codebase]` note is stale.** It states that no code path links
   expiration to `release_premium_user` (traced 2026-04-30). Since then the
   `customer.subscription.deleted` handler gained `_release_premium_assignment`,
   which hard-releases. The dangle the row describes now applies only to the
   DB-only / lost-webhook trigger the row itself uses, which is exactly what the
   expiration sweep job exists to catch. Corrected in the sheet on 2026-08-03:
   the note is now an `[UPDATE:]` recording the reconciliation, and the row's
   cleanup-path list gained the webhook release and the hourly sweep.

## Recommendations

1. **Fix the two false-positive tests, and the `nwb.py` 200-on-missing-file bug
   behind BT-509.** A test that passes on a broken product is the only category
   here that actively causes harm.
2. **Delete or rewrite the two tautological suites.** They contribute a false
   sense of a large suite. Done for the two premium ones.
3. **Write `SyncStatusView.test.tsx` and `InactivityWarning.test.tsx`.** Between
   them they legitimately close five rows that currently read as covered.
   `InactivityWarning.test.tsx` is written; `SyncStatusView.test.tsx` is not.
4. **Correct the sample-data precondition** in `helpers.ts` and
   `RELEASE_TEST_COVERAGE.md`, and reconcile the 600s/840s timeout inversion.
5. **Add a skip gate.** For a release sign-off sheet, a skipped test proves
   nothing. Either fail the run when a High-priority mapped test skips, or print
   a per-row skip summary the tester must reconcile against the sheet.
6. **Treat `(partial)` as requiring a stated uncovered half.** Most MEDIUM
   findings are rows where `(partial)` was doing unbounded work. The premium
   tables already model the honest style - copy it.
7. **Fold the new gaps into the PDR.** 34 rows moved to `No`; several are cheap
   (the `SyncStatusView` and `InactivityWarning` suites) and belong in the
   existing work packages rather than a new one.
