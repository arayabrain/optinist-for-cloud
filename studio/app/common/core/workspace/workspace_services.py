import os
import shutil
from pathlib import Path

import yaml
from sqlalchemy.exc import NoResultFound
from sqlmodel import Session, delete, update

from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.experiment.experiment_writer import ExptConfigWriter
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.mode import MODE
from studio.app.common.core.utils.file_reader import get_folder_size
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.db.database import session_scope
from studio.app.common.models.experiment import ExperimentRecord
from studio.app.common.models.workspace import Workspace
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()


def load_experiment_success_status(yaml_path: str) -> str:
    if not os.path.exists(yaml_path):
        return "not_found"

    try:
        with open(yaml_path, "r") as file:
            data = yaml.safe_load(file)
            return data.get("success", "unknown")
    except yaml.YAMLError:
        return "error"


class WorkspaceService:
    @classmethod
    def _update_exp_data_usage_yaml(cls, workspace_id: str, unique_id: str, data_usage):
        # Read config
        config = ExptConfigReader.read_raw(workspace_id, unique_id)
        if not config:
            logger.error(f"[{workspace_id}/{unique_id}] does not exist")
            return

        config["data_usage"] = data_usage

        # Update & Write config
        ExptConfigWriter.write_raw(workspace_id, unique_id, config)

    @classmethod
    def _update_exp_data_usage_db(
        cls, workspace_id: str, unique_id: str, data_usage: int
    ):
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
                exp.data_usage = data_usage
            except NoResultFound:
                exp = ExperimentRecord(
                    workspace_id=workspace_id,
                    uid=unique_id,
                    data_usage=data_usage,
                )
                db.add(exp)

    @classmethod
    def is_data_usage_available(cls) -> bool:
        # The workspace data usage feature is available in multiuser mode
        available = MODE.IS_MULTIUSER
        return available

    @classmethod
    def update_experiment_data_usage(cls, workspace_id: str, unique_id: str):
        workflow_dir = join_filepath([DIRPATH.OUTPUT_DIR, workspace_id, unique_id])
        if not os.path.exists(workflow_dir):
            logger.error(f"'{workflow_dir}' does not exist")
            return

        data_usage = get_folder_size(workflow_dir)

        cls._update_exp_data_usage_yaml(workspace_id, unique_id, data_usage)

        if cls.is_data_usage_available():
            cls._update_exp_data_usage_db(workspace_id, unique_id, data_usage)

    @classmethod
    def update_workspace_data_usage(cls, db: Session, workspace_id: str):
        workspace_dir = join_filepath([DIRPATH.INPUT_DIR, workspace_id])
        if not os.path.exists(workspace_dir):
            logger.error(f"'{workspace_dir}' does not exist")
            return

        input_data_usage = get_folder_size(workspace_dir)
        db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(input_data_usage=input_data_usage)
        )
        db.commit()

    @classmethod
    def delete_workspace_experiment(
        cls, db: Session, workspace_id: str, unique_id: str
    ):
        db.execute(
            delete(ExperimentRecord).where(
                ExperimentRecord.workspace_id == workspace_id,
                ExperimentRecord.uid == unique_id,
            )
        )

    @classmethod
    def delete_workspace_experiment_by_workspace_id(cls, db: Session, workspace_id):
        db.execute(
            delete(ExperimentRecord).where(
                ExperimentRecord.workspace_id == workspace_id
            )
        )

    @classmethod
    def delete_workspace_data_by_user_id(cls, db: Session, user_id):
        db.execute(
            update(Workspace).where(Workspace.user_id == user_id).values(deleted=True)
        )

    @classmethod
    def sync_workspace_experiment(cls, db: Session, workspace_id: str):
        folder = join_filepath([DIRPATH.OUTPUT_DIR, workspace_id])
        if not os.path.exists(folder):
            logger.error(f"'{folder}' does not exist")
            return
        exp_records = []

        for exp_folder in Path(folder).iterdir():
            unique_id = exp_folder.name
            data_usage = get_folder_size(exp_folder.as_posix())

            cls._update_exp_data_usage_yaml(workspace_id, unique_id, data_usage)

            exp_records.append(
                ExperimentRecord(
                    workspace_id=workspace_id,
                    uid=unique_id,
                    data_usage=data_usage,
                )
            )

        if cls.is_data_usage_available():
            db.execute(
                delete(ExperimentRecord).where(
                    ExperimentRecord.workspace_id == workspace_id
                )
            )
            db.bulk_save_objects(exp_records)

    @classmethod
    def delete_workspace_data(cls, workspace_id: int, db: Session):
        logger.info(f"Deleting workspace data for workspace '{workspace_id}'")

        # Step 1: Define all relevant paths
        workspace_dir = join_filepath([DIRPATH.OUTPUT_DIR, str(workspace_id)])
        input_dir = join_filepath([DIRPATH.INPUT_DIR, str(workspace_id)])

        # Step 2: Delete experiment folders under workspace
        if os.path.exists(workspace_dir):
            for experiment_id in os.listdir(workspace_dir):
                experiment_path = join_filepath([workspace_dir, experiment_id])
                if not os.path.isdir(experiment_path):
                    continue

                try:
                    yaml_path = join_filepath([experiment_path, "experiment.yaml"])
                    status = load_experiment_success_status(yaml_path)

                    if status == "running":
                        logger.warning(
                            f"Skipping deletion of running experiment '{experiment_id}'"
                        )
                        continue  # skip without raising

                    if ExperimentRecord.exists(workspace_id, uid=str(experiment_id)):
                        WorkspaceService.delete_workspace_experiment(
                            db=db, workspace_id=workspace_id, unique_id=experiment_id
                        )

                    shutil.rmtree(experiment_path)
                    logger.info(f"Deleted experiment data at: {experiment_path}")

                except Exception as e:
                    logger.error(
                        f"Failed to delete experiment '{experiment_id}': {e}",
                        exc_info=True,
                    )
        else:
            logger.warning(f"Workspace directory '{workspace_dir}' does not exist")

        # Step 3: Delete the workspace directory itself
        if os.path.exists(workspace_dir):
            try:
                shutil.rmtree(workspace_dir)
                logger.info(f"Deleted workspace directory: {workspace_dir}")
            except Exception as e:
                logger.error(
                    f"Failed to delete workspace directory '{workspace_dir}': {e}",
                    exc_info=True,
                )
        else:
            logger.warning(f"'{workspace_dir}' already deleted or never existed")

        # Step 4: Delete input directory
        if os.path.exists(input_dir):
            try:
                shutil.rmtree(input_dir)
                logger.info(f"Deleted input data at: {input_dir}")
            except Exception as e:
                logger.error(
                    f"Failed to delete input directory '{input_dir}': {e}",
                    exc_info=True,
                )
        else:
            logger.warning(f"'{input_dir}' does not exist, skipping")
