import argparse
import asyncio
import os
import sys
from pathlib import Path

import boto3
import yaml

from studio.app.common.core.storage.s3_storage_controller import S3StorageController

# Add the project root directory to the Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Import after path modification to avoid E402 linting errors
try:
    from sqlmodel import select

    from studio.app.common.core.logger import AppLogger
    from studio.app.common.core.utils.file_reader import get_folder_size
    from studio.app.common.core.utils.filepath_creater import join_filepath
    from studio.app.common.core.workspace.workspace_data_capacity_services import (
        WorkspaceDataCapacityService,
    )
    from studio.app.common.db.database import session_scope
    from studio.app.common.models.experiment import ExperimentRecord
    from studio.app.common.models.workspace import Workspace
    from studio.app.dir_path import DIRPATH
except ImportError as e:
    print(f"Import error: {e}")
    print(
        "Make sure you're running from the correct "
        "directory with dependencies installed"
    )
    sys.exit(1)

logger = AppLogger.get_logger()


class CloudWorkspaceDataCapacityService:
    """
    Enhanced workspace data capacity service that includes S3 storage usage
    """

    @classmethod
    async def get_s3_workspace_size(cls, bucket_name: str, workspace_id: str) -> int:
        """
        Calculate total S3 storage size for a workspace.

        Args:
            bucket_name: S3 bucket name
            workspace_id: Workspace ID to check storage for

        Returns:
            Total storage size in bytes for both input and output data
        """
        total_size = 0

        try:
            s3_client = boto3.client("s3")

            # Check both input and output directories for the workspace
            prefixes = [
                f"{S3StorageController.S3_BASE_PATH}/input/{workspace_id}/",
                f"{S3StorageController.S3_BASE_PATH}/output/{workspace_id}/",
            ]

            logger.info(f"Calculating S3 usage for workspace {workspace_id}")

            for prefix in prefixes:
                try:
                    # logger.debug(f"Scanning S3 prefix: {prefix}")
                    paginator = s3_client.get_paginator("list_objects_v2")
                    page_iterator = paginator.paginate(
                        Bucket=bucket_name, Prefix=prefix
                    )

                    prefix_size = 0
                    object_count = 0
                    for page in page_iterator:
                        if "Contents" in page:
                            for obj in page["Contents"]:
                                object_size = obj["Size"]
                                total_size += object_size
                                prefix_size += object_size
                                object_count += 1

                    # logger.debug(
                    #     f"Prefix {prefix}: {object_count} objects, "
                    #     f"{prefix_size:,} bytes"
                    # )

                except Exception as e:
                    logger.warning(f"Failed to get size for prefix {prefix}: {e}")
                    continue

        except Exception as e:
            logger.error(
                f"Failed to calculate S3 storage size for "
                f"workspace {workspace_id}: {e}"
            )
            return 0

        logger.info(
            f"S3 storage size for workspace {workspace_id}: {total_size:,} bytes"
        )
        return total_size

    @classmethod
    async def get_s3_experiment_size(
        cls, bucket_name: str, workspace_id: str, unique_id: str
    ) -> int:
        """
        Calculate S3 storage size for a specific experiment.

        Args:
            bucket_name: S3 bucket name
            workspace_id: Workspace ID
            unique_id: Experiment unique ID

        Returns:
            Storage size in bytes for the experiment
        """
        total_size = 0

        try:
            s3_client = boto3.client("s3")
            prefix = (
                f"{S3StorageController.S3_BASE_PATH}/output/{workspace_id}/{unique_id}/"
            )

            # logger.debug(
            #     f"Calculating S3 usage for experiment {workspace_id}/{unique_id}"
            # )

            paginator = s3_client.get_paginator("list_objects_v2")
            page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

            object_count = 0
            for page in page_iterator:
                if "Contents" in page:
                    for obj in page["Contents"]:
                        total_size += obj["Size"]
                        object_count += 1

            # logger.debug(
            #     f"S3 experiment {workspace_id}/{unique_id}: {object_count} objects, "
            #     f"{total_size:,} bytes"
            # )

        except Exception as e:
            logger.error(
                f"Failed to calculate S3 storage size for experiment "
                f"{workspace_id}/{unique_id}: {e}"
            )
            return 0

        return total_size

    @classmethod
    async def update_experiment_data_usage_with_s3(
        cls, bucket_name: str, workspace_id: str, unique_id: str
    ):
        """
        Update experiment data usage including both local and S3 storage.
        """
        # Get local storage size
        workflow_dir = join_filepath([DIRPATH.OUTPUT_DIR, workspace_id, unique_id])
        local_size = 0
        if os.path.exists(workflow_dir):
            local_size = get_folder_size(workflow_dir)

        # Get S3 storage size
        s3_size = await cls.get_s3_experiment_size(bucket_name, workspace_id, unique_id)

        # Total data usage is the max of local and S3 (they are mirrored)
        # Using max instead of sum to avoid double-counting synced files
        total_data_usage = max(local_size, s3_size)

        logger.info(
            f"Experiment {workspace_id}/{unique_id} usage: "
            f"local={local_size:,} bytes, S3={s3_size:,} bytes, "
            f"total={total_data_usage:,} bytes (using max to avoid double-counting)"
        )

        # Update the yaml file and database
        WorkspaceDataCapacityService._update_exp_data_usage_yaml(
            workspace_id, unique_id, total_data_usage
        )

        if WorkspaceDataCapacityService.is_available():
            WorkspaceDataCapacityService._update_exp_data_usage_db(
                workspace_id, unique_id, total_data_usage
            )

    @classmethod
    async def update_workspace_data_usage_with_s3(
        cls, db, bucket_name: str, workspace_id: str, auto_commit: bool = True
    ):
        """
        Update workspace input data usage including both local and S3 storage.
        """
        # Get local input storage size
        workspace_dir = join_filepath([DIRPATH.INPUT_DIR, workspace_id])
        local_input_size = 0
        if os.path.exists(workspace_dir):
            local_input_size = get_folder_size(workspace_dir)

        # Get S3 input storage size
        try:
            s3_client = boto3.client("s3")
            prefix = f"{S3StorageController.S3_BASE_PATH}input/{workspace_id}/"

            paginator = s3_client.get_paginator("list_objects_v2")
            page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

            s3_input_size = 0
            for page in page_iterator:
                if "Contents" in page:
                    for obj in page["Contents"]:
                        s3_input_size += obj["Size"]

        except Exception as e:
            logger.error(
                f"Failed to get S3 input size for workspace {workspace_id}: {e}"
            )
            s3_input_size = 0

        # Total input data usage is the max of local and S3 (they are mirrored)
        # Using max instead of sum to avoid double-counting synced files
        total_input_usage = max(local_input_size, s3_input_size)

        logger.info(
            f"Workspace {workspace_id} input usage: "
            f"local={local_input_size:,} bytes, S3={s3_input_size:,} bytes, "
            f"total={total_input_usage:,} bytes (using max to avoid double-counting)"
        )

        # Update database
        from sqlmodel import update

        db.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(input_data_usage=total_input_usage)
        )

        if auto_commit:
            db.commit()

    @classmethod
    async def recalculate_workspace_data_capacity_with_s3(
        cls, db, bucket_name: str, workspace_id: str, delete_existing: bool = False
    ):
        """
        Recalculate workspace data capacity including S3 storage for all experiments.
        """
        folder = join_filepath([DIRPATH.OUTPUT_DIR, workspace_id])
        exp_records = []

        # Get all experiment folders from local filesystem
        local_experiments = set()
        if os.path.exists(folder):
            for exp_folder in Path(folder).iterdir():
                if exp_folder.is_dir():
                    local_experiments.add(exp_folder.name)

        # Get all experiment folders from S3
        s3_experiments = set()
        try:
            s3_client = boto3.client("s3")
            prefix = f"{S3StorageController.S3_BASE_PATH}/output/{workspace_id}/"

            response = s3_client.list_objects_v2(
                Bucket=bucket_name, Prefix=prefix, Delimiter="/"
            )

            if "CommonPrefixes" in response:
                for exp_prefix in response["CommonPrefixes"]:
                    # Extract experiment ID from prefix like
                    # "{S3_BASE_PATH}/output/1/exp123/"
                    exp_id = exp_prefix["Prefix"].rstrip("/").split("/")[-1]
                    s3_experiments.add(exp_id)

        except Exception as e:
            logger.error(
                f"Failed to list S3 experiments for " f"workspace {workspace_id}: {e}"
            )

        # Process all experiments (union of local and S3)
        all_experiments = local_experiments.union(s3_experiments)
        logger.info(
            f"Found {len(all_experiments)} experiments for workspace {workspace_id}"
        )
        # logger.debug(f"Local: {local_experiments}, S3: {s3_experiments}")

        for unique_id in all_experiments:
            try:
                # Get local size
                local_exp_dir = join_filepath(
                    [DIRPATH.OUTPUT_DIR, workspace_id, unique_id]
                )
                local_size = 0
                if os.path.exists(local_exp_dir):
                    local_size = get_folder_size(local_exp_dir)

                # Get S3 size
                s3_size = await cls.get_s3_experiment_size(
                    bucket_name, workspace_id, unique_id
                )

                # Total data usage is the max of local and S3 (they are mirrored)
                # Using max instead of sum to avoid double-counting synced files
                total_data_usage = max(local_size, s3_size)

                # logger.debug(
                #     f"Experiment {workspace_id}/{unique_id}: "
                #     f"local={local_size:,}, S3={s3_size:,}, "
                #     f"total={total_data_usage:,}"
                # )

                # Update yaml file - skip if experiment.yaml is invalid/corrupted
                try:
                    WorkspaceDataCapacityService._update_exp_data_usage_yaml(
                        workspace_id, unique_id, total_data_usage
                    )
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
                        data_usage=total_data_usage,
                    )
                )

            except Exception as e:
                logger.error(
                    f"Failed to process experiment {workspace_id}/{unique_id}: {e}"
                )

        # Update database records
        if WorkspaceDataCapacityService.is_available():
            if delete_existing:
                # Delete all existing records and bulk insert new ones
                from sqlmodel import delete

                db.execute(
                    delete(ExperimentRecord).where(
                        ExperimentRecord.workspace_id == workspace_id
                    )
                )
                db.bulk_save_objects(exp_records)
                logger.info(
                    f"Deleted and recreated {len(exp_records)} experiment records "
                    f"for workspace [{workspace_id}]"
                )
            else:
                # Upsert: Update existing records or insert new ones
                from sqlalchemy.exc import NoResultFound

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

                logger.info(
                    f"Updated/inserted {len(exp_records)} experiment records "
                    f"for workspace [{workspace_id}]"
                )

        logger.info(
            f"Cloud workspace capacity recalculation completed "
            f"for workspace {workspace_id}"
        )

    @classmethod
    async def sync_workspace_data_capacity_with_s3(
        cls, db, bucket_name: str, workspace_id: str, delete_existing: bool = False
    ):
        """
        Sync workspace data usage including S3 storage and recalculate data capacity.
        """
        await cls.update_workspace_data_usage_with_s3(db, bucket_name, workspace_id)
        await cls.recalculate_workspace_data_capacity_with_s3(
            db, bucket_name, workspace_id, delete_existing=delete_existing
        )


async def main(args):
    """
    Main function to sync workspace data capacity including S3 storage.
    """
    # Get S3 bucket name from environment
    bucket_name = os.environ.get("S3_DEFAULT_BUCKET_NAME")
    if not bucket_name:
        logger.error("S3_DEFAULT_BUCKET_NAME environment variable not set")
        sys.exit(1)

    logger.info(f"Using S3 bucket: {bucket_name}")

    if WorkspaceDataCapacityService.is_available():
        logger.info("Multi-user mode detected")
        if args.delete_existing:
            logger.info(
                "Running with --delete-existing flag"
                " - will delete and recreate all records"
            )
        else:
            logger.info(
                "Running without --delete-existing flag - will update existing records"
            )

        with session_scope() as db:
            if args.workspace_id:
                # Process specific workspace
                workspace_ids = [args.workspace_id]
            else:
                # Process all non-deleted workspaces
                workspace_list = db.execute(
                    select(Workspace.id).filter(Workspace.deleted.is_(False))
                ).scalars()
                workspace_ids = list(workspace_list)

            logger.info(f"Processing {len(workspace_ids)} workspace(s)")

            for workspace_id in workspace_ids:
                logger.info(
                    f"Syncing workspace data capacity with S3 "
                    f"for workspace: [{workspace_id}]"
                )
                service = CloudWorkspaceDataCapacityService
                await service.sync_workspace_data_capacity_with_s3(
                    db,
                    bucket_name,
                    str(workspace_id),
                    delete_existing=args.delete_existing,
                )
    else:
        logger.info("Single-user mode detected")
        # Single-user mode always uses delete_existing=True (default)
        service = CloudWorkspaceDataCapacityService
        await service.recalculate_workspace_data_capacity_with_s3(
            db=None,
            bucket_name=bucket_name,
            workspace_id="1",
            delete_existing=args.delete_existing,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sync workspace data capacity including S3 storage "
        "for all workspaces"
    )
    parser.add_argument(
        "--delete-existing",
        action="store_true",
        help="Delete all existing records before syncing. "
        "Without this flag, existing records will be updated (upsert).",
    )
    parser.add_argument(
        "--workspace-id",
        type=str,
        help="Sync only a specific workspace ID. "
        "If not provided, syncs all workspaces.",
    )

    args = parser.parse_args()
    asyncio.run(main(args))
