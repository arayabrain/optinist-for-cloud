# Background Job Architecture: Dedicated ECS Service

## Executive Summary

- **Dedicated ECS service** runs most periodic background jobs separate from API processes
- **Standalone cleanup worker** on each free-tier instance handles local data deletion (instance-specific EBS)
- **Single-instance execution** with one task and one worker eliminates multi-worker coordination on the background service
- **Four jobs** handle experiment sync (5 min), data cleanup (60 min), storage reconciliation (60 min), and expiration lifecycle (24 hr)
- **Validate-then-trigger pattern** validates in S3 on the background service, triggers file downloads on API instances via ALB
- **Safety-first cleanup** verifies S3 backups and checks for active workflows before deleting; on the owning (instance-filtered) worker, "no local data" is treated as a completed cleanup, while an unfiltered run keeps the DB record so data on another instance is never orphaned

---

## Key Architectural Principles

1. **Separation of Concerns**
   - Background jobs run in a dedicated ECS service, not in API workers
   - API services disable their scheduler via `DISABLE_BACKGROUND_SCHEDULER=1`
   - Prevents duplicate job execution across multiple API workers

2. **Single-Instance Execution**
   - Background service runs exactly one ECS task with one uvicorn worker
   - Eliminates need for distributed locking or leader election
   - Replaces the previous file-based locking mechanism

3. **Validate-Then-Trigger Pattern**
   - Background service validates experiments in S3 using `ListObjectsV2` (cheap, no downloads)
   - On success, calls ALB to trigger file downloads on an API instance
   - Background service has no shared filesystem; actual file serving happens on API instances

4. **Same Codebase, Different Configuration**
   - Background service uses the same Docker image as API services
   - Key differences: `DISABLE_BACKGROUND_SCHEDULER`, `UVICORN_WORKERS`, `ALB_DNS_NAME`
   - Jobs reuse the same ORM models, S3 controllers, and utilities as the API

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────────────────────────────┐
│ ECS Cluster                                                                    │
├────────────────────────────────────────────────────────────────────────────────┤
│                                                                                │
│  ┌──────────────────────────────┐  ┌─────────────────────────┐                 │
│  │ Free-Tier Instance (ASG)     │  │ Background Service      │                 │
│  │                              │  │ (1 task, 1 worker)      │                 │
│  │  uvicorn         cleanup     │  │                         │                 │
│  │  (WORKERS=5)     _worker     │  │ → Sync, reconciliation, │                 │
│  │  +-----------+  +---------+  │  │   expiration jobs       │                 │
│  │  | FastAPI   |  | APSched |  │  │ → Orphaned DB cleanup   │                 │
│  │  | (API only,|  | Cleanup |  │  │ → No local data access  │                 │
│  │  | no sched) |  | filtered|  │  │                         │                 │
│  │  +-----------+  | by      |  │  └─────────────────────────┘                 │
│  │                 | inst_id |  │                                               │
│  │                 +---------+  │                                               │
│  │  Local EBS: user workspace   │                                               │
│  │  data (NOT shared)           │                                               │
│  └──────────────────────────────┘                                               │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
```

| Responsibility | Free-Tier Instance | Background Service |
|----------------|--------------------|--------------------|
| Serve HTTP requests | Yes (uvicorn) | No |
| Run background scheduler | No (disabled) | Yes (sync, reconciliation, expiration) |
| Run cleanup worker | Yes (standalone process) | No (orphaned DB cleanup only) |
| Local data deletion | Yes (instance-specific EBS) | No (no local data access) |
| File downloads from S3 | Yes (serves users) | No (no shared filesystem) |

---

## Implementation Details

### Background Jobs

| Job | Interval | Where | Purpose |
|-----|----------|-------|---------|
| `PublishedExperimentSyncJob` | 5 min | Background service | Validate S3 files, update DB status, trigger API download via ALB |
| `DataCleanupJob` | 60 min | Free-tier cleanup worker + Background service | Delete local data for logged-out free users (cleanup worker); clean orphaned DB records (background service) |
| `StorageReconciliationJob` | 60 min | Background service | Reconcile incremental storage tracking with S3 |
| `ExpirationLifecycleJob` | 24 hr | Background service | Handle subscription expiration lifecycle (data deletion for overdue users) |

Jobs are defined in `studio/app/common/core/background/` and registered in `studio/__main_unit__.py` (background service) or `studio/cleanup_worker.py` (free-tier cleanup worker).

### PublishedExperimentSyncJob

**File:** `studio/app/common/core/background/sync_job.py`
**Purpose:** Validate published experiments exist in S3 and trigger file pre-caching on API instances
**Input:** Queries `ExperimentRecord` rows with `local_sync_status` in (`pending`, `error`)
**Output:** Updates `local_sync_status` to `synced` or `error`; triggers proactive download via ALB
**Calls:** `validate_experiment_in_s3()` -> `_trigger_proactive_download()` -> ALB `POST /system-internal/sync-experiment/{workspace_id}/{unique_id}`

The background service has no shared filesystem and no port mappings. The job validates experiments in S3 using `ListObjectsV2` (no file downloads), updates DB status, then calls the ALB to trigger file downloads on an API instance.

- Limits: 50 experiments per run (`VALIDATION_LIMIT`), 10 concurrent validations (`VALIDATION_CONCURRENCY`)
- Retries: 3 attempts per run with exponential backoff; persistent failure alert after 9 total failures across runs

**File downloads on API instances happen through three paths:**

- **Proactive** (`_trigger_proactive_download()`) -- background job triggers via ALB after validation. Pre-caches on one ALB-selected instance; other instances rely on startup sync or on-demand download.
- **Startup sync** (`PublishedExperimentSyncJob.run_startup_sync()`) -- downloads missing experiments at container boot. Runs on all non-standalone containers but only useful on API instances.
- **On-demand** (`public_reproduce_experiment()` in `studio/app/common/routers/dataview.py`) -- downloads from S3 when a user requests an experiment not yet local

### DataCleanupJob

**File:** `studio/app/common/core/background/cleanup_job.py`
**Purpose:** Delete local workspace data for free users who have logged out
**Input:** Queries `FreeUserAssignment` rows where `logged_out_at` > 1 hour ago, `active_workflow_count` = 0, and `instance_id` matches current host
**Output:** Removes local input/output directories, deletes `FreeUserAssignment` record

**Dual execution model:**

- **Free-tier cleanup worker** (`studio/cleanup_worker.py`): Standalone process on each free-tier instance. Runs `DataCleanupJob.run()` every 60 minutes, filtered by `INSTANCE_ID`. Deletes local data from the instance's own EBS. Started by `cloud-startup.sh` when `ENABLE_LOCAL_CLEANUP=1`.
- **Background service**: Also runs `DataCleanupJob.run()`, but `_get_users_for_cleanup()` returns no users (no assignment matches the background service's instance ID). Only `_handle_orphaned_data()` is useful here — it cleans stale DB records for terminated instances.

Safety checks before deletion:

- Only processes users with `active_workflow_count = 0`
- Verifies S3 backup exists via `head_object` before deleting experiment outputs
- Re-checks for user re-login before each workspace and after cleanup completes
- Returns `False` when no local data is found (prevents premature DB record deletion when running on wrong instance)
- `_handle_orphaned_data()` only runs on background service (skipped when `ENABLE_LOCAL_CLEANUP=1`)

### StorageReconciliationJob

**File:** `studio/app/common/core/background/storage_reconciliation_job.py`
**Purpose:** Reconcile incremental storage tracking with actual S3 usage
**Input:** Queries `UserStorageUsage` records with activity since last scan
**Output:** Updates `storage_usage_bytes` with fresh S3 scan values, logs drift warnings

- Processes users in batches to prevent OOM
- Rate-limits S3 API calls between users
- Logs warnings for significant drift

---

## Edge Case Handling

### 1. S3 Validation Persistent Failure

**Problem:** An experiment consistently fails S3 validation across multiple job runs.

**Solution:** Escalating retry with alerting:
- 3 retry attempts per run with exponential backoff (1s, 2s)
- Retry count tracked in memory across runs (`_retry_counts`)
- After 9 total failures (`MAX_PERSISTENT_RETRIES`), publishes `PersistentSyncFailure` CloudWatch metric
- Experiment stays in `error` status for manual investigation

### 2. User Logs Back In During Cleanup

**Problem:** A free user logs back in while `DataCleanupJob` is deleting their workspace data.

**Solution:** Multiple re-login checks:
- Checks `logged_out_at` before processing each workspace
- Re-checks after cleanup completes, before removing the `FreeUserAssignment` record
- If re-login detected, aborts without marking as cleaned

### 3. Active Workflows During Cleanup

**Problem:** A long-running workflow (2+ hours) is still active when cleanup runs.

**Solution:** Workflow count protection:
- DB query filters for `active_workflow_count = 0`
- Final `_verify_no_active_workflows()` check after cleanup, before removing assignment record

### 4. Orphaned DB Records from Terminated Instances

**Problem:** An EC2 instance is terminated (e.g., ASG scale-in), destroying its local EBS. Stale `FreeUserAssignment` records remain in the database.

**Solution:** `DataCleanupJob._handle_orphaned_data()` (runs on background service only):
- Queries all `FreeUserAssignment` records on each run
- Checks if assigned instance still exists via EC2 `describe_instances`
- For terminated instances: removes the `FreeUserAssignment` record and closes `InstanceUsageLog` (DB-only cleanup — local EBS is already destroyed)

### 5. No Local Data Found During Cleanup

**Problem:** Cleanup runs but finds no local data for the user (e.g., wrong instance, or data already deleted).

**Solution:** `_cleanup_user_data()` tracks a `data_found` flag:
- Returns `False` when no input/output directories exist locally
- Prevents `_mark_cleaned()` from deleting the `FreeUserAssignment` DB record prematurely
- Logged as a warning for observability

### 6. Background Service Stops Running

**Problem:** The background ECS task crashes or is terminated.

**Solution:** Automatic recovery with alerting:
- ECS service `desired_count=1` ensures automatic task restart
- `RunningTaskCount < 1` CloudWatch alarm triggers after 2 evaluation periods (10 min)

---

## Monitoring and Metrics

### CloudWatch Alarms

| Alarm | Metric | Threshold | Description |
|-------|--------|-----------|-------------|
| `subscr-background-task-stopped` | `RunningTaskCount` | < 1 | Background jobs not running |
| `subscr-background-cpu-high` | `CpuUtilized` | > 400 (80% of 512 CPU) | Jobs may be delayed |
| `subscr-background-memory-high` | `MemoryUtilized` | > 600 (80% of 768 MB) | Memory pressure |

### Custom Metrics (Namespace: `OptiNiSt/BackgroundJobs`)

| Metric | Published By | Unit |
|--------|-------------|------|
| `ExperimentsSynced` | `PublishedExperimentSyncJob` | Count |
| `SyncErrors` | `PublishedExperimentSyncJob` | Count |
| `SyncErrorRate` | `PublishedExperimentSyncJob` | Percent |
| `PersistentSyncFailure` | `PublishedExperimentSyncJob` | Count |
| `DataCleanupCount` | `DataCleanupJob` | Count |
| `CleanupErrors` | `DataCleanupJob` | Count |
| `CleanupKept` | `DataCleanupJob` | Count |

> `CleanupErrors` = actionable failures (unexpected exception, or data retained
> because its S3 backup could not be verified). `CleanupKept` = benign
> keep-the-record outcomes (no local data on this instance, or the user returned)
> — deliberately **not** counted as errors. No CloudWatch alarm is wired to this
> namespace yet; these metrics are the intended signals for such alarms.

### Logs

View logs at: `/ecs/subscr-background-optinist-cloud-taskdef`

---

## Configuration

| Variable | Purpose | Background Service | Free-Tier Instance |
|----------|---------|-------------------|-------------------|
| `DISABLE_BACKGROUND_SCHEDULER` | Controls scheduler startup in FastAPI lifespan | `0` (enabled) | `1` (disabled) |
| `ENABLE_LOCAL_CLEANUP` | Starts standalone cleanup worker process | Not set | `1` |
| `INSTANCE_ID` | EC2 instance ID for cleanup filtering | Set by `cloud-startup.sh` | Set by `cloud-startup.sh` |
| `UVICORN_WORKERS` | Number of uvicorn workers | `1` | `5` |
| `S3_DEFAULT_BUCKET_NAME` | S3 bucket for experiments | Required | Required |
| `INTERNAL_API_SECRET` | Shared secret for internal API calls | Required (caller) | Required (receiver) |
| `ALB_DNS_NAME` | ALB hostname for proactive download calls | Required | Not set |

---

## Key Functions Reference

### Background Scheduler (`studio/app/common/core/background/scheduler.py`)

| Function | Purpose |
|----------|---------|
| `BackgroundScheduler.initialize()` | Validate S3 config and create scheduler |
| `BackgroundScheduler.add_job()` | Register a job with an interval |
| `BackgroundScheduler.start()` | Start the APScheduler event loop |

### PublishedExperimentSyncJob (`studio/app/common/core/background/sync_job.py`)

| Function | Purpose |
|----------|---------|
| `PublishedExperimentSyncJob.run()` | Periodic sync: validate pending experiments in S3 |
| `PublishedExperimentSyncJob.run_startup_sync()` | One-time sync at container boot: download missing experiments |
| `_trigger_proactive_download()` | Call ALB to trigger file download on an API instance |
| `validate_experiment_in_s3()` | Check required files exist in S3 via `ListObjectsV2` (in `S3StorageController`) |

### Cleanup Worker (`studio/cleanup_worker.py`)

| Function | Purpose |
|----------|---------|
| `main()` | Entry point: starts APScheduler with DataCleanupJob, handles SIGTERM/SIGINT |

### DataCleanupJob (`studio/app/common/core/background/cleanup_job.py`)

| Function | Purpose |
|----------|---------|
| `DataCleanupJob.run()` | Periodic cleanup: delete data for logged-out free users |
| `_get_current_instance_id()` | Return `INSTANCE_ID` env var or `"local"` for dev |
| `_get_users_for_cleanup()` | Query users filtered by instance_id (when not `"local"`) |
| `_cleanup_user_data()` | Delete local dirs; returns `False` if no data found |
| `_verify_s3_backup_exists()` | Check S3 for critical files before local deletion |
| `_handle_orphaned_data()` | Remove DB records for terminated instances (background service only) |
| `_cleanup_orphaned_assignment()` | DB-only cleanup: remove assignment + close usage log |

### StorageReconciliationJob (`studio/app/common/core/background/storage_reconciliation_job.py`)

| Function | Purpose |
|----------|---------|
| `StorageReconciliationJob.run()` | Periodic reconciliation: full S3 scan and DB update |
| `reconcile_user_storage()` | Single-user reconciliation for manual triggers |

---

## AWS Resources

### Background Service

**File:** `infrastructure/terraform/background_service.tf`

- **Instance**: `t3.micro` (minimal for background jobs)
- **Task Definition**: 512 CPU, 768 MB memory, single worker
- **ECS Service**: `desired_count=1`
- **No ALB target**: Does not serve HTTP traffic, but calls the ALB to trigger API downloads
- **CloudWatch Alarms**: Task stopped, CPU high, memory high

### Free-Tier Instances (API + Cleanup Worker)

**File:** `infrastructure/terraform/compute.tf`

- Autoscaling task definition sets `DISABLE_BACKGROUND_SCHEDULER=1` and `ENABLE_LOCAL_CLEANUP=1`
- `cloud-startup.sh` starts `python -m studio.cleanup_worker &` before uvicorn when `ENABLE_LOCAL_CLEANUP=1`
- Cleanup worker runs in a separate process from uvicorn (avoids UVICORN_WORKERS=5 multi-scheduler issue)

### Premium Instances

**File:** `infrastructure/terraform/compute.tf`

- Premium task definition sets `DISABLE_BACKGROUND_SCHEDULER=1`
- No cleanup worker (premium users have S3-backed storage, not local EBS)

### Lambda Functions

Infrastructure management Lambdas remain separate from background jobs:

| Lambda | Purpose | Why Lambda |
|--------|---------|------------|
| `free_manager` | Scale ECS/ASG for free tier | Event-driven (ASG lifecycle) |
| `premium_manager` | Manage premium instance routing | Event-driven (tier changes) |
| `common_user_manager` | User management operations | Event-driven |

These Lambdas respond to AWS infrastructure events and only need boto3, not the full Studio codebase.

---

## Testing

1. **API logs**: "Background scheduler disabled by DISABLE_BACKGROUND_SCHEDULER env var"
2. **Background service logs**: "Background scheduler initialized" and job execution logs
3. **Free-tier cleanup worker logs**: "cleanup_worker starting (instance: i-xxx, interval: 60min)"
4. **CloudWatch**: Background service running with task count = 1
5. **No duplicates**: Jobs execute once per interval (not per API worker)
6. **Unit tests**: `pytest studio/tests/app/common/core/background/test_cleanup_job.py`
