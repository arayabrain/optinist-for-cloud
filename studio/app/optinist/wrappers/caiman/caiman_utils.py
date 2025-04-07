import os
import re
import shutil


class CaimanUtils:
    """
    Utility functions for Caiman
    """

    CAIMAN_TEMP_ENV_VAR_NAME = "CAIMAN_TEMP"

    @staticmethod
    def get_caiman_tempdir() -> str:
        import caiman.paths

        try:
            caiman_tempdir_path = caiman.paths.get_tempdir()
        except Exception:
            caiman_tempdir_path = os.path.join(caiman.paths.caiman_datadir(), "temp")

        return caiman_tempdir_path

    @classmethod
    def set_caimam_byid_tempdir(cls, id: str):
        # If "CAIMAN_TEMP" env var is specified, skip
        if cls.CAIMAN_TEMP_ENV_VAR_NAME in os.environ:
            return

        caiman_tempdir_path = os.path.join(cls.get_caiman_tempdir(), id)
        os.makedirs(caiman_tempdir_path, exist_ok=True)
        os.environ[cls.CAIMAN_TEMP_ENV_VAR_NAME] = caiman_tempdir_path

    @classmethod
    def cleanup_caiman_byid_tempdir(cls, id: str):
        caiman_tempdir_path = cls.get_caiman_tempdir()

        if re.search(f"{id}$", caiman_tempdir_path):
            shutil.rmtree(caiman_tempdir_path)

            if cls.CAIMAN_TEMP_ENV_VAR_NAME in os.environ:
                del os.environ[cls.CAIMAN_TEMP_ENV_VAR_NAME]
