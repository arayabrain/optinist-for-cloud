from pynwb import get_class, load_namespaces
from pynwb.spec import NWBDatasetSpec, NWBGroupSpec

from studio.app.optinist.core.nwb.nwb_loader import export_nwb_namespace_file

name = "optinist"
ns_path = f"{name}.namespace.yaml"
ext_source = f"{name}.extensions.yaml"

# Now we define the data structures. We use `NWBDataInterface` as the base type,
# which is the most primitive type you are likely to use as a base. The name of the
# class is `CorticalSurface`, and it requires two matrices, `vertices` and
# `faces`.

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

# Now we set up the builder and add this object

export_nwb_namespace_file(name, ns_path, ext_source, postprocess)

load_namespaces(ns_path)

PostProcess = get_class("PostProcess", name)
