import os
import signal
from abc import ABCMeta, abstractmethod
from dataclasses import asdict
from datetime import datetime
from glob import glob
from typing import Dict

from fastapi import HTTPException

from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageController,
)
from studio.app.common.core.utils.config_handler import ConfigWriter
from studio.app.common.core.utils.file_reader import JsonReader, Reader
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.utils.pickle_handler import PickleReader
from studio.app.common.core.workflow.workflow import Message, OutputPath, ProcessType
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
        self.error_filepath = join_filepath([self.workflow_dirpath, "error.log"])
        self.pid_filepath = join_filepath([self.workflow_dirpath, "pid.json"])

    async def get(self, nodeIdList):
        results: Dict[str, Message] = {}

        for node_id in nodeIdList:
            # check if any errors occur (error.log)
            if os.path.exists(self.error_filepath):
                error_message = Reader.read(self.error_filepath)
                if error_message != "":
                    results[node_id] = Message(
                        status="error",
                        message=error_message,
                    )

            # search workflow result pkl files
            glob_pickle_filepath = glob(
                join_filepath([self.workflow_dirpath, node_id, "*.pkl"])
            )
            tmp_glob_pickle_filepath = glob(
                join_filepath([self.workflow_dirpath, node_id, "tmp_*.pkl"])
            )

            # process workflow result pkl files
            for pickle_filepath in list(
                set(glob_pickle_filepath) - set(tmp_glob_pickle_filepath)
            ):
                # process for procs
                if node_id == ProcessType.POST_PROCESS.id:
                    node_result = PostProcessResult(
                        self.remote_bucket_name,
                        self.workflow_dirpath,
                        node_id,
                        pickle_filepath,
                    )

                    if node_result.info is not None:
                        results[node_id] = await node_result.get()
                        self.update_has_nwb(node_id)

                # process for nodes
                else:
                    node_result = NodeResult(
                        self.workflow_dirpath,
                        node_id,
                        pickle_filepath,
                    )

                    if node_result.info is not None:
                        results[node_id] = node_result.get()
                        self.update_has_nwb(node_id)

        self.update_has_nwb()

        return results

    def update_has_nwb(self, node_id=None):
        if node_id is None:
            nwb_filepath_list = glob(join_filepath([self.workflow_dirpath, "*.nwb"]))
        else:
            nwb_filepath_list = glob(
                join_filepath([self.workflow_dirpath, node_id, "*.nwb"])
            )

        for nwb_filepath in nwb_filepath_list:
            if os.path.exists(nwb_filepath):
                config = ExptConfigReader.read(self.expt_filepath)

                if node_id is None:
                    config.hasNWB = True
                else:
                    config.function[node_id].hasNWB = True

                ConfigWriter.write(
                    dirname=self.workflow_dirpath,
                    filename=DIRPATH.EXPERIMENT_YML,
                    config=asdict(config),
                )

    def cancel(self):
        """
        The algorithm function of this workflow is being executed at the line:
        https://github.com/snakemake/snakemake/blob/27b224ed12448df8aebc7d1ff8f25e3bf7622232/snakemake/shell.py#L258
        ```
        proc = sp.Popen(
            cmd,
            bufsize=-1,
            shell=use_shell,
            stdout=stdout,
            universal_newlines=iterable or read or None,
            close_fds=close_fds,
            **cls._process_args,
            env=envvars,
        )
        ```
        The `cmd` argument has the following format:
        ```
        source ~/miniconda3/bin/activate
        '~/Documents/optinistfs/.snakemake/conda/491889952d2f07f3876bb801eea626e9_';
        set -euo pipefail;
        python ~/Documents/optinistfs/.snakemake/scripts/tmp03froqxo.func.py
        ```
        Interrupt the conda activate at the beginning of the process is impossible
        because it is only called when each algorithm function executes.
        This workflow is cancelled by killing process via PID of algorithm function
        saved in pid.json file
        Raises:
            HTTPException: if pid_filepath or last_script_file does not exist
        """
        if not os.path.exists(self.pid_filepath):
            raise HTTPException(status_code=404)

        pid_data = JsonReader.read(self.pid_filepath)

        if not os.path.exists(pid_data["last_script_file"]):
            raise HTTPException(status_code=404)

        os.remove(pid_data["last_script_file"])
        os.kill(pid_data["last_pid"], signal.SIGTERM)

        return True


class BaseNodeResult(metaclass=ABCMeta):
    @abstractmethod
    def get(self):
        pass

    @abstractmethod
    def success(self):
        pass

    @abstractmethod
    def error(self):
        pass


class NodeResult(BaseNodeResult):
    def __init__(self, workflow_dirpath, node_id, pickle_filepath):
        self.workflow_dirpath = workflow_dirpath
        self.node_id = node_id
        self.node_dirpath = join_filepath([self.workflow_dirpath, self.node_id])
        self.expt_filepath = join_filepath(
            [self.workflow_dirpath, DIRPATH.EXPERIMENT_YML]
        )

        pickle_filepath = pickle_filepath.replace("\\", "/")
        self.algo_name = os.path.splitext(os.path.basename(pickle_filepath))[0]
        try:
            self.info = PickleReader.read(pickle_filepath)
        except EOFError:
            self.info = None

    def get(self):
        expt_config = ExptConfigReader.read(self.expt_filepath)
        if isinstance(self.info, (list, str)):
            expt_config.function[self.node_id].success = "error"
            message = self.error()
        else:
            expt_config.function[self.node_id].success = "success"
            message = self.success()
            expt_config.function[self.node_id].outputPaths = message.outputPaths
        now = datetime.now().strftime(DATE_FORMAT)
        expt_config.function[self.node_id].finished_at = now
        expt_config.function[self.node_id].message = message.message

        # check all function processed status
        statuses = list(map(lambda x: x.success, expt_config.function.values()))
        if "running" not in statuses:
            expt_config.finished_at = now
            if "error" in statuses:
                expt_config.success = "error"
            else:
                expt_config.success = "success"

        # update EXPERIMENT_YML.
        ConfigWriter.write(
            dirname=self.workflow_dirpath,
            filename=DIRPATH.EXPERIMENT_YML,
            config=asdict(expt_config),
        )

        return message

    def success(self):
        return Message(
            status="success",
            message=f"{self.algo_name} success",
            outputPaths=self.outputPaths(),
        )

    def error(self):
        return Message(
            status="error",
            message=self.info if isinstance(self.info, str) else "\n".join(self.info),
        )

    def outputPaths(self):
        outputPaths: Dict[str, OutputPath] = {}
        for k, v in self.info.items():
            if isinstance(v, BaseData):
                v.save_json(self.node_dirpath)
                if v.output_path:
                    outputPaths[k] = v.output_path

        return outputPaths


class PostProcessResult(BaseNodeResult):
    def __init__(self, remote_bucket_name, workflow_dirpath, node_id, pickle_filepath):
        self.remote_bucket_name = remote_bucket_name
        self.workflow_dirpath = workflow_dirpath
        self.node_id = node_id
        self.node_dirpath = join_filepath([self.workflow_dirpath, self.node_id])
        self.expt_filepath = join_filepath(
            [self.workflow_dirpath, DIRPATH.EXPERIMENT_YML]
        )

        pickle_filepath = pickle_filepath.replace("\\", "/")
        self.algo_name = os.path.splitext(os.path.basename(pickle_filepath))[0]
        try:
            self.info = PickleReader.read(pickle_filepath)
        except EOFError:
            self.info = None

    async def get(self):
        expt_config = ExptConfigReader.read(self.expt_filepath)
        if isinstance(self.info, (list, str)):
            expt_config.procs[self.node_id].success = "error"
            message = self.error()
        else:
            expt_config.procs[self.node_id].success = "success"
            message = self.success()
        now = datetime.now().strftime(DATE_FORMAT)
        expt_config.procs[self.node_id].finished_at = now
        expt_config.procs[self.node_id].message = message.message

        # check all procs processed status
        statuses = list(map(lambda x: x.success, expt_config.procs.values()))
        if "running" not in statuses:
            expt_config.finished_at = now
            if "error" in statuses:
                expt_config.success = "error"
            else:
                expt_config.success = "success"

        # update EXPERIMENT_YML.
        ConfigWriter.write(
            dirname=self.workflow_dirpath,
            filename=DIRPATH.EXPERIMENT_YML,
            config=asdict(expt_config),
        )

        # Operate remote storage data.
        if RemoteStorageController.is_available():
            # upload latest EXPERIMENT_YML
            remote_storage_controller = RemoteStorageController(self.remote_bucket_name)
            await remote_storage_controller.upload_experiment(
                expt_config.workspace_id,
                expt_config.unique_id,
                [DIRPATH.EXPERIMENT_YML],
            )

        return message

    def success(self):
        return Message(
            status="success",
            message=f"{self.algo_name} success",
            outputPaths=[],
        )

    def error(self):
        return Message(
            status="error",
            message=self.info if isinstance(self.info, str) else "\n".join(self.info),
        )
