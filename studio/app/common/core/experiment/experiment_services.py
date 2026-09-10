from datetime import datetime
from glob import glob
from typing import Optional

from fastapi import HTTPException, status
from sqlmodel import Session

from studio.app.common.core.experiment.background_task_service import (
    BackgroundTaskService,
)
from studio.app.common.core.experiment.experiment import ExptConfig
from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.experiment.experiment_record_services import (
    ExperimentRecordService,
)
from studio.app.common.core.experiment.experiment_writer import ExptDataWriter
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteSyncLockFileUtil,
)
from studio.app.common.core.utils.datetime_utils import parse_datetime_for_timezone
from studio.app.common.core.workflow.workflow_runner import WorkflowRunner
from studio.app.common.schemas.experiment import CopyItem
from studio.app.const import DATE_FORMAT

logger = AppLogger.get_logger()


class ExperimentService:
    @staticmethod
    def _started_at(config: ExptConfig) -> datetime:
        # ordered across experiments, so different user zones must resolve first
        return parse_datetime_for_timezone(
            config.started_at, DATE_FORMAT, config.timezone
        )

    @classmethod
    def get_last_experiment(cls, workspace_id: str):
        last_expt_config: Optional[ExptConfig] = None
        config_paths = glob(ExptConfigReader.get_config_yaml_wild_path(workspace_id))

        for path in config_paths:
            config = ExptConfigReader.read_from_path(path)
            if not last_expt_config:
                last_expt_config = config
            elif cls._started_at(config) > cls._started_at(last_expt_config):
                last_expt_config = config

        return last_expt_config

    @classmethod
    async def delete_experiment(
        cls,
        db: Session,
        remote_bucket_name: str,
        workspace_id: str,
        unique_id: str,
        auto_commit: bool = False,
    ) -> bool:
        if RemoteStorageController.is_available():
            # Check for remote-sync-lock-file
            # - If lock file exists, an exception is raised (raise_error=True)
            RemoteSyncLockFileUtil.check_sync_lock_file(
                workspace_id, unique_id, raise_error=True
            )

        # Delete experiment data (S3 and local filesystem)
        s3_deletion_success = await ExptDataWriter(
            remote_bucket_name, workspace_id, unique_id
        ).delete_data()

        # Delete experiment database record
        if ExperimentRecordService.is_available():
            try:
                ExperimentRecordService.delete_record(
                    db, workspace_id, unique_id, auto_commit
                )
            except Exception as db_error:
                # S3 deleted but DB deletion failed - mark as orphaned
                if s3_deletion_success:
                    logger.error(
                        f"DB deletion failed after S3 deletion for experiment "
                        f"[{workspace_id}/{unique_id}]: {db_error}"
                    )
                    try:
                        # Mark the record as orphaned
                        ExperimentRecordService.mark_as_orphaned(
                            db,
                            workspace_id,
                            unique_id,
                            error_message="S3 data deleted, DB record retained due to "
                            f"deletion error: {str(db_error)[:200]}",
                        )
                        if auto_commit:
                            db.commit()
                    except Exception as mark_error:
                        logger.error(
                            f"Failed to mark experiment as orphaned: {mark_error}"
                        )
                        db.rollback()
                    # Return partial success - S3 cleaned up but DB record remains
                    return False
                else:
                    # S3 also failed, just re-raise the error
                    raise

        return s3_deletion_success

    @classmethod
    def queue_experiment_deletion(
        cls,
        user_id: int,
        workspace_id: int,
        unique_id: str,
    ) -> dict:
        """
        Queue experiment deletion as a background task.
        Returns immediately - deletion continues even if user logs out.

        Args:
            user_id: User initiating the deletion
            workspace_id: Workspace containing the experiment
            unique_id: Experiment unique ID

        Returns:
            Dict with task_id and status, or error message
        """
        task_id = BackgroundTaskService.queue_experiment_deletion(
            user_id=user_id,
            workspace_id=workspace_id,
            experiment_uid=unique_id,
        )

        if task_id:
            logger.info(
                f"Queued experiment deletion: user={user_id}, "
                f"workspace={workspace_id}, experiment={unique_id}, task={task_id}"
            )
            return {
                "status": "queued",
                "task_id": task_id,
                "message": "Deletion queued successfully",
            }
        else:
            logger.error(
                f"Failed to queue experiment deletion: workspace={workspace_id}, "
                f"experiment={unique_id}"
            )
            return {
                "status": "error",
                "message": "Failed to queue deletion task",
            }

    @classmethod
    async def copy_experiment(
        cls, db: Session, remote_bucket_name: str, workspace_id: int, copyItem: CopyItem
    ):
        created_unique_ids = []
        try:
            for unique_id in copyItem.uidList:
                config = ExptConfigReader.read(workspace_id, unique_id)
                new_unique_id = WorkflowRunner.create_workflow_unique_id()
                new_name = f"{config.name}_copy"
                success = await ExptDataWriter(
                    remote_bucket_name,
                    workspace_id,
                    unique_id,
                ).copy_data(new_unique_id, new_name)

                if not success:
                    raise Exception(f"Failed to copy data for unique_id: {unique_id}")

                if ExperimentRecordService.is_available():
                    ExperimentRecordService.copy_record(
                        db,
                        workspace_id,
                        unique_id,
                        new_unique_id,
                        new_name,
                        auto_commit=True,
                    )

                created_unique_ids.append(new_unique_id)
                logger.info(f"Copied experiment {unique_id} to {new_unique_id}")
            return True
        except Exception as e:
            logger.error(e, exc_info=True)
            # Clean up partially created data
            for created_unique_id in created_unique_ids:
                try:
                    await ExptDataWriter(
                        remote_bucket_name,
                        str(workspace_id),
                        created_unique_id,
                    ).delete_data()
                    logger.info(f"Cleaned up data for unique_id: {created_unique_id}")
                except Exception as cleanup_error:
                    logger.error(cleanup_error, exc_info=True)
                    logger.error(
                        f"Failed to clean up data for unique_id: {created_unique_id}",
                        exc_info=True,
                    )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to copy record. created files have been removed.",
            )
