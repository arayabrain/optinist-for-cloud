import asyncio
import os
from typing import List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi_pagination.ext.sqlmodel import paginate
from sqlalchemy import update
from sqlalchemy.sql import Select
from sqlmodel import Session, select

from studio.app.common import models
from studio.app.common.core.auth.auth_dependencies import get_current_user
from studio.app.common.core.dataview.dataview_services import (
    DataviewService,
    PublishValidator,
)
from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteExperimentNotFoundError,
    RemoteExperimentSyncMode,
    RemoteStorageController,
    RemoteStorageDownloadUtils,
    RemoteStorageLockError,
    RemoteStorageReader,
    RemoteStorageType,
    RemoteSyncStatusFileUtil,
)
from studio.app.common.core.storage.s3_storage_controller import S3StorageController
from studio.app.common.core.workspace.workspace_dependencies import (
    is_workspace_available,
)
from studio.app.common.db.database import get_db
from studio.app.common.routers.workflow import reproduce_experiment
from studio.app.common.schemas.base import SortDirection, SortOptions
from studio.app.common.schemas.dataview import (
    DataviewRecord,
    DataviewRecordHeader,
    DataviewRecordSearchOptions,
    LocalSyncStatus,
    PageWithHeader,
    PublishFlags,
    PublishStatus,
)
from studio.app.common.schemas.users import User
from studio.app.common.schemas.workflow import WorkflowWithResults

router = APIRouter(tags=["Dataview"], prefix="/api/dataview")
public_router = APIRouter(tags=["Dataview"], prefix="/api/public/dataview")

logger = AppLogger.get_logger()

# Max concurrent S3 pre-sync operations during bulk publish (bounds the
# per-request fan-out of RemoteStorageReader clients on one uvicorn process).
PUBLISH_PRESYNC_CONCURRENCY = 8


RECORDS_SORT_MAPPING = {
    "user_name": models.User.name,
    "workspace_name": models.Workspace.name,
    "timestamp": models.ExperimentRecord.analyzed_at,
}


def records_pagenate_transformer(items: Sequence) -> Sequence:
    records = []

    for idx, item in enumerate(items):
        record = DataviewRecord.from_orm(item)

        # Adjusting response fields
        record.owner = record.workspace.user
        record.workspace.user = None  # Not used

        records.append(record)

    return records


def get_records_common_query(sortOptions: SortOptions) -> Select:
    query = (
        select(models.ExperimentRecord)
        .join(
            models.Workspace,
            models.Workspace.id == models.ExperimentRecord.workspace_id,
        )
        .join(
            models.User,
            models.User.id == models.Workspace.user_id,
        )
        .filter(
            models.Workspace.deleted.is_(False),
            models.User.active.is_(True),
            models.ExperimentRecord.success.is_(True),
        )
    )

    sa_sort_list = sortOptions.get_sa_sort_list(
        sa_table=models.ExperimentRecord,
        mapping=RECORDS_SORT_MAPPING,
        default_sort=["analyzed_at", SortDirection.desc],
    )
    query = query.order_by(*sa_sort_list)

    return query


def get_records_filtered_query(
    query: Select, options: DataviewRecordSearchOptions
) -> Select:
    if options.uid:
        query = query.filter(
            models.ExperimentRecord.uid.contains(options.uid, autoescape=True)
        )

    if options.name:
        query = query.filter(
            models.ExperimentRecord.name.contains(options.name, autoescape=True)
        )

    if options.user_name:
        query = query.filter(
            models.User.name.contains(options.user_name, autoescape=True)
        )

    if options.workspace_id:
        query = query.filter(models.Workspace.id == int(options.workspace_id))

    if options.workspace_name:
        query = query.filter(
            models.Workspace.name.contains(options.workspace_name, autoescape=True)
        )

    if options.publish_status is not None:
        query = query.filter(
            models.ExperimentRecord.publish_status == options.publish_status
        )

    return query


@public_router.get(
    "",
    response_model=PageWithHeader[DataviewRecord],
    description="""
- Search and respond to data for display in Public Dataview
""",
)
async def public_search_dataview_records(
    db: Session = Depends(get_db),
    options: DataviewRecordSearchOptions = Depends(),
    sortOptions: SortOptions = Depends(),
):
    query = get_records_common_query(sortOptions).filter(
        models.ExperimentRecord.publish_status == PublishStatus.on.value,
    )
    query = get_records_filtered_query(query, options)

    data: PageWithHeader = paginate(
        session=db,
        query=query,
        transformer=records_pagenate_transformer,
    )

    return data


@router.get(
    "",
    response_model=PageWithHeader[DataviewRecord],
    description="""
- Search and respond to data for display in Dataview
""",
)
async def search_dataview_records(
    db: Session = Depends(get_db),
    options: DataviewRecordSearchOptions = Depends(),
    sortOptions: SortOptions = Depends(),
    current_user: User = Depends(get_current_user),
):
    query = get_records_common_query(sortOptions).filter(
        models.User.id == current_user.id,
    )
    query = get_records_filtered_query(query, options)

    if options.workspace_id:
        workspace_record = (
            db.query(models.Workspace)
            .filter(
                models.Workspace.id == options.workspace_id,
                models.Workspace.user_id == current_user.id,
                models.Workspace.deleted.is_(False),
            )
            .first()
        )

        record_header = (
            DataviewRecordHeader(
                workspace_id=workspace_record.id, workspace_name=workspace_record.name
            )
            if workspace_record
            else DataviewRecordHeader()
        )
    else:
        record_header = DataviewRecordHeader()

    data: PageWithHeader = paginate(
        session=db,
        query=query,
        transformer=records_pagenate_transformer,
        additional_data={"header": record_header},
    )

    return data


@public_router.get(
    "/workflow/reproduce/{workspace_id}/{unique_id}",
    response_model=WorkflowWithResults,
    description="""
- Public Dataview access wrapper for `GET /workflow/reproduce`
- Returns 202 if experiment is published but not yet synced
""",
)
async def public_reproduce_experiment(
    workspace_id: str,
    unique_id: str,
    db: Session = Depends(get_db),
):
    # Check target record accessibility
    record = DataviewService.find_dataview_record(
        db, int(workspace_id), unique_id, published_only=True
    )

    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # Ensure experiment is available on local EBS (download from S3 if needed)
    # Also try to sync if status is pending/error - the local data might be incomplete
    is_unsynced = RemoteSyncStatusFileUtil.check_sync_status_unsynced(
        workspace_id, unique_id
    )

    if not is_unsynced and hasattr(record, "local_sync_status"):
        # Also sync if status indicates data might be incomplete
        needs_sync = record.local_sync_status in [
            LocalSyncStatus.pending.value,
            LocalSyncStatus.error.value,
        ]

    else:
        needs_sync = is_unsynced

    if needs_sync and RemoteStorageController.is_available():
        download_error = await _ensure_experiment_downloaded(
            db, workspace_id, unique_id
        )
        if download_error is not None:
            return download_error

        # Download input files (TIFF/CSV) needed for viewing
        remote_bucket_name = _resolve_workspace_remote_bucket_name(db, workspace_id)
        await RemoteStorageDownloadUtils.sync_experiment_input_files(
            workspace_id, unique_id, remote_bucket_name
        )

    # Validate experiment is displayable (checks if required files exist locally)
    # This should be done BEFORE checking sync status, because on-demand download
    # may have just succeeded
    display_validation = PublishValidator.validate_for_display(
        workspace_id=workspace_id,
        unique_id=unique_id,
    )
    if not display_validation.is_displayable:
        # A 'synced' row that won't display may have lost its files from S3 after
        # publishing; confirm on S3 and demote so the background sync job re-checks it.
        if (
            RemoteStorageController.is_available()
            and hasattr(record, "local_sync_status")
            and record.local_sync_status == LocalSyncStatus.synced.value
        ):
            bucket = _resolve_workspace_remote_bucket_name(db, workspace_id)
            exists = None
            s3_error = None
            if bucket:
                exists, s3_error = await _validate_experiment_exists_in_s3(
                    workspace_id, unique_id, bucket
                )
            if exists is False:
                logger.error(
                    f"Experiment {workspace_id}/{unique_id} marked synced but not "
                    f"present in S3 ({s3_error}); demoting to error"
                )
                db.execute(
                    update(models.ExperimentRecord)
                    .where(models.ExperimentRecord.id == record.id)
                    .where(
                        models.ExperimentRecord.publish_status == PublishStatus.on.value
                    )
                    .where(
                        models.ExperimentRecord.local_sync_status
                        == LocalSyncStatus.synced.value
                    )
                    .values(local_sync_status=LocalSyncStatus.error.value)
                )
                db.commit()
        # Data is not available locally - check if we should return pending or error
        if hasattr(record, "local_sync_status"):
            if record.local_sync_status == LocalSyncStatus.pending.value:
                logger.info(
                    f"Experiment {workspace_id}/{unique_id} is pending sync "
                    f"and not yet available locally"
                )
                return JSONResponse(
                    status_code=status.HTTP_202_ACCEPTED,
                    content={
                        "status": "pending_sync",
                        "message": (
                            "Publishing in progress, check back in a few minutes. "
                            "Experiments are typically available within 5 minutes."
                        ),
                        "retry_after": 300,
                    },
                    headers={"Retry-After": "300"},
                )
        # For error status or no status, return data error
        logger.error(
            f"Experiment {workspace_id}/{unique_id} is not displayable: "
            f"{display_validation.reason}"
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "data_error",
                "message": display_validation.reason
                or "Experiment data is unavailable",
            },
        )

    # Data is valid and available locally - update sync status if needed
    if hasattr(record, "local_sync_status"):
        if record.local_sync_status != LocalSyncStatus.synced.value:
            logger.info(
                f"Experiment {workspace_id}/{unique_id} data is available, "
                f"updating sync status from '{record.local_sync_status}' to 'synced'"
            )
            stmt = (
                update(models.ExperimentRecord)
                .where(models.ExperimentRecord.id == record.id)
                .where(models.ExperimentRecord.version == record.version)
                .values(
                    local_sync_status=LocalSyncStatus.synced.value,
                    version=models.ExperimentRecord.version + 1,
                )
            )
            result = db.execute(stmt)
            db.commit()
            if result.rowcount == 0:
                logger.info(
                    f"Experiment {workspace_id}/{unique_id} sync status "
                    f"already updated by another process"
                )

    return await reproduce_experiment(workspace_id, unique_id)


# NOTE: This function is not specific to the dataview router module.
#   It is defined here for now, but should be moved to a shared module
#   (e.g. storage or workspace utilities) if reuse is needed elsewhere.
def _resolve_workspace_remote_bucket_name(db: Session, workspace_id: str) -> str:
    """Resolve the remote storage bucket name for a workspace from its owner."""
    workspace = (
        db.query(models.Workspace)
        .filter(models.Workspace.id == int(workspace_id))
        .first()
    )
    owner_bucket = None
    if workspace and workspace.user:
        owner_bucket = getattr(workspace.user, "remote_bucket_name", None)
    return owner_bucket or os.environ.get("S3_DEFAULT_BUCKET_NAME")


def _local_config_can_publish(workspace_id: str, unique_id: str) -> bool:
    """Whether the local experiment.yaml already passes publish validation.

    Uses the same PublishValidator the handler uses, so the pre-sync skip
    condition matches the validator's notion of "valid" (a config that merely
    parses may still be missing required fields or be stale).
    """
    return PublishValidator.validate(
        workspace_id=workspace_id,
        unique_id=unique_id,
        user_has_s3_bucket=True,
        check_files_on_disk=True,
    ).can_publish


async def _sync_experiment_config_for_publish(
    workspace_id: str, unique_id: str, remote_bucket_name: str
) -> None:
    """
    Sync metadata from S3 so publish validation reads a valid experiment.yaml.

    Repairs a local config that is missing or fails validation (empty stub,
    missing required fields, stale success state) by downloading fresh metadata
    from S3. An existing invalid config is moved aside first (the download skips
    files that already exist) and restored if the download produces nothing, so
    the only local copy is never lost when S3 has no replacement.

    Best-effort: any failure is logged and swallowed. If the experiment cannot
    be synced the local config is left as-is and PublishValidator.validate
    reports the real 400. The caller resolves the bucket, so this helper does no
    DB access (safe to run concurrently over one Session via asyncio.gather).
    """
    if not RemoteStorageController.is_available() or not remote_bucket_name:
        return

    try:
        config_path = ExptConfigReader.get_config_yaml_path(workspace_id, unique_id)

        # Skip when the local config already passes publish validation.
        if os.path.exists(config_path) and _local_config_can_publish(
            workspace_id, unique_id
        ):
            return

        # Move an existing (invalid) config aside so the download re-fetches it,
        # keeping a backup to restore if the download yields no replacement.
        backup_path = None
        if os.path.exists(config_path):
            backup_path = f"{config_path}.bak"
            try:
                os.replace(config_path, backup_path)
            except FileNotFoundError:
                # Raced with another remover; nothing to move.
                backup_path = None

        try:
            async with RemoteStorageReader(
                remote_bucket_name,
                workspace_id,
                unique_id,
                sync_mode=RemoteExperimentSyncMode.METADATA_ONLY,
            ) as controller:
                await controller.download_experiment_meta(workspace_id, unique_id)
        finally:
            # Discard the backup on a successful download; restore it otherwise.
            if backup_path and os.path.exists(backup_path):
                if os.path.exists(config_path):
                    os.remove(backup_path)
                else:
                    os.replace(backup_path, config_path)
    except Exception as e:
        logger.warning(
            f"Publish pre-sync failed for {workspace_id}/{unique_id}: {e}",
            exc_info=True,
        )


async def _ensure_experiment_downloaded(
    db: Session, workspace_id: str, unique_id: str
) -> Optional[JSONResponse]:
    """
    Download experiment data from remote storage (S3) to local EBS if needed.
    Resolves the owner's bucket from the workspace record.

    Raises:
        HTTPException(404): If experiment not found in remote storage
        HTTPException(423): If remote storage is locked

    Returns JSONResponse(503) via raise-like return if download fails,
    so callers must check the return value.
    """
    remote_bucket_name = _resolve_workspace_remote_bucket_name(db, workspace_id)

    try:
        async with RemoteStorageReader(
            remote_bucket_name, workspace_id, unique_id
        ) as remote_storage_controller:
            logger.info(
                f"Downloading dataview experiment {workspace_id}/{unique_id} "
                f"from remote bucket {remote_bucket_name}"
            )
            available = await remote_storage_controller.download_experiment(
                workspace_id,
                unique_id,
            )

            if not available:
                logger.error(
                    f"Failed to download experiment {workspace_id}/{unique_id} "
                    f"from remote bucket {remote_bucket_name}"
                )
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={
                        "status": "download_error",
                        "message": "Failed to load experiment data, "
                        "please try again later",
                    },
                )
    except RemoteExperimentNotFoundError as e:
        logger.warning(e)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    except RemoteStorageLockError as e:
        logger.warning(e)
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(e))

    return None


async def _validate_experiment_exists_in_s3(
    workspace_id: str, unique_id: str, bucket_name: str
) -> Tuple[Optional[bool], Optional[str]]:
    """
    Check if experiment data exists in S3.

    Args:
        workspace_id: The workspace ID
        unique_id: The experiment unique ID
        bucket_name: The S3 bucket name

    Returns:
        Tuple of (exists, error_message):
        - True: data confirmed present (error_message is None)
        - False: data confirmed absent
        - None: could not check (transient/S3 error) - caller must not treat as absent
    """
    if not RemoteStorageController.is_available():
        return True, None  # Skip validation if S3 not configured

    return await S3StorageController(bucket_name).experiment_prefix_exists(
        workspace_id, unique_id
    )


@router.get(
    "/workflow/reproduce/{workspace_id}/{unique_id}",
    response_model=WorkflowWithResults,
    dependencies=[Depends(is_workspace_available)],
    description="""
- Dataview access wrapper for `GET /workflow/reproduce`
""",
)
async def private_reproduce_experiment(
    workspace_id: str,
    unique_id: str,
    db: Session = Depends(get_db),
):
    # Check target record accessibility
    record = DataviewService.find_dataview_record(db, int(workspace_id), unique_id)

    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # Ensure experiment is available on local EBS (download from S3 if needed)
    # Also try to sync if status is pending/error - the local data might be incomplete
    is_unsynced = RemoteSyncStatusFileUtil.check_sync_status_unsynced(
        workspace_id, unique_id
    )
    needs_sync = is_unsynced

    if needs_sync and RemoteStorageController.is_available():
        download_error = await _ensure_experiment_downloaded(
            db, workspace_id, unique_id
        )
        if download_error is not None:
            return download_error

    return await reproduce_experiment(workspace_id, unique_id)


@router.post(
    "/publish/{id}/{flag}",
    response_model=bool,
    description="""
- Publishing Dataview records with optimistic locking
""",
)
async def publish_dataview_records(
    id: int,
    flag: PublishFlags,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import update

    max_retries = 3
    for attempt in range(max_retries):
        try:
            record = DataviewService.find_user_owned_dataview_record(
                db, id, current_user.id
            )

            if not record:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

            # Validate publish eligibility when publishing
            if flag == PublishFlags.on:
                # Repair missing/stub local config from S3 before validating.
                # Guard on is_available() so the bucket lookup (a DB join) is
                # skipped when remote storage is off.
                if RemoteStorageController.is_available():
                    await _sync_experiment_config_for_publish(
                        str(record.workspace_id),
                        record.uid,
                        _resolve_workspace_remote_bucket_name(
                            db, str(record.workspace_id)
                        ),
                    )

                validation = PublishValidator.validate(
                    workspace_id=str(record.workspace_id),
                    unique_id=record.uid,
                    user_has_s3_bucket=bool(current_user.remote_bucket_name),
                    check_files_on_disk=True,
                )
                if not validation.can_publish:
                    logger.warning(
                        "Publish rejected for experiment %s (%s/%s, name=%s): %s",
                        record.id,
                        record.workspace_id,
                        record.uid,
                        record.name,
                        validation.reason,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=validation.reason,
                    )

            # Store current version for optimistic locking
            current_version = record.version

            # Update publish status
            new_publish_status = int(flag == PublishFlags.on)

            # Check if status is actually changing
            if record.publish_status == new_publish_status:
                logger.info(
                    f"Experiment {record.id} already "
                    f"has publish_status={new_publish_status}, no change needed"
                )
                return True

            # Validate S3 data exists before publishing
            if flag == PublishFlags.on:
                remote_storage_type = RemoteStorageType.get_activated_type()
                if remote_storage_type == RemoteStorageType.S3:
                    bucket_name = current_user.remote_bucket_name or os.environ.get(
                        "S3_DEFAULT_BUCKET_NAME"
                    )
                    if bucket_name:
                        exists, error_msg = await _validate_experiment_exists_in_s3(
                            str(record.workspace_id),
                            record.uid,
                            bucket_name,
                        )
                        if not exists:
                            raise HTTPException(
                                status_code=status.HTTP_400_BAD_REQUEST,
                                detail=f"Cannot publish: {error_msg}",
                            )
                else:
                    # Currently not supported
                    pass

            # Determine new sync status
            new_sync_status = (
                LocalSyncStatus.pending.value
                if flag == PublishFlags.on
                else LocalSyncStatus.synced.value
            )

            # Use SQLAlchemy's update() with WHERE clause for optimistic locking
            # This is the proper ORM way to implement optimistic locking
            stmt = (
                update(models.ExperimentRecord)
                .where(models.ExperimentRecord.id == record.id)
                .where(models.ExperimentRecord.version == current_version)
                .values(
                    publish_status=new_publish_status,
                    local_sync_status=new_sync_status,
                    version=models.ExperimentRecord.version + 1,
                )
            )

            result = db.execute(stmt)
            db.commit()

            # Check if update actually occurred (rowcount = 0 means version conflict)
            if result.rowcount == 0:
                # Version mismatch - concurrent modification detected
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Optimistic lock conflict for experiment {id}, "
                        f"retrying (attempt {attempt + 1}/{max_retries})"
                    )
                    continue
                else:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Concurrent modification detected. Please try again.",
                    )

            # Success - log the action
            action = "Published" if flag == PublishFlags.on else "Unpublished"
            logger.info(
                f"{action} experiment {record.id}, " f"sync_status={new_sync_status}"
            )
            return True

        except HTTPException:
            # Re-raise HTTP exceptions (404, 409)
            raise
        except Exception as e:
            # Unexpected error
            db.rollback()
            logger.error(f"Error publishing experiment {id}: {e}", exc_info=True)
            if attempt < max_retries - 1:
                logger.warning(f"Retrying (attempt {attempt + 1}/{max_retries})")
                continue
            else:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to publish experiment: {str(e)}",
                )

    # Should never reach here
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Failed to publish experiment after multiple retries",
    )


@router.post(
    "/multiple/publish/{flag}",
    response_model=bool,
    description="""
- Publishing Dataview records in bulk
- Validates each record before publishing; fails if any record cannot be published
""",
)
async def multiple_publish_dataview_records(
    ids: List[int],
    flag: PublishFlags,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Validate all records before publishing
    if flag == PublishFlags.on:
        # First check S3 bucket (applies to all)
        if not current_user.remote_bucket_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot publish data: No cloud storage bucket configured "
                "for your account. Please contact support to enable publishing.",
            )

        # Resolve owned records once, de-duplicating ids (select-all across
        # pages can repeat ids; duplicates would collide on the sync lock file).
        owned_records = []
        seen_ids = set()
        for record_id in ids:
            if record_id in seen_ids:
                continue
            seen_ids.add(record_id)
            record = DataviewService.find_user_owned_dataview_record(
                db, record_id, current_user.id
            )
            if record:
                owned_records.append((record_id, record))

        # Repair missing/stub local config from S3 before validating.
        # Guarded on is_available() so bucket lookups (DB joins) are skipped when
        # remote storage is off.
        if RemoteStorageController.is_available():
            # Resolve the owner bucket once per workspace (bulk is typically a
            # single workspace) and keep DB access out of the gathered coroutines.
            bucket_by_workspace = {}
            for _, record in owned_records:
                ws_id = str(record.workspace_id)
                if ws_id not in bucket_by_workspace:
                    bucket_by_workspace[ws_id] = _resolve_workspace_remote_bucket_name(
                        db, ws_id
                    )

            # Bound the fan-out of concurrent RemoteStorageReader clients.
            semaphore = asyncio.Semaphore(PUBLISH_PRESYNC_CONCURRENCY)

            async def _bounded_sync(record):
                async with semaphore:
                    await _sync_experiment_config_for_publish(
                        str(record.workspace_id),
                        record.uid,
                        bucket_by_workspace[str(record.workspace_id)],
                    )

            # return_exceptions keeps one record's failure from aborting the batch.
            await asyncio.gather(
                *(_bounded_sync(record) for _, record in owned_records),
                return_exceptions=True,
            )

        # Validate each record
        failed_records = []
        for record_id, record in owned_records:
            validation = PublishValidator.validate(
                workspace_id=str(record.workspace_id),
                unique_id=record.uid,
                user_has_s3_bucket=True,  # Already checked above
                check_files_on_disk=True,
            )
            if not validation.can_publish:
                failed_records.append(
                    {
                        "id": record_id,
                        "name": record.name,
                        "reason": validation.reason,
                    }
                )

        if failed_records:
            logger.warning(
                "Bulk publish rejected %d record(s): %s",
                len(failed_records),
                "; ".join(
                    f"{rec['name']} ({rec['id']}): {rec['reason']}"
                    for rec in failed_records
                ),
            )
            # Return error with details about which records failed
            detail = "Some experiments cannot be published:\n"
            for rec in failed_records[:5]:  # Limit to first 5
                detail += f"- {rec['name']}: {rec['reason']}\n"
            if len(failed_records) > 5:
                detail += f"... and {len(failed_records) - 5} more"
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

    DataviewService.multiple_publish_dataview_records(db, current_user.id, ids, flag)

    return True
