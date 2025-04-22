import sys
from os.path import abspath, dirname

ROOT_DIRPATH = dirname(
    dirname(dirname(dirname(dirname(dirname(dirname(abspath(__file__)))))))
)
sys.path.append(ROOT_DIRPATH)

if __name__ == "__main__":
    """
    Note: If NWB Spec files are added, add the export process below.
    """

    from studio.app.optinist.core.nwb.nwb_spec_utils import export_spec_files
    from studio.app.optinist.core.nwb.specs.optinist_spec import (
        GROUP_SPEC as OPTINIST_GROUP_SPEC,
    )
    from studio.app.optinist.core.nwb.specs.optinist_spec import NAME as OPTINIST_NAME

    export_spec_files(OPTINIST_NAME, OPTINIST_GROUP_SPEC, 0)
