"""
Internal API endpoints for system-to-system communication.

These endpoints are protected by a shared secret (INTERNAL_API_SECRET)
rather than JWT authentication. They are called by Lambda functions
after user migrations to trigger experiment metadata sync.
"""

import os
import secrets
import time
from typing import Dict

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    status,
)
from sqlmodel import Session, select

from studio.app.common.core.auth.auth_dependencies import _get_user_remote_bucket_name
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteExperimentSyncMode,
    RemoteStorageController,
    RemoteStorageReader,
    RemoteStorageSimpleReader,
)
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.utils.path_guard import secure_component
from studio.app.common.db.database import get_db, get_session
from studio.app.common.models.user import User
from studio.app.common.models.workspace import Workspace
from studio.app.dir_path import DIRPATH

router = APIRouter(prefix="/system-internal", tags=["system-internal"])

logger = AppLogger.get_logger()

INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET")

# Rate limiting for internal endpoints (in-memory cache)
_rate_limit_cache: Dict[int, float] = {}
_sync_rate_limit_cache: Dict[str, float] = {}
_RATE_LIMIT_SECONDS = 10


def _cleanup_rate_limit_cache(cache: Dict, cutoff: float) -> None:
    """Remove rate-limit entries older than cutoff to prevent unbounded growth."""
    expired = [k for k, v in cache.items() if v < cutoff]
    for k in expired:
        del cache[k]


def verify_internal_secret(x_internal_secret: str = Header(...)) -> None:
    """
    Verify the internal API secret header using constant-time comparison.
    Raises HTTPException 403 if invalid.
    """
    if not INTERNAL_API_SECRET:
        logger.error("INTERNAL_API_SECRET not configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal API not configured",
        )

    # Use constant-time comparison to prevent timing attacks
    if not secrets.compare_digest(x_internal_secret, INTERNAL_API_SECRET):
        logger.warning("Invalid internal API secret provided")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid internal secret",
        )


@router.post("/sync-experiments/{user_id}")
async def sync_user_experiments(
    user_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: None = Depends(verify_internal_secret),
):
    """
    Trigger experiment metadata sync for a user.

    Called by Lambda functions (free_manager, premium_manager) after
    migrating a user to a new instance. Downloads all experiment
    metadata from S3 to the local instance.

    Args:
        user_id: Database ID of the user to sync experiments for

    Returns:
        Status indicating sync was initiated
    """
    # Rate limiting check
    current_time = time.time()
    _cleanup_rate_limit_cache(_rate_limit_cache, current_time - _RATE_LIMIT_SECONDS * 2)
    last_request = _rate_limit_cache.get(user_id, 0)
    if current_time - last_request < _RATE_LIMIT_SECONDS:
        logger.warning(
            f"Rate limited sync request for user {user_id}: "
            f"{current_time - last_request:.1f}s since last request"
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Sync request too frequent",
        )
    _rate_limit_cache[user_id] = current_time

    # Look up user (User.id is the integer primary key)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        logger.warning(f"Sync requested for non-existent user: {user_id}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Get bucket name
    bucket_name = _get_user_remote_bucket_name(user)

    logger.info(
        f"Initiating experiment sync for user {user_id} "
        f"(email: {user.email}, bucket: {bucket_name})"
    )

    # Trigger background download (non-blocking)
    background_tasks.add_task(_download_experiments_for_user, bucket_name, user_id)

    return {"status": "sync_initiated", "user_id": user_id}


@router.post("/sync-experiment/{workspace_id}/{unique_id}")
async def sync_single_experiment(
    workspace_id: str = Path(..., pattern=r"^\d+$"),
    unique_id: str = Path(..., pattern=r"^[a-zA-Z0-9_-]+$"),
    bucket_name: str = Query(
        ...,
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9.\-]*[a-z0-9]$",
    ),
    has_thumbnails: bool = Query(default=True),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    _: None = Depends(verify_internal_secret),
):
    """
    Trigger download of a single experiment on this
    API instance.

    Called by the background sync job after validating an
    experiment in S3. Downloads thumbnails and essential
    metadata so they are pre-cached before users request
    them.
    """
    # Rate limiting per experiment
    # Becomes a directory name below, so validate it here.
    workspace_id = secure_component(workspace_id)
    unique_id = secure_component(unique_id)

    cache_key = f"{workspace_id}/{unique_id}"
    current_time = time.time()
    _cleanup_rate_limit_cache(
        _sync_rate_limit_cache, current_time - _RATE_LIMIT_SECONDS * 2
    )
    last_req = _sync_rate_limit_cache.get(cache_key, 0)
    if current_time - last_req < _RATE_LIMIT_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Sync request too frequent",
        )
    _sync_rate_limit_cache[cache_key] = current_time

    logger.info(
        f"Proactive sync requested for"
        f" {workspace_id}/{unique_id}"
        f" (bucket: {bucket_name})"
    )

    background_tasks.add_task(
        _download_single_experiment,
        bucket_name,
        workspace_id,
        unique_id,
        has_thumbnails,
    )

    return {
        "status": "sync_initiated",
        "workspace_id": workspace_id,
        "unique_id": unique_id,
    }


async def _download_single_experiment(
    bucket_name: str,
    workspace_id: str,
    unique_id: str,
    has_thumbnails: bool = True,
) -> None:
    """
    Background task to download thumbnails and essential
    metadata for a single experiment from S3.
    """
    if not RemoteStorageController.is_available():
        logger.warning(
            "Remote storage not available,"
            " skipping proactive sync for"
            f" {workspace_id}/{unique_id}"
        )
        return

    # Skip if required files already exist locally
    local_path = join_filepath([DIRPATH.OUTPUT_DIR, workspace_id, unique_id])
    exp_yaml = os.path.join(local_path, DIRPATH.EXPERIMENT_YML)
    wf_yaml = os.path.join(local_path, DIRPATH.WORKFLOW_YML)
    if os.path.exists(exp_yaml) and os.path.exists(wf_yaml):
        logger.info(
            "Skipping proactive sync for"
            f" {workspace_id}/{unique_id}:"
            " files already exist locally"
        )
        return

    try:
        async with RemoteStorageReader(
            bucket_name,
            workspace_id,
            unique_id,
            RemoteExperimentSyncMode.THUMBNAILS_ONLY,
        ) as controller:
            if has_thumbnails:
                await controller.download_experiment(
                    workspace_id,
                    unique_id,
                    sync_mode=RemoteExperimentSyncMode.THUMBNAILS_ONLY,
                )
            await controller.download_experiment(
                workspace_id,
                unique_id,
                sync_mode=RemoteExperimentSyncMode.ESSENTIAL_ONLY,
            )
        logger.info("Proactive sync completed for" f" {workspace_id}/{unique_id}")
    except Exception as e:
        logger.error(
            "Proactive sync failed for" f" {workspace_id}/{unique_id}: {e}",
            exc_info=True,
        )


async def _download_experiments_for_user(bucket_name: str, user_id: int) -> None:
    """
    Background task to download experiment metadata from S3.
    Creates its own database session to avoid using closed session.

    Args:
        bucket_name: S3 bucket name for the user
        user_id: User ID (for logging)
    """
    if not RemoteStorageController.is_available():
        logger.warning(
            f"Remote storage not available, skipping sync for user {user_id}"
        )
        return

    db = None
    try:
        # Create new database session for background task
        db = get_session()

        # Get all workspace IDs for this user
        # Workspace.user_id is the foreign key to User.id (integer)
        workspaces = (
            db.execute(select(Workspace).where(Workspace.user_id == user_id))
            .scalars()
            .all()
        )
        workspace_ids = [ws.id for ws in workspaces]

        if not workspace_ids:
            logger.info(f"No workspaces found for user {user_id}, skipping sync")
            return

        async with RemoteStorageSimpleReader(bucket_name) as controller:
            await controller.download_all_experiments_metas(workspace_ids)
        logger.info(f"Experiment sync completed for user {user_id}")
    except Exception as e:
        logger.error(f"Experiment sync failed for user {user_id}: {e}", exc_info=True)
    finally:
        if db:
            db.close()
