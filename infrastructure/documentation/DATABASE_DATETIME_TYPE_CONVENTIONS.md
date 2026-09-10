# Database Datetime Type Conventions: DATETIME vs TIMESTAMP Inconsistency

## Executive Summary

- The schema uses **both `DATETIME` and `TIMESTAMP`** for datetime columns, with no single enforced convention.
- Root cause is a **dual convention**: `TimestampMixin` (in `studio/app/common/models/base.py`) produces `DATETIME`, while the subscription/billing models (`studio/app/common/models/subscription.py`) declare `TIMESTAMP` explicitly.
- The same logical column (`created_at`, `updated_at`) resolves to **different types across tables** (roughly core tables = `DATETIME`, billing tables = `TIMESTAMP`), and some tables mix both types internally.
- This is a **known, pre-existing limitation**. It is recorded here for awareness; a full unification is not scheduled as of 2026-07 (see Status).
- The `alembic check` CI guard (see `Related Work`) enables `compare_type`, so any future datetime type drift between a model and the migrations is now detected.

---

## Status

- **As of:** 2026-07, recorded alongside the alembic-check column-type guard added during the model/migration drift remediation (issue #723, #779).
- **State:** Known inconsistency, documented, not scheduled for change.
- **Scope of any fix:** Schema-wide. Unifying the type would touch many tables and require a data migration (`ALTER` per column), so it is treated as a deliberate future initiative, not incidental work.
- **Guardrail in place:** `compare_type` is enabled in the Alembic environment, so models and migrations can no longer silently disagree on column type going forward.

---

## Root Cause: A Dual Convention

Two datetime conventions coexist in the models:

| Origin | Implementation | Resulting DB type |
|--------|----------------|-------------------|
| `TimestampMixin` in `base.py` | `sa_column_kwargs` only; type inferred from the `datetime` annotation | `DATETIME` |
| Subscription/billing models in `subscription.py` | `Column(TIMESTAMP, ...)` declared explicitly | `TIMESTAMP` |

Tables inheriting `TimestampMixin`: `experiment_records`, `background_tasks`, `users`, `workspaces`.

`background_tasks` overrides the mixin and redeclares `created_at` / `updated_at` as `TIMESTAMP`, so the mixin convention is already broken in at least one place.

---

## Current State Audit

### `created_at`

| Type | Tables |
|------|--------|
| `TIMESTAMP` | `background_tasks`, `subscription_plans`, `subscription_users`, `subscription_providers`, `subscription_user_accounts`, `subscription_user_purchases`, `storage_operations`, `user_deletion_records`, `subscription_audit_log`, `taxes`, `user_preferences` |
| `DATETIME` | `experiment_records`, `users`, `workspaces` (via `TimestampMixin`), `roles`, `user_roles`, `organization`, `workspaces_share_users`, `user_storage_usage` |

### `updated_at`

| Type | Tables |
|------|--------|
| `TIMESTAMP` | `background_tasks`, `subscription_providers`, `subscription_user_accounts`, `subscription_user_purchases`, `user_deletion_records`, `taxes`, `user_preferences` |
| `DATETIME` | `experiment_records`, `users`, `workspaces` (via `TimestampMixin`), `user_storage_usage` (`last_updated`), `subscription_users` |

### Intra-table mixes

Several tables use both types. A loose pattern exists (audit columns `TIMESTAMP`, business/event datetimes `DATETIME`) but it is not applied consistently:

| Table | `TIMESTAMP` columns | `DATETIME` columns |
|-------|---------------------|--------------------|
| `background_tasks` | `created_at`, `updated_at` | `started_at`, `completed_at` |
| `storage_operations` | `created_at` | `completed_at` |
| `subscription_users` | `created_at`, `last_synced` | `updated_at`, `expiration`, `deletion_processed_at` |
| `user_storage_usage` | (none) | `created_at`, `last_updated`, `last_full_scan` |

---

## Decision Record

### `subscription_users.updated_at` (resolved)

This column was the single datetime **type drift** between a model and the migrations: the model declared `TIMESTAMP` while the migration-built column is `DATETIME`. It was also inconsistent inside the billing cluster, where the sibling tables (`subscription_providers`, `subscription_user_accounts`, `subscription_user_purchases`) use `TIMESTAMP`.

**Decision:** Align the model to the deployed database type (`DATETIME`) rather than migrate the database to `TIMESTAMP`.

**Rationale:**

1. `DATETIME` is a defensible type for `updated_at` (no 1970-2038 range limit, no implicit timezone conversion) and matches the core tables (`users`, `workspaces`, `experiment_records`).
2. It requires no schema change to the deployed database, keeping the change within the scope of the CI-guard work.
3. Migrating to `TIMESTAMP` would be a data migration with timezone and range considerations, and only makes sense as part of a deliberate, schema-wide unification (see below), not as a side effect.

The model change locks the model and the deployed schema together so `compare_type` passes.

---

## Future Options (If Unification Is Scheduled)

Neither option is planned. They are recorded so whoever picks this up has the tradeoffs.

### Option A: Standardize on `DATETIME`

- Migrate the `TIMESTAMP` columns (mostly the billing/subscription tables) to `DATETIME`.
- Change `TimestampMixin` to stay `DATETIME` (already its effective behavior).
- Pros: no 2038 range limit, no implicit timezone conversion, matches the larger core-table set.
- Cons: large number of `ALTER`s on billing tables; loses UTC-normalization semantics where they were intended.

### Option B: Standardize on `TIMESTAMP`

- Migrate the `DATETIME` columns (core tables via the mixin, plus assorted others) to `TIMESTAMP`.
- Change `TimestampMixin` to declare `TIMESTAMP` explicitly.
- Pros: UTC-normalized storage, timezone-aware, matches the newer billing tables.
- Cons: imposes the 2038 range limit and timezone-conversion behavior across the whole schema; larger blast radius (the mixin backs the highest-traffic core tables).

Any unification must:

- Decide the convention for the `TimestampMixin` first, since it backs the core tables.
- Handle business/event datetime columns (`expiration`, `started_at`, `completed_at`, etc.) explicitly rather than assuming they follow the audit-column choice.
- Ship as its own PR with per-column `ALTER` migrations and data-migration verification.

---

## Do Not Reintroduce Drift

- Do not change `subscription_users.updated_at` back to `TIMESTAMP` in the model without a matching migration; that reintroduces the exact drift the guard was added to catch.
- When adding a new datetime column, follow the type already used by that table's sibling columns, and provide a migration that matches the model.

---

## Related Work

- `studio/alembic/env.py` enables `compare_type` (detects column-type drift) and intentionally leaves `compare_server_default` off (documented blind spot).
- The `alembic check` CI guard runs `alembic upgrade head` on a throwaway database and then `alembic check` to assert the models and migrations agree; see `docker-compose.alembic-check.yml`, the `alembic_check` target in `Makefile`, and `.github/workflows/alembic-check.yml`.
- Issue #723, #779 - the drift remediation during which this convention issue surfaced.

---

## References

- `studio/app/common/models/base.py` - `TimestampMixin` (source of the `DATETIME` convention)
- `studio/app/common/models/subscription.py` - billing models (explicit `TIMESTAMP` convention)
- `studio/app/common/models/experiment.py` - `background_tasks` overrides the mixin to `TIMESTAMP`
- `studio/alembic/env.py` - Alembic environment (`compare_type` enabled)
