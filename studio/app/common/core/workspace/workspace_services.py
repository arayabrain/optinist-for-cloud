import os
import shutil
from pathlib import Path

import yaml
from sqlalchemy.exc import NoResultFound
from sqlmodel import Session, delete, update

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
    def _update_exp_data_usage_yaml(cls, exp_filepath, data_usage):
        if not os.path.isfile(exp_filepath):
            logger.error(f"'{exp_filepath}' does not exist")
            return

        with open(exp_filepath, "r") as f:
            config = yaml.safe_load(f)
            config["data_usage"] = data_usage

        with open(exp_filepath, "w") as f:
            yaml.dump(config, f, sort_keys=False)

    @classmethod
    def _update_exp_data_usage_db(cls, workspace_id, unique_id, data_usage):
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
    def update_experiment_data_usage(cls, workspace_id, unique_id):
        workflow_dir = join_filepath([DIRPATH.OUTPUT_DIR, workspace_id, unique_id])
        if not os.path.exists(workflow_dir):
            logger.error(f"'{workflow_dir}' does not exist")
            return

        exp_filepath = join_filepath([workflow_dir, DIRPATH.EXPERIMENT_YML])
        data_usage = get_folder_size(workflow_dir)

        cls._update_exp_data_usage_yaml(exp_filepath, data_usage)

        if not MODE.IS_STANDALONE:
            cls._update_exp_data_usage_db(workspace_id, unique_id, data_usage)

    @classmethod
    def update_workspace_data_usage(cls, db: Session, workspace_id):
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
    def delete_workspace_experiment(cls, db: Session, workspace_id, unique_id):
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
    def sync_workspace_experiment(cls, db: Session, workspace_id):
        folder = join_filepath([DIRPATH.OUTPUT_DIR, workspace_id])
        if not os.path.exists(folder):
            logger.error(f"'{folder}' does not exist")
            return
        exp_records = []

        for exp_folder in Path(folder).iterdir():
            data_usage = get_folder_size(exp_folder.as_posix())
            cls._update_exp_data_usage_yaml(
                (exp_folder / DIRPATH.EXPERIMENT_YML).as_posix(), data_usage
            )
            exp_records.append(
                ExperimentRecord(
                    workspace_id=workspace_id,
                    uid=exp_folder.name,
                    data_usage=data_usage,
                )
            )

        if not MODE.IS_STANDALONE:
            db.execute(
                delete(ExperimentRecord).where(
                    ExperimentRecord.workspace_id == workspace_id
                )
            )
            db.bulk_save_objects(exp_records)

    @classmethod
    def delete_workspace_data(cls, workspace_id: int, db: Session):
        logger.info(f"Deleting workspace data for workspace '{workspace_id}'")

        workspace_dir = join_filepath([DIRPATH.OUTPUT_DIR, str(workspace_id)])
        if not os.path.exists(workspace_dir):
            logger.error(f"'{workspace_dir}' does not exist")
            return

        for experiment_id in os.listdir(workspace_dir):
            experiment_path = join_filepath([workspace_dir, experiment_id])
            if not os.path.isdir(experiment_path):
                continue

            yaml_path = join_filepath([experiment_path, "experiment.yaml"])
            status = load_experiment_success_status(yaml_path)

            if status == "running":
                logger.info(
                    f"Skipping delete of experiment '{experiment_id}' (status: running)"
                )
                raise Exception(
                    f"Experiment '{experiment_id}' is still running. Cannot delete."
                )

            try:
                if ExperimentRecord.exists(workspace_id, uid=str(experiment_id)):
                    WorkspaceService.delete_workspace_experiment(
                        db=db, workspace_id=workspace_id, unique_id=experiment_id
                    )

                shutil.rmtree(experiment_path)
                logger.info(f"Deleted experiment data at: {experiment_path}")
            except Exception as e:
                logger.error(f"Failed to delete {experiment_path}: {e}")

        # Delete the workspace directory itself
        try:
            shutil.rmtree(workspace_dir)
            logger.info(f"Deleted workspace data at: {workspace_dir}")
        except Exception as e:
            logger.error(f"Failed to delete {workspace_dir}: {e}")

        # Delete INPUT_DIR contents
        input_dir = join_filepath([DIRPATH.INPUT_DIR, str(workspace_id)])
        if os.path.exists(input_dir):
            try:
                shutil.rmtree(input_dir)
                logger.info(f"Deleted input data at: {input_dir}")
            except Exception as e:
                logger.error(f"Failed to delete {input_dir}: {e}")
        else:
            logger.warning(f"'{input_dir}' does not exist, skipping INPUT_DIR deletion")
