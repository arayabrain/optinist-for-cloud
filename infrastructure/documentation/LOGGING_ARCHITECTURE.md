# Logging: System-Wide Log Configuration and Monitoring

## Executive Summary

- **Dual-output logging** routes application logs to both stdout (CloudWatch via awslogs driver) and local rotating files (`logs/studio.log`) with 365-day retention
- **Per-user log isolation** uses hashed `client_id` injected via ASGI middleware and propagated across process boundaries (subprocess, snakemake) for multiuser filtering
- **Six CloudWatch log groups** capture ECS container output (free/premium/background) and Lambda execution (free-manager, premium-manager/cleanup, common-user-manager), all with explicit retention policies
- **CloudWatch Agent** on EC2 instances publishes host-level metrics (CPU, memory, I/O wait, load average) to the `CWAgent` namespace for infrastructure monitoring
- **Centralized dashboard** (`subscr-optinist-monitoring`) aggregates 19 alarms across ECS, EC2, RDS, EFS, and ALB with threshold-based alerting
- **Python logging** always uses YAML config defaults (optinist=DEBUG, snakemake=DEBUG) so all log levels reach CloudWatch
- **`LOG_LEVEL` environment variable** controls which levels appear in the **frontend log viewer**, not Python logging
- **`--log-level` CLI argument** provides local developer override for Python logging (useful for reducing console noise locally)
- **ECS production default** is `LOG_LEVEL=INFO`, meaning the frontend hides DEBUG logs while CloudWatch captures everything
- **Frontend "ALL" filter** excludes DEBUG logs; the DEBUG filter option is hidden when `LOG_LEVEL` >= INFO

---

## Key Architectural Principles

1. **Log Once, Route Everywhere**
   - Application writes to Python logging; the ECS awslogs driver and file handler fan out to CloudWatch and local disk
   - No application code writes directly to CloudWatch

2. **Two Separate Concerns**
   - Python logging level (controlled by YAML config) determines what is **written** to logs/CloudWatch
   - `LOG_LEVEL` env var determines what the **frontend log viewer** displays to users
   - CloudWatch always has the full picture; the frontend filters for readability

3. **User Identity in Every Log Line**
   - `ClientIdLoggingMiddleware` extracts the Firebase/JWT uid, hashes it to a 16-char `client_id`, and stores it in a `ContextVar`
   - `ClientIdFilter` injects `client_id` and `ecs_task_id` into every log record automatically
   - Snakemake scripts receive `client_id` via config dict; subprocesses receive it via kwargs

4. **Non-Breaking Defaults**
   - When `LOG_LEVEL` is unset, the frontend shows all levels including DEBUG
   - YAML files remain the source of truth for Python logging levels
   - Invalid `LOG_LEVEL` values fall through to showing all levels

5. **Frontend Filtering**
   - `GET /logs/level` endpoint returns available filter levels based on `LOG_LEVEL`
   - The "ALL" filter in the log viewer excludes DEBUG (use CloudWatch directly for DEBUG)
   - The DEBUG filter option is hidden from the UI when `LOG_LEVEL` >= INFO

6. **Infrastructure Logs Are Separate from App Logs**
   - EC2 setup logs (`/var/log/ecs-setup.log`) and app setup logs (`/var/log/app-setup.log`) live on the host filesystem
   - Lambda functions log to their own `/aws/lambda/*` log groups
   - Application logs flow through the ECS container log driver

7. **Child Process Consistency**
   - Env vars are inherited by child processes (snakemake workers, ProcessPoolExecutor)
   - Each child calls `AppLogger.init_logger()` which uses YAML defaults for logging
   - `--log-level` CLI arg still overrides Python logging for local development

---

## Logging Level Policy

This section defines **when to use each log level**. All new logging calls
must follow these rules. Existing calls that violate these rules should be
corrected when the surrounding code is modified.

### Level Definitions

#### CRITICAL

System is unusable or data integrity is compromised.

- Partial operations that leave the system in an inconsistent state
  (e.g., Firebase account deleted but DB record remains)
- Unrecoverable startup failures

Expected frequency: near-zero in healthy systems.

#### ERROR

An operation **failed and cannot proceed**. The caller will receive an error
response, or a job item will be skipped/retried.

Use ERROR when:
- A database write/read fails unexpectedly
- An external API call fails (S3, Stripe, Firebase) and no fallback exists
- A workflow execution fails
- A business-logic invariant is violated

Do NOT use ERROR when:
- The system handles the failure gracefully (use WARNING)
- The failure is expected in normal operation (use WARNING or DEBUG)

#### WARNING

Something **unexpected happened but the system recovered** or degraded
gracefully. A human should review these periodically but no immediate action
is required.

Use WARNING when:
- A resource is missing but the code returns early or uses a fallback
  (e.g., missing workflow directory, missing experiment config)
- A non-critical cleanup operation fails (e.g., temp file deletion after
  successful computation)
- A caught exception represents a client-side issue, not a server bug
  (e.g., validation error -> 400/401, expired auth token)
- A platform detection or optional feature probe fails
- A caught exception that was handled but may indicate a deeper issue

Do NOT use WARNING when:
- The condition is the normal/happy path (use DEBUG or INFO)
- The operation succeeded (use INFO)

#### INFO

A **significant business event or operational milestone** occurred. INFO is
the default production-visible level in the frontend log viewer.

Use INFO when:
- A high-level operation starts or completes (workflow run, background job,
  experiment copy/delete)
- A business event occurs (user login, subscription created/renewed/cancelled,
  email sent)
- Infrastructure initializes (cloud services connected, S3 bucket created,
  DB connection verified)
- A job-level summary is produced ("Processed N items", "Cleanup complete")

Do NOT use INFO when:
- Logging per-item details in a batch operation (use DEBUG)
- Logging internal function parameters or return values (use DEBUG)
- Logging step-by-step traces through a multi-step process (use DEBUG)
- Logging sensitive data: auth tokens, verification links, webhook secrets
  (use DEBUG -- these should only appear when explicitly requested)

Rule of thumb: **one INFO message per high-level operation**. If a loop
body contains `logger.info()`, it almost certainly should be `logger.debug()`.

#### DEBUG

**Diagnostic detail** for developers investigating issues. Always captured
by CloudWatch but hidden from the frontend log viewer when `LOG_LEVEL=INFO`.

Use DEBUG when:
- Logging step-by-step progress through a multi-step process (webhook
  processing, checkout flow, S3 sync)
- Logging per-item details in batch operations (per-file download,
  per-experiment cleanup, per-user reconciliation)
- Logging function parameters, return values, or intermediate state
- Logging cache hits/misses, skip decisions, no-op conditions
- Logging sensitive data that aids debugging (verification links, password
  reset links, webhook signatures, Lambda response bodies)
- Logging the happy/normal path of a check ("no limit warning" = user is
  within limits = nothing to report at higher levels)

### Decision Flowchart

```
Is the system in an inconsistent/unusable state?
  YES -> CRITICAL

Did the operation fail and the caller will get an error?
  YES -> ERROR

Did something unexpected happen but the system handled it?
  YES -> WARNING

Is this a significant business event or operational milestone?
  YES -> INFO

Everything else (traces, per-item details, parameters, sensitive data)
  -> DEBUG
```

### Anti-Patterns

These are the most common mistakes found during the log-level audit. Avoid
them in new code and fix them when modifying existing code.

| Anti-Pattern | Correct Level | Example |
|---|---|---|
| Caught exception logged as INFO | WARNING | `"Failed to detect Apple Silicon: {e}"` |
| Happy-path / no-problem-found logged as WARNING | DEBUG | `"No limit warning for user {id}"` |
| Successful recovery logged as WARNING | INFO | `"Bucket recovery on login: {bucket}"` |
| Per-item batch detail logged as INFO | DEBUG | `"Deleting input directory: {dir}"` |
| Internal state dump logged as INFO | DEBUG | `"Lambda response body: {body}"` |
| Sensitive credentials logged as INFO | DEBUG | `"Verification link: {url}"` |
| Graceful early-return logged as ERROR | WARNING | `"'{path}' does not exist"` (returns early) |
| Non-critical cleanup failure logged as ERROR | WARNING | `"Failed to cleanup memmap files"` |

### Logger Usage

All modules must use `AppLogger.get_logger()` from
`studio.app.common.core.logger`. Do not use `logging.getLogger()` directly.

```python
# Correct
from studio.app.common.core.logger import AppLogger
logger = AppLogger.get_logger()

# Incorrect -- do not use
import logging
logging.getLogger().info(...)
```

This ensures all log output passes through the centralized configuration
(YAML-based levels, `ClientIdFilter`, concurrent file handler, `LOG_LEVEL`
frontend filtering).

---

## Architecture Overview

```
┌──────────────────────────────────────────────────────────┐
│ Application (uvicorn workers)                            │
│                                                          │
│  ClientIdLoggingMiddleware                               │
│    → extracts uid → hashes to client_id                  │
│    → stores in ContextVar                                │
│                                                          │
│  AppLogger (Python logging)                              │
│    → ClientIdFilter injects client_id + ecs_task_id      │
│    → fmt: "%(asctime)s %(levelprefix)s [%(name)s]        │
│           (pid:...) (task:...) (client:...) ..."         │
└──────────────┬───────────────────────┬───────────────────┘
               │                       │
               ▼                       ▼
┌──────────────────────┐  ┌────────────────────────────────┐
│ StreamHandler        │  │ ConcurrentTimedRotatingFile     │
│ (stdout → awslogs)   │  │ Handler (logs/studio.log)       │
│                      │  │ rotation: midnight, keep 365    │
└──────────┬───────────┘  └────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────┐
│ CloudWatch Log Groups                                    │
│                                                          │
│  /ecs/subscr-optinist-cloud-taskdef         (365 days)   │
│  /ecs/subscr-premium-optinist-cloud-taskdef (365 days)   │
│  /ecs/subscr-background-optinist-cloud-taskdef (30 days) │
│                                                          │
│  /aws/lambda/subscr-free-manager            (30 days)    │
│  /aws/lambda/subscr-free-cleanup            (30 days)    │
│  /aws/lambda/subscr-premium-manager         (30 days)    │
│  /aws/lambda/subscr-premium-cleanup         (30 days)    │
│  /aws/lambda/subscr-common-user-manager     (30 days)    │
└──────────────────────────────────────────────────────────┘

ECS Task Definition (LOG_LEVEL=INFO)
  -> cloud-startup.sh
    -> main.py --host --port --workers [--log-level DEBUG]
      -> __main_unit__.main()
        -> AppLogger.get_logging_config()
          -> LoggingConfigHelper.load_and_configure_logging_config()
            -> Reads YAML file (base config: optinist=DEBUG, root=INFO)
          -> Adds ClientIdFilter
          -> NOTE: LOG_LEVEL env var does NOT override Python logger levels
        -> If --log-level CLI arg set:
          -> _apply_log_level_override() (local dev only)
        -> uvicorn.run(log_config=logging_config)

Frontend log viewer:
  -> GET /logs/level (reads LOG_LEVEL env var -> returns available filter levels)
  -> GET /logs?levels=... (log reader excludes DEBUG from "ALL" filter)
```

### Responsibility Matrix

| Log Category              | Audience         | Destination                  |
|---------------------------|------------------|------------------------------|
| AWS Task Logs             | Developer        | CloudWatch (awslogs driver)  |
| Instance Startup Logs     | Developer        | EC2 host filesystem          |
| App Workflow Logs         | User + Developer | Local file + CloudWatch      |
| Backup Settings           | Developer        | RDS snapshots, S3 versioning |
| Operation Routine Logging | Developer        | Lambda CloudWatch log groups |
| CloudWatch Metrics        | Developer        | CWAgent + custom namespaces  |

### Python Logging vs Frontend Display

| Concern | What controls it | Production default |
|---------|------------------|--------------------|
| Python logging level | YAML config (`logging.multiuser.yaml`) | optinist=DEBUG, root=INFO |
| CloudWatch capture | Python logging level | Everything including DEBUG |
| Frontend "ALL" filter | Backend log reader | Excludes DEBUG |
| Frontend filter options | `LOG_LEVEL` env var via `/logs/level` | INFO, WARNING, ERROR, CRITICAL |

---

## Logging Configuration

### YAML Config Files

The application selects a logging config at startup based on the mode:

- **Standalone mode** uses `studio/config/logging.yaml`
- **Multiuser mode** uses `studio/config/logging.multiuser.yaml`

**File:** `studio/app/common/core/logger.py` - `AppLogger.get_logging_config()`

### Log Format

All handlers share a single format string:

```
%(asctime)s %(levelprefix)s [%(name)s] (pid:%(process)d) (task:%(ecs_task_id)s) (client:%(client_id)s) %(funcName)s():%(lineno)d - %(message)s
```

Example output:

```
2026-02-20 10:15:32,456 INFO [optinist] (pid:42) (task:abc123def) (client:a1b2c3d4e5f67890) run_workflow():128 - Starting workflow execution
```

### Handlers

| Handler                    | Class                                  | Level | Output                | Rotation  |
|----------------------------|----------------------------------------|-------|-----------------------|-----------|
| `console`                  | `logging.StreamHandler`                | DEBUG | stdout (→ CloudWatch) | None      |
| `rotating_file`            | `TimedRotatingFileHandler`             | DEBUG | `logs/studio.log`     | Midnight  |
| `rotating_file_concurrency`| `ConcurrentTimedRotatingFileHandler`   | DEBUG | `logs/studio.log`     | Midnight  |

On non-Windows platforms, `rotating_file` is replaced by `rotating_file_concurrency` at runtime to support multi-process (multi-worker uvicorn) file locking.

**File:** `studio/app/common/core/logger.py` - `LoggingConfigHelper._apply_concurrent_handler_if_supported()`

### Logger Hierarchy

| Logger       | Level | Handlers                           | Propagate |
|--------------|-------|------------------------------------|-----------|
| `root`       | INFO  | `[console, rotating_file]`         | N/A       |
| `optinist`   | DEBUG | Inherited from root                | Yes       |
| `snakemake`  | DEBUG | Standalone: `[console, rotating_file]`; Multiuser: `[rotating_file]` | No |

### Standalone vs Multiuser Differences

| Aspect            | Standalone             | Multiuser                    |
|-------------------|------------------------|------------------------------|
| Config file       | `logging.yaml`         | `logging.multiuser.yaml`     |
| Snakemake handler | `[console, rotating_file]` | `[rotating_file]` only   |
| `client_id`       | Always `"default"`     | Hashed from Firebase uid     |
| Log API filtering | No client_id filter    | Filters by current user      |

---

## AWS Task Logs (Developer Only)

### CloudWatch Log Groups

| Log Group                                        | Source       | Retention | Defined In            |
|--------------------------------------------------|--------------|-----------|----------------------|
| `/ecs/subscr-optinist-cloud-taskdef`             | Free ECS     | 365 days  | `monitoring.tf`       |
| `/ecs/subscr-premium-optinist-cloud-taskdef`     | Premium ECS  | 365 days  | `monitoring.tf`       |
| `/ecs/subscr-background-optinist-cloud-taskdef`  | Background   | 14 days   | `background_service.tf` |
| `/aws/lambda/subscr-free-manager`                | Free Manager | 14 days   | `free_manager.tf`     |
| `/aws/lambda/subscr-free-cleanup`                | Free Cleanup | 14 days   | `free_manager.tf`     |
| `/aws/lambda/subscr-premium-manager`             | Premium Mgr  | 14 days   | `premium_manager.tf`  |
| `/aws/lambda/subscr-premium-cleanup`             | Premium Clnp | 14 days   | `premium_manager.tf`  |
| `/aws/lambda/subscr-common-user-manager`         | User Mgr     | 14 days   | `common_user_manager.tf` |

### ECS Log Driver Configuration

All ECS task definitions use the `awslogs` driver with non-blocking mode:

```json
{
  "logDriver": "awslogs",
  "options": {
    "awslogs-group": "/ecs/<task-family>",
    "awslogs-region": "ap-northeast-1",
    "awslogs-stream-prefix": "ecs",
    "awslogs-create-group": "true",
    "awslogs-multiline-pattern": "^\\d{4}-\\d{2}-\\d{2}\\s\\d{2}:\\d{2}:\\d{2}",
    "mode": "non-blocking",
    "max-buffer-size": "25m"
  }
}
```

The multiline pattern groups stack traces with the preceding log entry. It must
match the application's own line prefix, which starts with a bare date: a
pattern that never matches leaves every event in the group stamped with the
task's start time, so `filter-log-events --start-time` reports the tier silent
however live it is.

### CloudWatch Metric Filters

| Filter Name           | Log Group                | Pattern                                        | Metric Namespace       |
|-----------------------|--------------------------|-------------------------------------------------|------------------------|
| `user-cpu-usage`      | Free ECS                 | `[timestamp, level, user_id, cpu_usage]`        | `OptiNiSt/Application` |
| `premium-assignments` | Premium Manager Lambda   | `"Successfully assigned premium user*"`         | `OptiNiSt/Premium`     |

### RDS CloudWatch Log Exports

RDS exports three log types to CloudWatch automatically:

| Log Type    | Content                               |
|-------------|---------------------------------------|
| `error`     | MySQL error log                       |
| `general`   | All SQL statements (verbose)          |
| `slowquery` | Queries exceeding the slow threshold  |

**File:** `infrastructure/terraform/infrastructure.tf` - `aws_db_instance.main`

---

## Instance Startup Logs (Developer Only)

### EC2 User Data

**File:** `infrastructure/scripts/ecs-user-data.sh`

All output redirected to `/var/log/ecs-setup.log`:

```bash
exec > /var/log/ecs-setup.log 2>&1
```

Logs ECS cluster registration, package installation, swap setup, Docker build, EFS mount, and DB connectivity check. Runs once at instance launch.

### Application Setup

**File:** `infrastructure/scripts/app_setup.sh`

Logs to `/var/log/app-setup.log` via tee:

```bash
exec > >(tee -a "$LOGFILE") 2>&1
```

Covers secrets retrieval, infrastructure discovery, config file creation, database initialization, and Firebase admin verification.

### Container Startup

**File:** `cloud-startup.sh`

Logs to stdout (captured by ECS awslogs driver). Covers:

- DB connectivity wait loop (30 attempts, 2s intervals)
- Alembic migration execution
- EC2 instance ID retrieval from ECS metadata
- Uvicorn startup with worker count

### CloudWatch Agent Metrics

Configured in `ecs-user-data.sh`, publishes to `CWAgent` namespace:

| Metric                     | Type   | Collection Interval |
|----------------------------|--------|---------------------|
| `mem_used_percent`         | Memory | Default (60s)       |
| `cpu_usage_idle`           | CPU    | Default (60s)       |
| `cpu_usage_iowait`         | CPU    | Default (60s)       |
| `diskio_iops_in_progress`  | Disk   | Default (60s)       |
| `diskio_io_time`           | Disk   | Default (60s)       |

Every metric carries an `AutoScalingGroupName` dimension (`append_dimensions`)
and is rolled up to the ASG level (`aggregation_dimensions`). Without this,
the host-only dimension would never match the ASG-scoped alarms below.

Also collects `/proc/loadavg` to log group `/aws/ec2/loadavg`.

---

## App Workflow Logs (User + Developer)

### Log API

**File:** `studio/app/common/routers/logs.py`

The `/logs` endpoint serves paginated log data with filtering:

| Parameter | Default | Description                                   |
|-----------|---------|-----------------------------------------------|
| `offset`  | `-1`    | Start position (`-1` = end of file)           |
| `limit`   | `50`    | Max log entries to return                      |
| `reverse` | `true`  | Read logs in reverse (newest first)            |
| `search`  | `null`  | Text search within log entries                 |
| `levels`  | `[ALL]` | Filter by log level (INFO, ERROR, DEBUG, etc.) |

In multiuser mode, the API automatically filters logs by the current user's `client_id`. Standalone mode shows all logs.

### client_id Context Propagation

The `client_id` is propagated across three execution boundaries:

**1. HTTP Requests (middleware)**

`ClientIdLoggingMiddleware` runs on every HTTP request:
- Extracts uid from Firebase/JWT token
- Generates `client_id` = first 16 chars of MD5(uid)
- Stores in `ContextVar` for the request lifetime

**File:** `studio/app/common/core/middleware/logging_middleware.py`

**2. Subprocess (ProcessPoolExecutor)**

Parent passes `client_id` as a kwarg; the `@with_client_id_context` decorator restores it:

```python
client_id = get_client_id_for_subprocess()
executor.submit(func, arg1, client_id=client_id)
```

**File:** `studio/app/common/core/logger_context_helpers.py`

**3. Snakemake Scripts**

`client_id` is passed through snakemake's config dict and restored at script entry:

```python
init_client_id_from_snakemake_config(snakemake.config)
```

Used in: `rules/data.py`, `rules/func.py`, `rules/post_process.py`, `rules/run_edit_ROI.py`

**File:** `studio/app/common/core/workflow/workflow_runner.py` - passes `client_id` into snakemake config

### Snakemake Error Logs

**File:** `studio/app/common/core/snakemake/smk_status_logger.py`

Per-workflow error logs written to `{OUTPUT_DIR}/{workspace_id}/{unique_id}/error.log`:

- Logger created per workflow run with a dedicated `FileHandler`
- Level: ERROR only
- Format: `%(asctime)s : %(levelname)s - %(filename)s - %(message)s`
- Previous error log deleted on new workflow run
- Retrieved via `SmkStatusLogger.get_error_content()` for UI display

### Log Reader

**File:** `studio/app/common/core/utils/log_reader.py`

`LogRecordReader` parses the structured log format using regex and supports:

- Multiline log entry detection (entries starting with timestamp pattern)
- Level-based filtering
- `client_id`-based filtering (multiuser mode only)
- Exclusion of polling endpoints (`GET /logs`, `OPTIONS /logs`)

### Frontend Logging

Frontend `console.error` and `console.warn` calls are intercepted by the error reporter (`initErrorReporter()`) and forwarded to the backend via `POST /users/me/frontend-errors`. See [Section 6: Frontend Error Forwarding](#6-frontend-error-forwarding) for the full pipeline.

---

## Implementation Details

### Config Loading Chain

`AppLogger.get_logging_config()` loads YAML config and adds filters. `LOG_LEVEL` is **not** applied to Python loggers -- it only controls the frontend:

```python
def get_logging_config():
    # 1. Load YAML config (base defaults: optinist=DEBUG, root=INFO)
    logging_config = LoggingConfigHelper.load_and_configure_logging_config(...)

    # 2. Add ClientIdFilter to all handlers
    # ...

    # NOTE: LOG_LEVEL env var controls the frontend log viewer filter,
    # not the Python logging level.
    return logging_config
```

The CLI `--log-level` argument is still applied in `__main_unit__.main()` for local development to override Python logging levels.

### Frontend Level Endpoint

`GET /logs/level` reads `LOG_LEVEL` env var at startup and returns levels >= that threshold:

```python
# With LOG_LEVEL=INFO -> returns ["INFO", "WARNING", "ERROR", "CRITICAL"]
# With LOG_LEVEL=DEBUG or unset -> returns all five levels
```

### Log Reader ALL Filter

When the frontend sends `levels=ALL`, the backend log reader returns everything **except** DEBUG. Users who need DEBUG logs should use CloudWatch directly.

### Valid Values

Standard Python logging levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Input is case-insensitive, normalized to uppercase.

### Components Not Affected

| Component | Reason |
|-----------|--------|
| `SmkStatusLogger` | Independent per-workflow error logger, always captures `ERROR` |
| Lambda functions | Use `print()`, not Python logging |
| YAML config files | Remain as fallback defaults when `LOG_LEVEL` is unset |

---

## How to Change Log Level

The log level is read once at process startup. There is no hot-reload
mechanism -- changing the level always requires restarting the process.

### Local Development

```bash
# Option A: CLI argument (recommended, fastest feedback loop)
poetry run python main.py --log-level DEBUG

# Option B: Env var (useful for matching deployed behavior)
LOG_LEVEL=WARNING poetry run python main.py

# Option C: No override (uses YAML defaults: root=INFO, optinist=DEBUG)
poetry run python main.py
```

To change the level, Ctrl-C the server and re-run with the new value.
Uvicorn `--reload` watches file changes only, not env var changes.

### Deployed ECS Containers

ECS containers cannot be SSHed into to export an env var. The `LOG_LEVEL`
value is baked into the ECS task definition at deploy time. To change it:

**Option A: Terraform redeploy (persistent change)**

1. Edit `LOG_LEVEL` in the task definition (`compute.tf` or
   `background_service.tf`)
2. `terraform apply` -- ECS rolls out new tasks with the new value
3. Revert the change and re-apply when done debugging

**Option B: One-off ECS task with override (no Terraform change)**

```bash
aws ecs run-task \
  --cluster subscr-optinist-cluster \
  --task-definition subscr-optinist-cloud-taskdef \
  --overrides '{
    "containerOverrides": [{
      "name": "optinist",
      "environment": [{"name": "LOG_LEVEL", "value": "DEBUG"}]
    }]
  }'
```

This launches a single task with `DEBUG` without touching the running
service. Useful for reproducing an issue in an isolated container.

**Option C: ECS service env var override (temporary service-wide)**

Update the running service's task definition override via the AWS Console:
1. Go to ECS > Clusters > Service > Update
2. Edit the container environment variables
3. Set `LOG_LEVEL=DEBUG`
4. Deploy -- ECS performs a rolling replacement
5. Revert when done

In all cases, the startup log line `Starting Optinist server on ...
(log_level=...)` confirms the active level in CloudWatch.

---

## Backup Settings

| Resource   | Mechanism              | Retention         | Config Location        |
|------------|------------------------|-------------------|------------------------|
| RDS        | Automated backups      | 35 days           | `infrastructure.tf`    |
| S3         | Bucket versioning      | Enabled (no lifecycle) | `infrastructure.tf` |
| EFS        | None                   | N/A               | N/A                    |
| App logs   | `TimedRotatingFileHandler` | 365 daily files | `logging.yaml`         |
| ALB logs   | S3 access logs         | 30 days             | `infrastructure.tf`   |

---

## Operation Routine Logging

### Lambda Schedules

| Lambda                      | Schedule       | Log Group                             |
|-----------------------------|----------------|---------------------------------------|
| `subscr-free-manager`       | Every 5 min    | `/aws/lambda/subscr-free-manager`     |
| `subscr-premium-manager`    | Every 15 min   | `/aws/lambda/subscr-premium-manager`  |
| `subscr-premium-cleanup`    | Every 1 hour   | `/aws/lambda/subscr-premium-cleanup`  |
| `subscr-common-user-manager`| Every 10 min   | `/aws/lambda/subscr-common-user-manager` |
| `subscr-cost-tracker`       | Every 1 hour   | (shares premium manager role)         |

The free manager also triggers on ASG lifecycle events (instance launch/terminate) via EventBridge.

### CloudWatch Alarms

| Alarm                              | Metric                    | Threshold    | Namespace              |
|------------------------------------|---------------------------|--------------|------------------------|
| `subscr-optinist-cpu-high`         | CPUUtilization            | > 60%        | AWS/ECS                |
| `subscr-optinist-memory-high`      | MemoryUtilization         | > 80%        | AWS/ECS                |
| `subscr-optinist-cpu-low`          | CPUUtilization            | < 20%        | AWS/ECS                |
| `subscr-optinist-memory-low`       | MemoryUtilization         | < 10%        | AWS/ECS                |
| `subscr-optinist-load-average-high`| CPUUtilization            | > 80%        | AWS/EC2                |
| `subscr-optinist-high-iowait`      | cpu_usage_iowait          | > 30%        | CWAgent                |
| `subscr-optinist-ebs-queue-length-high` | diskio_iops_in_progress | > 8 (2/3 min) | CWAgent              |
| `subscr-optinist-rds-cpu-high`     | CPUUtilization            | > 80%        | AWS/RDS                |
| `subscr-optinist-rds-connections-high` | DatabaseConnections   | > 80         | AWS/RDS                |
| `subscr-optinist-rds-storage-low`  | FreeStorageSpace          | < 10 GB      | AWS/RDS                |
| `subscr-optinist-efs-burst-credits-low` | BurstCreditBalance   | < 1 TB       | AWS/EFS                |
| `subscr-optinist-efs-throughput-high` | PercentIOLimit         | > 80%        | AWS/EFS                |
| `subscr-optinist-alb-5xx-errors`   | HTTPCode_Target_5XX_Count | >= 20 / 5 min | AWS/ApplicationELB    |
| `subscr-optinist-free-tg-response-time-high` | TargetResponseTime | p95 > 10s (25/30 min) | AWS/ApplicationELB |
| `subscr-optinist-public-tg-response-time-high` | TargetResponseTime | p95 > 5s (25/30 min) | AWS/ApplicationELB |
| `subscr-optinist-free-tg-unhealthy-hosts`   | UnHealthyHostCount  | > 0          | AWS/ApplicationELB     |
| `subscr-optinist-public-tg-unhealthy-hosts` | UnHealthyHostCount  | > 0          | AWS/ApplicationELB     |
| `subscr-premium-<user_id>-tg-unhealthy-hosts` | UnHealthyHostCount | > 0 (created per-user by the premium-manager Lambda) | AWS/ApplicationELB |
| `subscr-background-task-stopped`   | RunningTaskCount          | < 1          | ECS/ContainerInsights  |
| `subscr-background-cpu-high`       | CpuUtilized               | > 400 units  | ECS/ContainerInsights  |
| `subscr-background-memory-high`    | MemoryUtilized            | > 600 MB     | ECS/ContainerInsights  |
| `subscr-monthly-cost-high` | TotalMonthlyCost          | > $500/day   | OptiNiSt/Cost          |
| `subscr-premium-cpu-high`          | CPUUtilization            | > 80%        | AWS/ECS                |
| `subscr-premium-memory-high`       | MemoryUtilization         | > 85%        | AWS/ECS                |

**Alarm name prefixes differ by who creates them.** Terraform-managed alarms use the `subscr-optinist-` prefix (Terraform `local.env_prefix` = `<environment>-optinist`). Premium alarms created by the premium-manager/cleanup Lambdas use the bare `subscr-` prefix (the Lambdas' `ENV_PREFIX` = `<environment>`, matching their instance and target-group naming). When listing alarms, filter on `subscr-` to catch both; `subscr-optinist-` alone will miss every per-user premium alarm.

### CloudWatch Dashboard

**Dashboard:** `subscr-optinist-monitoring`

7 rows of widgets covering:

1. Free vs Premium CPU/Memory comparison + ASG capacity
2. ECS service metrics + cost tracking
3. ALB performance + user tier operations
4. EC2 load average/I/O wait + RDS/EFS health
5. ASG lifecycle + autoscaling triggers
6. Background jobs + Lambda operations
7. Alarm status overview (all 19 alarms)

---

## Configuration

### Environment Variables

| Variable                       | Purpose                                       | Default   |
|--------------------------------|-----------------------------------------------|-----------|
| `LOG_LEVEL`                    | Control frontend log viewer filter levels      | `"INFO"`  |
| `UVICORN_ACCESS_LOG`           | Enable uvicorn access log (`1`/`0`)            | `"1"`     |
| `PYTHONUNBUFFERED`             | Disable Python output buffering                | `"1"`     |
| `DISABLE_BACKGROUND_SCHEDULER` | Disable APScheduler in API workers             | `"1"`     |
| `CLOUDWATCH_LOG_GROUP`         | Passed to container (informational only)       | Varies    |
| `ECS_CONTAINER_METADATA_URI_V4`| Auto-set by ECS; used to fetch task ID         | (ECS)     |

| CLI Argument | Purpose | Default |
|--------------|---------|---------|
| `--log-level` | Override Python logging level (local dev) | `None` (uses YAML defaults) |

---

## Edge Case Handling

### 1. Invalid LOG_LEVEL Value

**Problem:** `LOG_LEVEL=VERBOSE` or other invalid string.

**Solution:** `_apply_log_level_override()` validates against the allowed set. Invalid values trigger `warnings.warn()` and YAML defaults are used unchanged.

**Guarantee:** Application never crashes due to invalid log level.

### 2. Empty LOG_LEVEL String

**Problem:** `LOG_LEVEL=""` set in environment.

**Solution:** `os.environ.get("LOG_LEVEL")` returns `""` which is falsy. The override is skipped entirely.

**Guarantee:** Empty string behaves identically to unset.

### 3. CLI Conflicts with Env Var

**Problem:** `LOG_LEVEL=WARNING` in env, `--log-level DEBUG` on CLI.

**Solution:** CLI wins by design. `get_logging_config()` applies env var first. `main()` applies CLI arg on top.

**Guarantee:** Developer intent (CLI) always overrides deployment config (env var).

### 4. No Hot Reload

**Problem:** Developer changes `LOG_LEVEL` while server is running.

**Solution:** The level is read once at startup. A full process restart is required. See "How to Change Log Level" above for the workflows.

**Guarantee:** Log level is consistent for the lifetime of a server process.

### 5. Multiple Uvicorn Workers

**Problem:** Production runs multiple workers, each forks independently.

**Solution:** Each worker inherits the same environment and calls `get_logging_config()` during fork initialization.

**Guarantee:** All workers get the same log level.

### 6. Frontend Error Forwarding

Frontend `console.error` and `console.warn` calls are automatically intercepted and forwarded to the backend via `POST /users/me/frontend-errors`.

**Pipeline:**
1. `initErrorReporter()` in `frontend/src/index.tsx` overrides `console.error`/`console.warn` globally
2. Errors are queued in memory (max 20 items, 2000 char truncation)
3. Every 5 seconds, the queue is flushed to the backend via `fetch` (not axios, to avoid interceptor loops)
4. `beforeunload` listener flushes on page close using `keepalive: true`
5. Backend logs each error via `logger.warning()` with `[FRONTEND]` prefix

**Log format:**
```
[FRONTEND] [ERROR|WARN] user=<uid> url=<page_url> source=<js_source>: <message>
```

**Rate limiting:** 10 errors per 60s per user (in-memory sliding window, per-process).

**CloudWatch query:**
```
filter @message like "[FRONTEND]"
```

**Log viewer:** The FRONTEND filter button in the log modal filters for lines containing `[FRONTEND]` in the message body. It is orthogonal to severity-based filters (INFO/WARNING/etc).

---

## Testing

### Unit Tests

**File:** `studio/tests/app/common/core/test_logger_log_level.py`

| Test | Purpose |
|------|---------|
| `test_sets_all_levels` | Valid level overrides root, loggers, and handlers |
| `test_invalid_value_warns_and_keeps_defaults` | Invalid level emits warning, config unchanged |
| `test_case_insensitive` | Lowercase input normalized to uppercase |
| `test_handler_without_level_key_is_skipped` | Handlers without `level` key are not modified |
| `test_all_valid_levels_accepted` | Parametric test for all 5 valid levels |
| `test_env_var_overrides_config` | `LOG_LEVEL` env var applies override |
| `test_empty_string_env_var_skipped` | Empty string treated as unset |
| `test_unset_env_var_skipped` | Unset env var preserves YAML defaults |

### Manual Verification

```bash
# Verify WARNING+ only in console
LOG_LEVEL=WARNING poetry run python main.py

# Verify DEBUG lines appear
poetry run python main.py --log-level DEBUG

# Verify startup log includes effective level
# Look for: "Starting Optinist server on ... (log_level=...)"
```

---

## Key Functions Reference

| Function | File | Purpose |
|----------|------|---------|
| `AppLogger.get_logging_config()` | `studio/app/common/core/logger.py` | Loads YAML config, applies concurrent handler, adds ClientIdFilter |
| `AppLogger.init_logger()` | `studio/app/common/core/logger.py` | One-time logging initialization via `dictConfig()` |
| `AppLogger.generate_client_id()` | `studio/app/common/core/logger.py` | MD5 hash of uid, truncated to 16 chars |
| `LoggingConfigHelper._apply_log_level_override()` | `studio/app/common/core/logger.py` | Apply a level string to all loggers and handlers (used by `--log-level` CLI only) |
| `LoggingConfigHelper.load_and_configure_logging_config()` | `studio/app/common/core/logger.py` | Unified config loading with path adjustment and concurrent handler setup |
| `ClientIdLoggingMiddleware.__call__()` | `studio/app/common/core/middleware/logging_middleware.py` | Extracts uid from request, sets client_id in context |
| `with_client_id_context()` | `studio/app/common/core/logger_context_helpers.py` | Decorator for subprocess client_id propagation |
| `init_client_id_from_snakemake_config()` | `studio/app/common/core/logger_context_helpers.py` | Restores client_id in snakemake script processes |
| `get_log_data()` | `studio/app/common/routers/logs.py` | Log API endpoint with pagination and filtering |
| `GET /logs/level` | `studio/app/common/routers/logs.py` | Return available frontend filter levels based on `LOG_LEVEL` env var |
| `LogRecordReader.validate()` | `studio/app/common/core/utils/log_reader.py` | Filters log entries by level and client_id |
| `SmkStatusLogger.get_logger()` | `studio/app/common/core/snakemake/smk_status_logger.py` | Creates per-workflow error logger with FileHandler |

---

## AWS Resources

| Resource | Configuration |
|----------|--------------|
| ECS Task Definitions (Free, Premium, Background) | `LOG_LEVEL=INFO` environment variable |
