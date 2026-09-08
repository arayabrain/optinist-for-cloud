import hashlib
import os

from studio.app.dir_path import DIRPATH


class FileLockUtils:
    @classmethod
    def get_lockfile_path(cls, file_path: str) -> str:
        """
        Automatically generate the save path for the lockfile
          used in the FileLock module.
        """
        file_basename = os.path.basename(file_path)

        # SHA-256 rather than MD5; truncating to 16 hex chars keeps the
        # existing lockfile name length.
        file_path_hash = hashlib.sha256(file_path.encode()).hexdigest()
        file_path_hash = file_path_hash[:16]

        lockfile_path = os.path.join(
            DIRPATH.LOCKFILE_DIR, f"{file_path_hash}_{file_basename}.lock"
        )

        return lockfile_path
