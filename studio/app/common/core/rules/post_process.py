# flake8: noqa
# Exclude from lint for the following reason
# This file is executed by snakemake and cause the following lint errors
# - E402: sys.path.append is required to import optinist modules
# - F821: do not import snakemake
import json
import os
import sys
from os.path import abspath, dirname
from pathlib import Path

ROOT_DIRPATH = dirname(dirname(dirname(dirname(dirname(dirname(abspath(__file__)))))))

sys.path.append(ROOT_DIRPATH)

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.rules.runner import Runner
from studio.app.common.core.snakemake.snakemake_reader import RuleConfigReader
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteStorageType,
)
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.utils.pickle_handler import PickleWriter
from studio.app.dir_path import DIRPATH

logger = AppLogger.get_logger()

if __name__ == "__main__":
    # TODO: debug log.
    logger.debug(
        ">>>>>>>>> Post process logging\n"
        f"[snakemake.input: {snakemake.input}]\n"
        f"[snakemake.output: {snakemake.output}]\n"
    )

    # Get input data for a rule.
    # Note: Check if all node data can be successfully retrieved.
    #       If there is an error in any node, an AssertionError is generated here.
    input_info = Runner.read_input_info(snakemake.input)
    del input_info

    # Operate remote storage.
    if RemoteStorageController.use_remote_storage():
        # Get workspace_id, unique_id
        # TODO: 関数（Utility）化
        rule_config = RuleConfigReader.read(snakemake.params.name)
        workspace_id, unique_id = rule_config.output.split("/")[0:2]

        # 処理結果データの外部ストレージへのデータ転送
        # TODO: データ転送の実施要否の判定追加（環境変数想定）
        remote_storage_controller = RemoteStorageController()
        remote_storage_controller.upload_experiment(workspace_id, unique_id)
    else:
        logger.debug("remote storage is unused in post-process.")

    # 処理結果ファイルを保存
    output_path = str(snakemake.output)
    output_info = {"success": True}  # TODO: より適切な情報の記録に要修正
    PickleWriter.write(output_path, output_info)
