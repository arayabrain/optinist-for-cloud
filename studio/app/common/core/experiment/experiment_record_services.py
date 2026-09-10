from fastapi import HTTPException, status
from sqlalchemy.exc import NoResultFound
from sqlmodel import Session, delete

from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.mode import MODE
from studio.app.common.core.workflow.workflow import NodeRunStatus
from studio.app.common.core.workflow.workflow_reader import WorkflowConfigReader
from studio.app.common.db.database import session_scope
from studio.app.common.models.experiment import ExperimentRecord
from studio.app.common.schemas.dataview import DataviewThumbnails

logger = AppLogger.get_logger()


class ExperimentRecordService:
    @classmethod
    def is_available(cls) -> bool:
        # ExperimentRecordService is available in multiuser mode
        available = MODE.IS_MULTIUSER
        return available

    @classmethod
    def record_exists(cls, workspace_id: str, unique_id: str) -> bool:
        with session_scope() as db:
            return (
                db.query(ExperimentRecord.id)
                .filter(
                    ExperimentRecord.workspace_id == workspace_id,
                    ExperimentRecord.uid == unique_id,
                )
                .first()
                is not None
            )

    @classmethod
    def regist_record_on_workflow_completed(
        cls, workspace_id: str, unique_id: str, generate_thumbnails: bool = True
    ):
        """
        Processing upon workflow completion

        Args:
            workspace_id: Workspace identifier
            unique_id: Experiment unique identifier
            generate_thumbnails: If True, generate PNG thumbnail images for fast
                DataView loading (default True)
        """

        from studio.app.common.core.dataview.dataview_services import DataviewService

        experiment_config = ExptConfigReader.read(workspace_id, unique_id)
        workflow_config = WorkflowConfigReader.read(workspace_id, unique_id)

        # Get input data paths
        input_paths = WorkflowConfigReader.extract_input_file_paths(workflow_config)

        # Get original thumbnail paths (TIFF/JSON references)
        (
            original_thumbnails,
            dataset_paths,
        ) = DataviewService.make_dataview_thumnail_paths(
            workspace_id,
            unique_id,
            experiment_config,
            workflow_config_=workflow_config,
        )

        # Generate PNG thumbnails if requested (for fast DataView loading)
        thumbnails = original_thumbnails
        if generate_thumbnails:
            generated_thumbnails = DataviewService.generate_thumbnail_images(
                workspace_id,
                unique_id,
                image_path=original_thumbnails.image_url,
                roi_path=original_thumbnails.roi_url,
                dataset_paths=dataset_paths,
            )
            # Use generated PNG paths if available, fall back to originals
            thumbnails = DataviewThumbnails(
                image_url=generated_thumbnails.image_url
                or original_thumbnails.image_url,
                roi_url=generated_thumbnails.roi_url or original_thumbnails.roi_url,
            )

        workflow_success = experiment_config.success == NodeRunStatus.SUCCESS.value
        analyzed_at = experiment_config.finished_at or experiment_config.started_at

        # Update ExperimentRecord to database
        with session_scope() as db:
            try:
                exp = (
                    db.query(ExperimentRecord)
                    .filter(
                        ExperimentRecord.workspace_id == workspace_id,
                        ExperimentRecord.uid == unique_id,
                    )
                    .one()
                )
                exp.name = experiment_config.name
                exp.input_paths = input_paths
                exp.thumbnails = dict(thumbnails)
                exp.success = workflow_success
                exp.analyzed_at = analyzed_at
                exp.has_intermediates = True
                exp.has_outputs = True
                exp.has_inputs = True
                exp.has_nwb = experiment_config.hasNWB

            except NoResultFound:
                exp = ExperimentRecord(
                    workspace_id=workspace_id,
                    uid=unique_id,
                    name=experiment_config.name,
                    input_paths=input_paths,
                    thumbnails=dict(thumbnails),
                    success=workflow_success,
                    analyzed_at=analyzed_at,
                    has_intermediates=True,
                    has_outputs=True,
                    has_inputs=True,
                    has_nwb=experiment_config.hasNWB,
                )
                db.add(exp)

    @classmethod
    def delete_record(
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
    def copy_record(
        cls,
        db: Session,
        workspace_id: str,
        unique_id: str,
        new_unique_id: str,
        _new_name: str,
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

            # Create new record by copying all attributes except primary key
            new_exp_data = {}
            for column in ExperimentRecord.__table__.columns:
                if column.name not in ["id"]:
                    new_exp_data[column.name] = getattr(exp, column.name)

            # Override specific columns
            new_exp_data["uid"] = new_unique_id
            new_exp_data["name"] = _new_name
            new_exp_data["publish_status"] = False

            new_exp = ExperimentRecord(**new_exp_data)
            db.add(new_exp)

            if auto_commit:
                db.commit()

        except NoResultFound:
            # If it fails roll back the transaction
            logger.warning(
                f"Experiment [{unique_id}] not found in workspace [{workspace_id}]"
            )

    @classmethod
    def update_name(cls, workspace_id: str, unique_id: str, new_name: str):
        with session_scope() as db:
            try:
                exp = (
                    db.query(ExperimentRecord)
                    .filter(
                        ExperimentRecord.workspace_id == workspace_id,
                        ExperimentRecord.uid == unique_id,
                    )
                    .one()
                )
                exp.name = new_name

            except Exception as e:
                db.rollback()
                logger.error(e, exc_info=True)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Failed to update ExperimentRecord"
                    f" [{workspace_id}/{unique_id}].",
                )

    @classmethod
    def mark_as_orphaned(
        cls,
        db: Session,
        workspace_id: str,
        unique_id: str,
        error_message: str,
    ):
        """
        Mark an experiment as orphaned when S3 data was deleted but DB deletion failed.

        This is called during deletion recovery to prevent ghost
        experiments that appear in the UI but have no underlying data.

        Args:
            db: Database session
            workspace_id: Workspace identifier
            unique_id: Experiment unique identifier
            error_message: Description of what went wrong
        """
        try:
            exp = (
                db.query(ExperimentRecord)
                .filter(
                    ExperimentRecord.workspace_id == workspace_id,
                    ExperimentRecord.uid == unique_id,
                )
                .one()
            )
            exp.deletion_error = error_message
            logger.warning(
                f"Marked experiment [{workspace_id}/{unique_id}] as orphaned: "
                f"{error_message}"
            )

        except NoResultFound:
            # Experiment record doesn't exist, nothing to mark
            logger.warning(
                f"Cannot mark experiment as orphaned - record not found: "
                f"[{workspace_id}/{unique_id}]"
            )
