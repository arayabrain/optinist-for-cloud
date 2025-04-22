"""
*If this NWB Spec definition file is updated,
run the generation script (`python export_spec_files.py`)
to regenerate the nwb spec files.
"""

from pynwb.spec import NWBDatasetSpec, NWBGroupSpec

NAME = "optinist"

"""
Now we define the data structures. We use `NWBDataInterface` as the base type,
which is the most primitive type you are likely to use as a base. The name of the
class is `CorticalSurface`, and it requires two matrices, `vertices` and `faces`.
"""

postprocess = NWBGroupSpec(
    doc="postprocess",
    datasets=[
        NWBDatasetSpec(
            doc="data",
            shape=[
                (None,),
                (None, None),
                (None, None, None),
                (None, None, None, None),
            ],
            name="data",
            dtype="float",
        )
    ],
    neurodata_type_def="PostProcess",
    neurodata_type_inc="NWBDataInterface",
)

config_data = NWBGroupSpec(
    doc="configuration data",
    datasets=[
        NWBDatasetSpec(
            doc="configuration as JSON string",
            name="config_json",
            dtype="text",  # Use text datatype for strings
        )
    ],
    neurodata_type_def="ConfigData",
    neurodata_type_inc="NWBDataInterface",
)

GROUP_SPEC = [postprocess, config_data]
