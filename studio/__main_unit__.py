import argparse
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi_pagination import add_pagination
from starlette.middleware.cors import CORSMiddleware

from studio.app.common.core.auth.auth_dependencies import (
    get_admin_user,
    get_current_user,
    get_current_user_with_dataview_outputs_check,
)
from studio.app.common.core.instance_mode import (
    INSTANCE_MODE_DEFAULT,
    INSTANCE_MODE_ENV,
    INSTANCE_MODE_PUBLIC,
)
from studio.app.common.core.logger import AppLogger, LoggingConfigHelper
from studio.app.common.core.middleware import (
    ClientIdLoggingMiddleware,
    SecureRoutingMiddleware,
    SPARoutingMiddleware,
    UserActivityMiddleware,
)
from studio.app.common.core.middleware.spa_routing_middleware import (
    INDEX_HTML_CACHE_HEADERS,
)
from studio.app.common.core.mode import MODE
from studio.app.common.core.storage.remote_storage_controller import RemoteStorageType
from studio.app.common.core.subscription.constants import (
    ExpirationDeletion,
    PremiumExpirationSweep,
    StorageReconciliation,
    SyncStatusConstants,
)

# Background job imports (only used in non-standalone mode)
if not MODE.IS_STANDALONE:
    from studio.app.common.core.background.cleanup_job import DataCleanupJob
    from studio.app.common.core.background.expiration_lifecycle_job import (
        ExpirationLifecycleJob,
    )
    from studio.app.common.core.background.premium_expiration_sweep_job import (
        PremiumExpirationSweepJob,
    )
    from studio.app.common.core.background.scheduler import BackgroundScheduler
    from studio.app.common.core.background.storage_reconciliation_job import (
        StorageReconciliationJob,
    )
    from studio.app.common.core.background.sync_job import PublishedExperimentSyncJob

from studio.app.common.core.workspace.workspace_dependencies import (
    is_workspace_available,
    is_workspace_owner,
)
from studio.app.common.routers import (
    algolist,
    auth,
    dataview,
    experiment,
    files,
    internal,
    log_report,
    logs,
    outputs,
    params,
    registrations,
    run,
    storage_limit_alerts,
    subscriptions,
    users_admin,
    users_me,
    users_search,
    workflow,
    workspace,
)
from studio.app.dir_path import DIRPATH
from studio.app.optinist.routers import hdf5, mat, nwb, roi
from studio.app.version import BuildInfo, Version

logger = AppLogger.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup event
    """
    import platform
    import sys

    sys_version = sys.version.replace("\n", " ")
    mode = "standalone" if MODE.IS_STANDALONE else "multiuser"
    remote_storage_type = RemoteStorageType.get_activated_type()

    logger = AppLogger.get_logger()

    logger.info(
        f'"Studio" application startup complete.\n'
        f"    # Platform: {platform.platform()}\n"
        f"    # Python Version: {sys_version}\n"
        f"    # App Version: {Version.APP_VERSION}\n"
        f"    # Git Commit: {BuildInfo.GIT_COMMIT}\n"
        f"    # Build Time: {BuildInfo.BUILD_TIMESTAMP}\n"
        f"    # Env:DATA_DIR: {DIRPATH.DATA_DIR}\n"
        f"    # Mode: {mode}\n"
        f"    # REMOTE_STORAGE_TYPE: {remote_storage_type}\n"
    )

    # Initialize background job scheduler
    # Can be disabled with DISABLE_BACKGROUND_SCHEDULER=1 env var
    # (e.g., when using cron)
    disable_scheduler = os.environ.get("DISABLE_BACKGROUND_SCHEDULER", "0") == "1"

    # Only the public tier serves the published-experiment cache, so only it warms it.
    instance_mode = os.environ.get(INSTANCE_MODE_ENV, INSTANCE_MODE_DEFAULT)
    if _should_run_startup_sync(instance_mode, MODE.IS_STANDALONE):
        import asyncio

        # Store on app.state to prevent GC mid-execution
        app.state.startup_sync_task = asyncio.create_task(_startup_sync())
        logger.info("Startup sync task scheduled (runs in background)")

    if not MODE.IS_STANDALONE and not disable_scheduler:
        logger.info("Initializing background job scheduler")
        BackgroundScheduler.initialize()

        # Add sync job (every 5 minutes)
        BackgroundScheduler.add_job(
            func=PublishedExperimentSyncJob.run,
            interval_minutes=SyncStatusConstants.SYNC_INTERVAL_MINUTES,
            job_id="published_experiment_sync",
        )

        # Add cleanup job (every 60 minutes)
        # NOTE: On free-tier instances, DataCleanupJob is run by a dedicated
        # standalone process (studio/cleanup_worker.py) started from
        # cloud-startup.sh.  This registration remains here for the
        # background service, where _handle_orphaned_data() still runs
        # to clean up DB records of terminated instances.
        # Future: consider migrating all background jobs to standalone
        # worker processes to fully decouple them from FastAPI lifespan.
        BackgroundScheduler.add_job(
            func=DataCleanupJob.run,
            interval_minutes=SyncStatusConstants.CLEANUP_INTERVAL_MINUTES,
            job_id="data_cleanup",
        )

        # Add storage reconciliation job (every 60 minutes)
        BackgroundScheduler.add_job(
            func=StorageReconciliationJob.run,
            interval_minutes=StorageReconciliation.INTERVAL_MINUTES,
            job_id="storage_reconciliation",
        )

        BackgroundScheduler.add_job(
            func=ExpirationLifecycleJob.run,
            interval_minutes=ExpirationDeletion.JOB_INTERVAL_MINUTES,
            job_id=ExpirationDeletion.JOB_ID,
        )

        # Backstop: release dangling premium assignments after expiration
        # (covers missed customer.subscription.deleted events / direct DB
        # expirations such as test 600-17b). Event-driven release in
        # WebhookService is unchanged; this is purely a safety net.
        BackgroundScheduler.add_job(
            func=PremiumExpirationSweepJob.run,
            interval_minutes=PremiumExpirationSweep.JOB_INTERVAL_MINUTES,
            job_id=PremiumExpirationSweep.JOB_ID,
        )

        # Start scheduler
        BackgroundScheduler.start()
        logger.info("Background job scheduler started")
    elif disable_scheduler:
        logger.info(
            "Background scheduler disabled by DISABLE_BACKGROUND_SCHEDULER env var"
        )

    yield

    # Shutdown event
    if not MODE.IS_STANDALONE and not disable_scheduler:
        BackgroundScheduler.shutdown()
        logger.info("Background job scheduler shut down")

    logger.info('"Studio" application shutdown.')


app = FastAPI(docs_url="/docs", openapi_url="/openapi", lifespan=lifespan)


@app.get("/health")
async def health_check():
    try:
        return {"status": "healthy"}
    except Exception as e:
        logger.error(f"Exception in health check: {str(e)}")
        return {
            "status": "warning",
            "details": {"application": "running", "error": str(e)},
        }


add_pagination(app)


async def _startup_sync():
    """One leader across the ASG performs sync; others stand down."""
    import asyncio

    from studio.app.common.core.background.sync_job import PublishedExperimentSyncJob
    from studio.app.common.core.storage.startup_leader import startup_sync_leader_lock

    try:
        await asyncio.sleep(5)
        with startup_sync_leader_lock() as acquired:
            if not acquired:
                logger.info("Startup sync deferred to leader task")
                return
            await PublishedExperimentSyncJob.run_startup_sync()
    except Exception as e:
        logger.error(f"Startup sync error: {e}", exc_info=True)


def _should_run_startup_sync(instance_mode: str, is_standalone: bool) -> bool:
    """Startup sync warms the public tier's cache; other tiers shouldn't run it."""
    if is_standalone:
        return False
    return instance_mode == INSTANCE_MODE_PUBLIC


def _register_public_routers(app: FastAPI) -> None:
    """Routers mounted on every tier so the SPA can bootstrap during a
    free-tier outage: auth (login/refresh), users_me (the GET /users/me +
    premium-assign / heartbeat / beacon set the SPA calls right after login),
    public dataview, internal, outputs. Public-tier tasks still talk to the
    same RDS and invoke the same premium-manager Lambda as free, so these
    routes work identically whichever tier serves them.
    """
    app.include_router(dataview.public_router)
    app.include_router(internal.router)  # Internal secret auth, not JWT.
    app.include_router(
        outputs.router,
        dependencies=[Depends(get_current_user_with_dataview_outputs_check)],
    )
    app.include_router(auth.router)
    app.include_router(users_me.router, dependencies=[Depends(get_current_user)])
    app.include_router(users_me.beacon_router)
    app.include_router(log_report.router, dependencies=[Depends(get_current_user)])


def _register_authenticated_routers(app: FastAPI) -> None:
    """Workflow/optinist routers gated out of the public tier."""
    app.include_router(algolist.router, dependencies=[Depends(get_current_user)])
    app.include_router(experiment.router, dependencies=[Depends(get_current_user)])
    app.include_router(files.router, dependencies=[Depends(get_current_user)])
    app.include_router(logs.router, dependencies=[Depends(get_current_user)])
    app.include_router(params.router, dependencies=[Depends(get_current_user)])
    app.include_router(run.router, dependencies=[Depends(get_current_user)])
    app.include_router(
        storage_limit_alerts.router, dependencies=[Depends(get_current_user)]
    )
    app.include_router(users_admin.router, dependencies=[Depends(get_admin_user)])
    app.include_router(users_search.router, dependencies=[Depends(get_current_user)])
    app.include_router(workflow.router, dependencies=[Depends(get_current_user)])
    app.include_router(workspace.router, dependencies=[Depends(get_current_user)])
    app.include_router(dataview.router, dependencies=[Depends(get_current_user)])
    app.include_router(subscriptions.router, dependencies=[Depends(get_current_user)])
    app.include_router(subscriptions.webhook_router)
    app.include_router(registrations.router)

    # optinist routers
    app.include_router(hdf5.router, dependencies=[Depends(get_current_user)])
    app.include_router(mat.router, dependencies=[Depends(get_current_user)])
    app.include_router(nwb.router, dependencies=[Depends(get_current_user)])
    app.include_router(roi.router, dependencies=[Depends(get_current_user)])


def _register_routers(app: FastAPI, instance_mode: str) -> None:
    _register_public_routers(app)
    if instance_mode == INSTANCE_MODE_PUBLIC:
        return
    _register_authenticated_routers(app)


INSTANCE_MODE = os.environ.get(INSTANCE_MODE_ENV, INSTANCE_MODE_DEFAULT)
_register_routers(app, INSTANCE_MODE)


def skip_dependencies():
    pass


if MODE.IS_STANDALONE:
    app.dependency_overrides[get_current_user] = skip_dependencies
    app.dependency_overrides[
        get_current_user_with_dataview_outputs_check
    ] = skip_dependencies
    app.dependency_overrides[get_admin_user] = skip_dependencies
    app.dependency_overrides[is_workspace_owner] = skip_dependencies
    app.dependency_overrides[is_workspace_available] = skip_dependencies

app.add_middleware(SecureRoutingMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Routing headers issued by SecureRoutingMiddleware.
    # Canonical definitions: infrastructure/aws_constants.py (RoutingHeaders class)
    expose_headers=["x-user-tier", "x-routing-id", "x-served-by-instance"],
)

app.add_middleware(SPARoutingMiddleware)

app.add_middleware(ClientIdLoggingMiddleware)

app.add_middleware(UserActivityMiddleware)


@app.get("/is_standalone", response_model=bool, tags=["others"])
async def is_standalone():
    return MODE.IS_STANDALONE


os.makedirs(f"{DIRPATH.FRONTEND_DIRS.BUILD}/static", exist_ok=True)
app.mount(
    "/static",
    StaticFiles(directory=f"{DIRPATH.FRONTEND_DIRS.BUILD}/static"),
    name="static",
)

# Mount images directory for card brand icons and other images
os.makedirs(f"{DIRPATH.FRONTEND_DIRS.BUILD}/images", exist_ok=True)
app.mount(
    "/images",
    StaticFiles(directory=f"{DIRPATH.FRONTEND_DIRS.BUILD}/images"),
    name="images",
)

public_templates = Jinja2Templates(directory=DIRPATH.FRONTEND_DIRS.PUBLIC)
build_templates = Jinja2Templates(directory=DIRPATH.FRONTEND_DIRS.BUILD)


@app.get("/")
async def root(request: Request):
    if os.path.exists(f"{DIRPATH.FRONTEND_DIRS.BUILD}/index.html"):
        return build_templates.TemplateResponse(
            "index.html", {"request": request}, headers=INDEX_HTML_CACHE_HEADERS
        )
    else:
        return public_templates.TemplateResponse(
            "no-built-pages.html",
            {"request": request},
            headers=INDEX_HTML_CACHE_HEADERS,
        )


@app.get("/{_:path}")
async def any_pages(request: Request):
    """
    Requests that don't match any routers come here.
    """
    # For backend API requests, it returns 404
    # (Determined by request.headers)
    if "application/json" in request.headers.get("accept", ""):
        return Response(status_code=status.HTTP_404_NOT_FOUND, content="")
    # In all other cases, forward to frontend.
    else:
        return await root(request)


def main(develop_mode: bool = False):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Override log level (takes precedence over LOG_LEVEL env var)",
    )
    timeout_keep_alive = 60
    args = parser.parse_args()

    logging_config = AppLogger.get_logging_config()

    if args.log_level:
        logging_config = LoggingConfigHelper._apply_log_level_override(
            logging_config, args.log_level
        )

    effective_level = logging_config.get("root", {}).get("level", "INFO")
    logger.info(
        f"Starting Optinist server on {args.host}:{args.port} "
        f"(log_level={effective_level})"
    )

    if develop_mode:
        if args.workers > 1:
            reload = False
            reload_options = {}
        else:
            reload = args.reload
            reload_options = {"reload_dirs": ["studio"]} if args.reload else {}

        uvicorn.run(
            "studio.__main_unit__:app",
            host=args.host,
            port=args.port,
            log_config=logging_config,
            workers=args.workers,
            timeout_keep_alive=timeout_keep_alive,
            reload=reload,
            **reload_options,
        )
    else:
        uvicorn.run(
            "studio.__main_unit__:app",
            host=args.host,
            port=args.port,
            log_config=logging_config,
            workers=args.workers,
            timeout_keep_alive=timeout_keep_alive,
            reload=False,
        )
