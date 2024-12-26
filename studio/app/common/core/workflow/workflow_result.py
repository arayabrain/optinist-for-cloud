import os
from abc import ABCMeta, abstractmethod
from dataclasses import asdict
from datetime import datetime
from glob import glob
from typing import Dict

from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.experiment.experiment_writer import ExptConfigWriter
from studio.app.common.core.snakemake.smk_status_logger import SmkStatusLogger
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
    RemoteStorageWriter,
)
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.utils.pickle_handler import PickleReader
from studio.app.common.core.workflow.workflow import (
    Message,
    NodeRunStatus,
    OutputPath,
    ProcessType,
)
from studio.app.common.dataclass import BaseData
from studio.app.const import DATE_FORMAT
from studio.app.dir_path import DIRPATH


class WorkflowResult:
    def __init__(self, remote_bucket_name, workspace_id, unique_id):
        self.remote_bucket_name = remote_bucket_name
        self.workspace_id = workspace_id
        self.unique_id = unique_id
        self.workflow_dirpath = join_filepath(
            [
                DIRPATH.OUTPUT_DIR,
                self.workspace_id,
                self.unique_id,
            ]
        )
        self.expt_filepath = join_filepath(
            [self.workflow_dirpath, DIRPATH.EXPERIMENT_YML]
        )

    async def observe(self, nodeIdList) -> Dict:
        """
        Perform the following operations for the specified workflow
          - Check and update the workflow execution status
          - Response with the confirmed workflow execution status
        """

        results: Dict[str, Message] = {}

        # check for workflow errors
        workflow_error = SmkStatusLogger.get_error_content(
            self.workspace_id, self.unique_id
        )

        # observe node list
        for node_id in nodeIdList:
            # Cases with errors in workflow
            if workflow_error["has_error"]:
                node_pickle_path = None
                node_result = NodeResult(
                    self.workspace_id,
                    self.unique_id,
                    node_id,
                    node_pickle_path,
                    workflow_error,
                )
                results[node_id] = node_result.observe()

            # Normal case
            else:
                # search node pickle files
                node_dirpath = join_filepath([self.workflow_dirpath, node_id])
                node_pickle_files = list(
                    set(glob(join_filepath([node_dirpath, "*.pkl"])))
                    - set(glob(join_filepath([node_dirpath, "tmp_*.pkl"])))
                )

                # process node pickle files
                for node_pickle_path in node_pickle_files:
                    # process for procs
                    if node_id == ProcessType.POST_PROCESS.id:
                        node_result = PostProcessResult(
                            self.remote_bucket_name,
                            self.workspace_id,
                            self.unique_id,
                            node_id,
                            node_pickle_path,
                        )
                        results[node_id] = await node_result.observe()

                        self.__check_has_nwb(node_id)

                    # process for nodes
                    else:
                        node_result = NodeResult(
                            self.workspace_id,
                            self.unique_id,
                            node_id,
                            node_pickle_path,
                        )
                        results[node_id] = node_result.observe()

                        self.__check_has_nwb(node_id)

        # check workflow nwb
        self.__check_has_nwb()

        return results

    def __check_has_nwb(self, node_id=None):
        target_whole_nwb = node_id is None

        if target_whole_nwb:
            nwb_filepath_list = glob(join_filepath([self.workflow_dirpath, "*.nwb"]))
        else:
            node_dirpath = join_filepath([self.workflow_dirpath, node_id])
            nwb_filepath_list = glob(join_filepath([node_dirpath, "*.nwb"]))

        for nwb_filepath in nwb_filepath_list:
            if os.path.exists(nwb_filepath):
                config = ExptConfigReader.read(self.expt_filepath)

                if target_whole_nwb:
                    config.hasNWB = True
                else:
                    config.function[node_id].hasNWB = True

                # Update EXPERIMENT_YML
                ExptConfigWriter.write_raw(
                    self.workspace_id, self.unique_id, asdict(config)
                )


class BaseNodeResult(metaclass=ABCMeta):
    @abstractmethod
    def observe(self) -> Message:
        pass

    @abstractmethod
    def success(self) -> Message:
        pass

    @abstractmethod
    def error(self) -> Message:
        pass


class NodeResult(BaseNodeResult):
    def __init__(
        self,
        workspace_id: str,
        unique_id: str,
        node_id: str,
        pickle_filepath: str,
        workflow_error: dict = None,
    ):
        self.workspace_id = workspace_id
        self.unique_id = unique_id
        self.workflow_dirpath = join_filepath(
            [
                DIRPATH.OUTPUT_DIR,
                self.workspace_id,
                self.unique_id,
            ]
        )
        self.node_id = node_id
        self.node_dirpath = join_filepath([self.workflow_dirpath, self.node_id])
        self.expt_filepath = join_filepath(
            [self.workflow_dirpath, DIRPATH.EXPERIMENT_YML]
        )
        self.workflow_has_error = (
            workflow_error["has_error"] if workflow_error else False
        )
        self.workflow_error_log = (
            workflow_error["error_log"] if workflow_error else None
        )

        if not self.workflow_has_error:
            pickle_filepath = pickle_filepath.replace("\\", "/")
            self.algo_name = os.path.splitext(os.path.basename(pickle_filepath))[0]
            try:
                self.info = PickleReader.read(pickle_filepath)
            except EOFError:
                self.info = None  # indicates error
        else:
            self.algo_name = None
            self.info = None

    def observe(self) -> Message:
        expt_config = ExptConfigReader.read(self.expt_filepath)

        # case) error throughout workflow
        if self.workflow_has_error:
            message = self.error(self.workflow_error_log)
        # case) error in node
        elif PickleReader.check_is_error_node_pickle(self.info):
            message = self.error()
        # case) success in node
        else:
            message = self.success()
            expt_config.function[self.node_id].outputPaths = message.outputPaths

        expt_config.function[self.node_id].success = message.status

        now = datetime.now().strftime(DATE_FORMAT)
        expt_config.function[self.node_id].finished_at = now
        expt_config.function[self.node_id].message = message.message

        # check all function processed status
        statuses = list(map(lambda x: x.success, expt_config.function.values()))

        if NodeRunStatus.RUNNING.value not in statuses:
            expt_config.finished_at = now
            if NodeRunStatus.ERROR.value in statuses:
                expt_config.success = NodeRunStatus.ERROR.value
            else:
                expt_config.success = NodeRunStatus.SUCCESS.value

        # Update EXPERIMENT_YML
        ExptConfigWriter.write_raw(
            self.workspace_id, self.unique_id, asdict(expt_config)
        )
        return message

    def success(self) -> Message:
        return Message(
            status=NodeRunStatus.SUCCESS.value,
            message=f"{self.algo_name} success",
            outputPaths=self.output_paths(),
        )

    def error(self, message: str = None) -> Message:
        if message is None:
            if self.info is None:
                message = "Invalid node result info: None"
            else:
                message = (
                    "\n".join(self.info) if isinstance(self.info, list) else self.info
                )

        return Message(status=NodeRunStatus.ERROR.value, message=message)

    def output_paths(self) -> dict:
        output_paths: Dict[str, OutputPath] = {}
        for k, v in self.info.items():
            if isinstance(v, BaseData):
                v.save_json(self.node_dirpath)
                if v.output_path:
                    output_paths[k] = v.output_path

        return output_paths


class PostProcessResult(BaseNodeResult):
    def __init__(
        self,
        remote_bucket_name: str,
        workspace_id: str,
        unique_id: str,
        node_id: str,
        pickle_filepath: str,
        workflow_error: dict = None,
    ):
        self.remote_bucket_name = remote_bucket_name
        self.workspace_id = workspace_id
        self.unique_id = unique_id
        self.workflow_dirpath = join_filepath(
            [
                DIRPATH.OUTPUT_DIR,
                self.workspace_id,
                self.unique_id,
            ]
        )
        self.node_id = node_id
        self.node_dirpath = join_filepath([self.workflow_dirpath, self.node_id])
        self.expt_filepath = join_filepath(
            [self.workflow_dirpath, DIRPATH.EXPERIMENT_YML]
        )
        self.workflow_has_error = (
            workflow_error["has_error"] if workflow_error else False
        )
        self.workflow_error_log = (
            workflow_error["error_log"] if workflow_error else None
        )

        if not self.workflow_has_error:
            pickle_filepath = pickle_filepath.replace("\\", "/")
            self.algo_name = os.path.splitext(os.path.basename(pickle_filepath))[0]
            try:
                self.info = PickleReader.read(pickle_filepath)
            except EOFError:
                self.info = None  # indicates error
        else:
            self.algo_name = None
            self.info = None

    async def observe(self) -> Message:
        expt_config = ExptConfigReader.read(self.expt_filepath)

        # case) error throughout workflow
        if self.workflow_has_error:
            message = self.error(self.workflow_error_log)
        # case) error in node
        elif PickleReader.check_is_error_node_pickle(self.info):
            message = self.error()
        # case) success in node
        else:
            message = self.success()

        expt_config.procs[self.node_id].success = message.status

        now = datetime.now().strftime(DATE_FORMAT)
        expt_config.procs[self.node_id].finished_at = now
        expt_config.procs[self.node_id].message = message.message

        # check all function processed status
        statuses = list(map(lambda x: x.success, expt_config.procs.values()))

        if NodeRunStatus.RUNNING.value not in statuses:
            expt_config.finished_at = now
            if NodeRunStatus.ERROR.value in statuses:
                expt_config.success = NodeRunStatus.ERROR.value
            else:
                expt_config.success = NodeRunStatus.SUCCESS.value

        # Update EXPERIMENT_YML
        ExptConfigWriter.write_raw(
            self.workspace_id, self.unique_id, asdict(expt_config)
        )

        # Operate remote storage data.
        if RemoteStorageController.is_available():
            # upload latest EXPERIMENT_YML
            async with RemoteStorageWriter(
                self.remote_bucket_name, expt_config.workspace_id, expt_config.unique_id
            ) as remote_storage_controller:
                await remote_storage_controller.upload_experiment(
                    expt_config.workspace_id,
                    expt_config.unique_id,
                    [DIRPATH.EXPERIMENT_YML],
                )

        return message

    def success(self) -> Message:
        return Message(
            status=NodeRunStatus.SUCCESS.value,
            message=f"{self.algo_name} success",
            outputPaths=[],
        )

    def error(self, message: str = None) -> Message:
        if message is None:
            if self.info is None:
                message = "Invalid node result info: None"
            else:
                message = (
                    "\n".join(self.info) if isinstance(self.info, list) else self.info
                )

        return Message(status=NodeRunStatus.ERROR.value, message=message)
