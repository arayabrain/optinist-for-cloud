# flake8: noqa
# Exclude from lint for the following reason
# This file is executed by snakemake and cause the following lint errors
# - E402: sys.path.append is required to import optinist modules
# - F821: do not import snakemake
import json
import os
import sys
import traceback
from os.path import abspath, dirname
from pathlib import Path

ROOT_DIRPATH = dirname(dirname(dirname(dirname(dirname(dirname(abspath(__file__)))))))

sys.path.append(ROOT_DIRPATH)

from studio.app.common.core.experiment.experiment import ExptOutputPathIds
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.rules.runner import Runner
from studio.app.common.core.snakemake.smk import Rule
from studio.app.common.core.snakemake.snakemake_reader import RuleConfigReader
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteStorageType,
)
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.utils.pickle_handler import PickleWriter
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()


class PostProcessRunner:
    @classmethod
    def run(cls, __rule: Rule):
        try:
            logger.info("start post_process runner")

            # Get input data for a rule.
            # Note:
            #   - Check if all node data can be successfully retrieved.
            #     If there is an error in any node, an AssertionError is generated here.
            #   - read_input_info() is used to determine if there is an error,
            #     and the return value is not used here.
            Runner.read_input_info(__rule.input)

            # Operate remote storage.
            if RemoteStorageController.use_remote_storage():
                # Get workspace_id, unique_id from output file path
                ids = ExptOutputPathIds(dirname(__rule.output))
                workspace_id = ids.workspace_id
                unique_id = ids.unique_id

                # Transfer of processing result data to remote storage
                remote_storage_controller = RemoteStorageController()
                remote_storage_controller.upload_experiment(workspace_id, unique_id)
            else:
                logger.debug("remote storage is unused in post_process.")

            # Save processing result pkl file.
            output_path = __rule.output
            output_info = {"success": True}  # Note: Add parameters as needed.
            PickleWriter.write(output_path, output_info)

        except Exception as e:
            """
            Note: The code here is the same as in the except section of Runner.run
            """
            err_msg = list(traceback.TracebackException.from_exception(e).format())

            # show full trace to console
            logger.error("\n".join(err_msg))

            # save msg for GUI
            PickleWriter.write(__rule.output, err_msg)


if __name__ == "__main__":
    logger.debug(
        "post process startup debug logging\n"
        f"[snakemake.input: {snakemake.input}]\n"
        f"[snakemake.output: {snakemake.output}]\n"
    )

    rule_config = RuleConfigReader.read(snakemake.params.name)

    rule_config.input = snakemake.input
    rule_config.output = snakemake.output[0]

    PostProcessRunner.run(rule_config)
