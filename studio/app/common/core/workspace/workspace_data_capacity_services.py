import os
from pathlib import Path

import yaml
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound
from sqlmodel import Session, delete, select, update

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


class WorkspaceDataCapacityService:
    @classmethod
    def is_available(cls) -> bool:
        # The workspace data capcaticy feature is available in multiuser mode
        available = MODE.IS_MULTIUSER
        return available

    @classmethod
    def update_experiment_data_usage(cls, workspace_id: str, unique_id: str):
        workflow_dir = join_filepath([DIRPATH.OUTPUT_DIR, workspace_id, unique_id])
        if not os.path.exists(workflow_dir):
            logger.warning(f"'{workflow_dir}' does not exist")
            return

        data_usage = get_folder_size(workflow_dir)

        cls._update_exp_data_usage_yaml(workspace_id, unique_id, data_usage)

        if cls.is_available():
            cls._update_exp_data_usage_db(workspace_id, unique_id, data_usage)

    @classmethod
    def _update_exp_data_usage_yaml(cls, workspace_id: str, unique_id: str, data_usage):
        # Read experiment config
        config = ExptConfigReader.read(workspace_id, unique_id)
        if not config:
            logger.warning(f"[{workspace_id}/{unique_id}] does not exist")
            return

        # Make overwrite params
        update_params = {"data_usage": data_usage}

        # Overwrite experiment config
        ExptConfigWriter(workspace_id, unique_id).overwrite(update_params)

    # MySQL advisory-lock namespace for _update_exp_data_usage_db (name max 64).
    _EXP_DATA_USAGE_LOCK_PREFIX = "exp_data_usage"
    _EXP_DATA_USAGE_LOCK_TIMEOUT_SECONDS = 10

    @classmethod
    def _update_exp_data_usage_db(
        cls, workspace_id: str, unique_id: str, data_usage: int
    ):
        # Concurrent writers (main /run/result task + executor) write the same
        # row. Two safeguards:
        #   - Core UPDATE with an existence SELECT (not UPDATE rowcount, which is
        #     0 for a same-value write on MySQL) avoids the ORM stale-data error.
        #   - (workspace_id, uid) has no unique constraint, so the check-then-
        #     write is serialized by a MySQL advisory lock, held on a dedicated
        #     lock_db session and released only AFTER the inner write commits.
        #     Spanning the commit is essential: otherwise a second writer could
        #     acquire the lock and run its existence SELECT before this INSERT is
        #     visible (REPEATABLE READ) and insert a duplicate. Best-effort:
        #     proceeds unlocked if the lock backend is unavailable.
        lock_name = f"{cls._EXP_DATA_USAGE_LOCK_PREFIX}_{workspace_id}_{unique_id}"[:64]

        with session_scope() as lock_db:
            got_lock = False
            try:
                got_lock = (
                    lock_db.execute(
                        text("SELECT GET_LOCK(:name, :timeout) AS r"),
                        {
                            "name": lock_name,
                            "timeout": cls._EXP_DATA_USAGE_LOCK_TIMEOUT_SECONDS,
                        },
                    ).scalar()
                    == 1
                )
            except Exception as e:
                logger.warning(
                    f"Advisory lock unavailable for experiment data usage "
                    f"[{workspace_id}/{unique_id}]: {e}; proceeding without it"
                )

            try:
                # Separate session: its commit lands while lock_db still holds
                # the lock.
                with session_scope() as db:
                    exists = (
                        db.execute(
                            select(ExperimentRecord.id).where(
                                ExperimentRecord.workspace_id == workspace_id,
                                ExperimentRecord.uid == unique_id,
                            )
                        ).first()
                        is not None
                    )

                    if exists:
                        db.execute(
                            update(ExperimentRecord)
                            .where(
                                ExperimentRecord.workspace_id == workspace_id,
                                ExperimentRecord.uid == unique_id,
                            )
                            .values(data_usage=data_usage)
                        )
                    else:
                        db.add(
                            ExperimentRecord(
                                workspace_id=workspace_id,
                                uid=unique_id,
                                data_usage=data_usage,
                            )
                        )
            finally:
                # Release after the inner write committed (lock spans commit).
                if got_lock:
                    try:
                        lock_db.execute(
                            text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name}
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to release advisory lock for experiment "
                            f"data usage [{workspace_id}/{unique_id}]: {e}"
                        )

    @classmethod
    def update_workspace_data_usage(
        cls, db: Session, workspace_id: str, auto_commit: bool = True
    ):
        workspace_dir = join_filepath([DIRPATH.INPUT_DIR, workspace_id])
        if not os.path.exists(workspace_dir):
            logger.warning(f"'{workspace_dir}' does not exist")
            return

        input_data_usage = get_folder_size(workspace_dir)
        db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(input_data_usage=input_data_usage)
        )

        if auto_commit:
            db.commit()

    @classmethod
    def sync_workspace_data_capacity(
        cls, db: Session, workspace_id: str, delete_existing: bool = False
    ):
        """
        Sync workspace data usage and recalculate data capacity
        This is a convenience method that combines update_workspace_data_usage
        and recalculate_workspace_data_capacity

        Args:
            db: Database session
            workspace_id: The workspace ID to sync
            delete_existing: If True, delete all existing records
              before syncing (default: False)
        """
        cls.update_workspace_data_usage(db, workspace_id)
        cls.recalculate_workspace_data_capacity(
            db, workspace_id, delete_existing=delete_existing
        )

    @classmethod
    def recalculate_workspace_data_capacity(
        cls, db: Session, workspace_id: str, delete_existing: bool = False
    ):
        folder = join_filepath([DIRPATH.OUTPUT_DIR, workspace_id])
        if not os.path.exists(folder):
            logger.warning(f"'{folder}' does not exist")
            return
        exp_records = []

        for exp_folder in Path(folder).iterdir():
            try:
                unique_id = exp_folder.name
                data_usage = get_folder_size(exp_folder.as_posix())

                # Update yaml file - skip if experiment.yaml is invalid/corrupted
                try:
                    cls._update_exp_data_usage_yaml(workspace_id, unique_id, data_usage)
                except AssertionError:
                    # A missing or empty experiment.yaml is recoverable - capacity
                    # is still tracked in the DB - so it is logged at debug to
                    # avoid re-warning on every recalculation.
                    logger.debug(
                        f"Skipping YAML update for experiment "
                        f"{workspace_id}/{unique_id}: "
                        f"experiment.yaml is missing or empty"
                    )
                except (ValueError, yaml.YAMLError) as yaml_error:
                    logger.warning(
                        f"Skipping YAML update for experiment "
                        f"{workspace_id}/{unique_id}: "
                        f"malformed experiment.yaml ({yaml_error}). "
                        f"Data usage will still be tracked in the database."
                    )

                # Add experiment record even if YAML update failed
                # This ensures data usage is tracked in the database
                exp_records.append(
                    ExperimentRecord(
                        workspace_id=workspace_id,
                        uid=unique_id,
                        data_usage=data_usage,
                    )
                )
            except Exception as e:
                logger.error(f"Failed to process experiment [{exp_folder}] [{e}]")

        if cls.is_available():
            if delete_existing:
                # Delete all existing records and bulk insert new ones
                db.execute(
                    delete(ExperimentRecord).where(
                        ExperimentRecord.workspace_id == workspace_id
                    )
                )
                db.bulk_save_objects(exp_records)
                logger.debug(
                    f"Deleted and recreated {len(exp_records)} experiment records "
                    f"for workspace [{workspace_id}]"
                )
            else:
                # Upsert: Update existing records or insert new ones
                for exp_record in exp_records:
                    try:
                        existing_record = (
                            db.query(ExperimentRecord)
                            .filter(
                                ExperimentRecord.workspace_id
                                == exp_record.workspace_id,
                                ExperimentRecord.uid == exp_record.uid,
                            )
                            .one()
                        )
                        # Update only data_usage field
                        existing_record.data_usage = exp_record.data_usage
                    except NoResultFound:
                        # Insert new record if it doesn't exist
                        db.add(exp_record)

                logger.debug(
                    f"Updated/inserted {len(exp_records)} experiment records "
                    f"for workspace [{workspace_id}]"
                )

        logger.info(
            "Workspace capacity recalculation succeeded. "
            f"[workspace_id: {workspace_id}]"
        )
