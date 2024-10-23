import os

import yaml

from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.utils.filepath_finder import find_condaenv_filepath
from studio.app.common.core.workflow.workflow import NodeType, NodeTypeUtil, ProcessType
from studio.app.const import FILETYPE
from studio.app.dir_path import DIRPATH
from studio.app.wrappers import wrapper_dict


class SmkUtils:
    @classmethod
    def input(cls, details):
        if NodeTypeUtil.check_nodetype_from_filetype(details["type"]) == NodeType.DATA:
            if details["type"] in [FILETYPE.IMAGE]:
                return [join_filepath([DIRPATH.INPUT_DIR, x]) for x in details["input"]]
            else:
                return join_filepath([DIRPATH.INPUT_DIR, details["input"]])
        else:
            return [join_filepath([DIRPATH.OUTPUT_DIR, x]) for x in details["input"]]

    @classmethod
    def output(cls, details):
        return join_filepath([DIRPATH.OUTPUT_DIR, details["output"]])

    @classmethod
    def conda(cls, details):
        # skip conda for input node
        if NodeTypeUtil.check_nodetype_from_filetype(details["type"]) == NodeType.DATA:
            return None
        # skip conda for process-event
        elif details["type"] in [
            ProcessType.POST_PROCESS.type,
        ]:
            return None

        wrapper = cls.dict2leaf(wrapper_dict, details["path"].split("/"))

        if "conda_name" in wrapper:
            conda_name = wrapper["conda_name"]
            conda_filepath = f"{DIRPATH.CONDAENV_DIR}/envs/{conda_name}"
            if os.path.exists(conda_filepath):
                return conda_filepath
            else:
                return find_condaenv_filepath(conda_name)

        return None

    @classmethod
    def get_datatypes_inputs(
        cls, workspace_id: str, unique_id: str, apply_basename: bool = False
    ) -> list:
        smk_config_yml_path = join_filepath(
            [DIRPATH.OUTPUT_DIR, workspace_id, unique_id, DIRPATH.SNAKEMAKE_CONFIG_YML]
        )
        with open(smk_config_yml_path, "r") as f:
            smk_config = yaml.safe_load(f)

        input_paths = []

        for node in smk_config["rules"].values():
            if NodeTypeUtil.check_nodetype_from_filetype(node["type"]) == NodeType.DATA:
                if node["type"] in [FILETYPE.IMAGE]:
                    if apply_basename:
                        tmp_input_paths = [os.path.basename(x) for x in node["input"]]
                    else:
                        tmp_input_paths = node["input"]
                    input_paths.extend(tmp_input_paths)
                else:
                    if apply_basename:
                        tmp_input_path = os.path.basename(node["input"])
                    else:
                        tmp_input_path = node["input"]
                    input_paths.append(tmp_input_path)

        return input_paths

    @classmethod
    def dict2leaf(cls, root_dict: dict, path_list):
        path = path_list.pop(0)
        if len(path_list) > 0:
            return cls.dict2leaf(root_dict[path], path_list)
        else:
            return root_dict[path]
