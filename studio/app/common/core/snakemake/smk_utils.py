import hashlib
import os
import platform
import subprocess
from typing import Dict

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.utils.config_handler import ConfigReader
from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.common.core.utils.filepath_finder import find_condaenv_filepath
from studio.app.common.core.workflow.workflow import NodeType, NodeTypeUtil, ProcessType
from studio.app.const import FILETYPE
from studio.app.dir_path import DIRPATH
from studio.app.wrappers import wrapper_dict

logger = AppLogger.get_logger()


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
    def dict2leaf(cls, root_dict: dict, path_list):
        """Recursively unpacks nested dictionary using path list to get leaf value"""
        path = path_list.pop(0)
        if len(path_list) > 0:
            return cls.dict2leaf(root_dict[path], path_list)
        else:
            return root_dict[path]

    @classmethod
    def get_conda_env_filepath(cls, conda_name) -> str:
        conda_env_filepath = f"{DIRPATH.CONDAENV_DIR}/envs/{conda_name}"
        if os.path.exists(conda_env_filepath):
            return conda_env_filepath
        else:
            return find_condaenv_filepath(conda_name)

    @classmethod
    def conda(cls, details):
        """Gets conda env path and handles special case of CaImAn on Apple Silicon"""

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

            # Handle CaImAn params modification if needed
            is_caiman = "caiman" in conda_name.lower()
            if is_caiman and cls.is_apple_silicon():
                # Modify the parameters directly in the details dictionary
                if "params" in details:
                    details["params"] = cls.modify_caiman_params_for_apple_silicon(
                        details["params"]
                    )

            return cls.get_conda_env_filepath(conda_name)

        return None

    @classmethod
    def get_datatypes_inputs(
        cls, workspace_id: str, unique_id: str, apply_basename: bool = False
    ) -> list:
        smk_config_yml_path = join_filepath(
            [DIRPATH.OUTPUT_DIR, workspace_id, unique_id, DIRPATH.SNAKEMAKE_CONFIG_YML]
        )
        smk_config = ConfigReader.read(smk_config_yml_path)

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

    @staticmethod
    def is_apple_silicon():
        """
        Detects if running on Apple Silicon CPU, including under Rosetta 2 emulation
        """
        try:
            # Check the architecture reported by Python
            python_arch = platform.machine()

            # Check the underlying hardware architecture using sysctl
            cmd = ["sysctl", "-n", "hw.machine"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            hardware_arch = result.stdout.strip()

            # If Python reports x86_64 but hardware is arm64, we're running Rosetta 2
            # CaImAn cnn is currently failing with Rosetta 2
            if python_arch == "x86_64" and hardware_arch == "arm64":
                return "arm64"  # Underlying hardware is Apple Silicon
            else:
                return hardware_arch

        except Exception as e:
            logger.info("Failed to detect Apple Silicon: %s", e)
            return False

    @staticmethod
    def modify_caiman_params_for_apple_silicon(params: Dict) -> Dict:
        """
        Modifies CaImAn params to be compatible with Apple Silicon by disabling CNN
        """
        if params is None:
            return params

        # Create a deep copy to avoid modifying the original
        modified_params = params.copy()

        # Check if advanced parameters exist and contain quality evaluation params
        if "advanced" in modified_params:
            if "quality_evaluation_params" in modified_params["advanced"]:
                modified_params["advanced"]["quality_evaluation_params"][
                    "use_cnn"
                ] = False
                logger.info("Disabled CNN usage in CaImAn parameters for Apple Silicon")

        # Also check top-level parameters
        if "quality_evaluation_params" in modified_params:
            modified_params["quality_evaluation_params"]["use_cnn"] = False
            logger.info("Disabled CNN usage in CaImAn parameters for Apple Silicon")

        return modified_params


# Cache conda env path (performance consideration)
_global_smk_conda_env_paths_cache: Dict[str, str] = {}


class SmkInternalUtils:
    """
    This class defines functions that directly use Snakemake's internal API.
    - Notes.
      - If there are API specification changes due to future version upgrades
        of Snakemake, it is necessary to follow the changes.
      - Initially, we have confirmed the operation with the following versions
        of snakemake.
        - snakemake v7.30
    """

    @classmethod
    def verify_conda_env_exists(
        cls,
        conda_name: str,
        conda_env_rootpath: str = None,
        conda_env_filepath: str = None,
    ) -> bool:
        """
        Verify that the specified conda environment has been generated by snakemake.
        """

        """
        Get the root path to the conda env destination managed by snakemake.
        NOTE:
          This path is generated in `Persistence.__init__`, but since the above function
          also performs initialization processes other than path generation,
          Persistence is not used directly here.
        """
        if conda_env_rootpath is None:
            conda_env_rootpath = DIRPATH.SNAKEMAKE_CONDA_ENV_DIR

        # Get the path of the conda env configuration file
        if conda_env_filepath is None:
            conda_env_filepath = SmkUtils.get_conda_env_filepath(conda_name) or ""
        if not os.path.exists(conda_env_filepath):
            assert False, (
                "Invalid conda_env_filepath. "
                f"[conda_name: {conda_name}] [env_filepath: {conda_env_filepath}]"
            )

        """
        Get the path of the target conda env generated by snakemake.
        NOTE:
          - This determination is defined as follows:
            - snakemake.deployment.conda.CondaEnvFileSpec.get_conda_env
          - It is possible to get the conda env path using the following
            snakemake module (Env.address), but it is recommended to avoid
            using it directly as it may affect the snakemake process.
            - snakemake.Workflow import Workflow
            - snakemake.deployment.conda.Env import Env
            - example)
              ```
              conda_env = Env(
                  _snakemake_workflow_cache,
                  env_file=conda_env_filepath,
                  env_dir=conda_env_rootpath,
                  container_img=None,
                  cleanup=None,
              )
              conda_env_dirpath = conda_env.address or ""
              ```
        """
        if conda_env_filepath in _global_smk_conda_env_paths_cache:
            conda_env_dirpath = _global_smk_conda_env_paths_cache[conda_env_filepath]
        else:
            conda_env_dirpath = cls._get_conda_env_address(
                conda_env_filepath, conda_env_rootpath
            )
            _global_smk_conda_env_paths_cache[conda_env_filepath] = conda_env_dirpath

        """
        Verify that conda env has been created by snakemake
        NOTE: This determination is defined as follows:
          - snakemake.deployment.conda.Env.create
        """
        is_conda_env_exists = os.path.exists(
            os.path.join(conda_env_dirpath, "env_setup_start")
        ) and os.path.exists(os.path.join(conda_env_dirpath, "env_setup_done"))

        return is_conda_env_exists

    @classmethod
    def _get_conda_env_address(cls, _env_file: str, _env_dir: str) -> str:
        """
        Porting/Emurate `snakemake.deployment.conda.address`
        """

        hash = cls._get_conda_env_hash(_env_file, _env_dir)
        env_dir = _env_dir
        get_path = lambda h: os.path.join(env_dir, h)  # noqa: E731
        hash_candidates = [
            hash[:8],
            hash,
            hash + "_",
            # activate no-shortcuts behavior
            # (so that no admin rights are needed on win)
        ]  # [0] is the old fallback hash (shortened)
        exists = [os.path.exists(get_path(h)) for h in hash_candidates]
        for candidate, candidate_exists in zip(hash_candidates, exists):
            if candidate_exists or candidate == hash_candidates[-1]:
                # exists or it is the last (i.e. the desired one)
                return get_path(candidate)

        return ""  # Invalid value

    @classmethod
    def _get_conda_env_hash(cls, _env_file: str, _env_dir: str) -> str:
        """
        Porting/Emurate `snakemake.deployment.conda.hash`
        """

        md5hash = hashlib.md5()
        # Include the absolute path of the target env dir into the hash.
        # By this, moving the working directory around automatically
        # invalidates all environments. This is necessary, because binaries
        # in conda environments can contain hardcoded absolute RPATHs.
        env_dir = os.path.realpath(_env_dir)
        md5hash.update(env_dir.encode())
        md5hash.update(cls._get_conda_env_content(_env_file))
        _hash = md5hash.hexdigest()

        return _hash

    @classmethod
    def _get_conda_env_content(cls, _env_file: str) -> str:
        """
        Porting/Emurate `snakemake.deployment.conda._get_content`
        """

        with open(_env_file, "rb") as f:
            contents = f.read()

        return contents
