import os
import shutil

from fastapi import HTTPException, status
from sqlalchemy.exc import NoResultFound
from sqlmodel import Session, delete

from studio.app.common.core.experiment.experiment_writer import ExptDataWriter
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.workflow.workflow_runner import WorkflowRunner
from studio.app.common.core.workspace.workspace_data_capacity_services import (
    WorkspaceDataCapacityService,
)
from studio.app.common.models.experiment import ExperimentRecord
from studio.app.common.models.workspace import Workspace
from studio.app.common.schemas.experiment import CopyItem
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()


class WorkspaceService:
    @classmethod
    def delete_workspace_experiment(
        cls, db: Session, workspace_id: str, unique_id: str, auto_commit: bool = False
    ) -> bool:
        # Delete experiment data
        deleted = ExptDataWriter(workspace_id, unique_id).delete_data()

        # Delete experiment database record
        if WorkspaceDataCapacityService.is_available():
            cls._delete_workspace_experiment_db(
                db, workspace_id, unique_id, auto_commit
            )

        return deleted

    @classmethod
    def _delete_workspace_experiment_db(
        cls, db: Session, workspace_id: str, unique_id: str, auto_commit: bool = False
    ):
        db.execute(
            delete(ExperimentRecord).where(
                ExperimentRecord.workspace_id == workspace_id,
                ExperimentRecord.uid == unique_id,
            )
        )

        if auto_commit:
            db.commit()

    @classmethod
    def copy_workspace_experiment(
        cls, db: Session, workspace_id: int, copyItem: CopyItem
    ):
        created_unique_ids = []
        try:
            for unique_id in copyItem.uidList:
                new_unique_id = WorkflowRunner.create_workflow_unique_id()
                ExptDataWriter(
                    workspace_id,
                    unique_id,
                ).copy_data(new_unique_id)

                if WorkspaceDataCapacityService.is_available():
                    cls._copy_workspace_experiment_db(
                        db,
                        workspace_id,
                        unique_id,
                        new_unique_id,
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
                    ExptDataWriter(
                        workspace_id,
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

    @classmethod
    def _copy_workspace_experiment_db(
        cls,
        db: Session,
        workspace_id: str,
        unique_id: str,
        new_unique_id: str,
        auto_commit: bool = False,
    ):
        try:
            exp = (
                db.query(ExperimentRecord)
                .filter(
                    ExperimentRecord.workspace_id == workspace_id,
                    ExperimentRecord.uid == unique_id,
                )
                .one()
            )
            new_exp = ExperimentRecord(
                workspace_id=workspace_id,
                uid=new_unique_id,
                data_usage=exp.data_usage,
            )
            db.add(new_exp)
            if auto_commit:
                db.commit()
        except NoResultFound:
            # If it fails roll back the transaction
            logger.error(
                f"Experiment {unique_id} not found in workspace {workspace_id}"
            )

    @classmethod
    def delete_workspace_contents(
        cls,
        db: Session,
        ws: Workspace,
    ):
        workspace_id = str(ws.id)
        logger.info(f"Deleting workspace data for workspace '{workspace_id}'")

        workspace_dir = join_filepath([DIRPATH.OUTPUT_DIR, workspace_id])
        hasDeleteDataArr = []

        # Delete experiment folders under workspace
        if os.path.exists(workspace_dir):
            for experiment_id in os.listdir(workspace_dir):
                # Skip hidden files and directories
                if experiment_id.startswith("."):
                    continue

                deleted = cls.delete_workspace_experiment(
                    db, workspace_id, experiment_id, auto_commit=False
                )

                hasDeleteDataArr.append(deleted)
        else:
            logger.warning(f"Workspace directory '{workspace_dir}' does not exist")

        if all(hasDeleteDataArr):
            # Delete the workspace directory itself
            cls.delete_workspace_files(workspace_id=workspace_id)

            # Delete input directory
            cls.delete_workspace_files(workspace_id=workspace_id, is_input_dir=True)

            # Soft delete the workspace
            ws.deleted = True
        else:
            # Throw Exception if data was not deleted
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete workspace '{workspace_id}'",
            )

    @classmethod
    def delete_workspace_files(cls, workspace_id: str, is_input_dir: bool = False):
        if is_input_dir:
            directory = join_filepath([DIRPATH.INPUT_DIR, workspace_id])
        else:
            directory = join_filepath([DIRPATH.OUTPUT_DIR, workspace_id])
        try:
            if os.path.exists(directory):
                shutil.rmtree(directory)
                logger.info(f"Deleted directory: {directory}")
            else:
                logger.warning(f"'{directory}' already deleted or never existed")
        except Exception as e:
            logger.error(
                f"Failed to delete directory '{directory}': {e}",
                exc_info=True,
            )

    @classmethod
    def process_workspace_deletion(cls, db: Session, workspace_id: str, user_id: str):
        try:
            # Search for workspace
            ws: Workspace = (
                db.query(Workspace)
                .filter(
                    Workspace.id == workspace_id,
                    Workspace.user_id == user_id,
                    Workspace.deleted.is_(False),
                )
                .first()
            )

            if not ws:
                raise HTTPException(status_code=404, detail="Workspace not found")

            # Delete workspace storage files
            cls.delete_workspace_contents(db, ws)

            # Commit all DB changes before doing anything irreversible
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error(
                "Error deleting or updating workspace %s: %s",
                workspace_id,
                e,
                exc_info=True,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete or update workspace {workspace_id}.",
            )
