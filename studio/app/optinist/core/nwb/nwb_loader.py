import os
import time

from filelock import FileLock
from pynwb.spec import NWBGroupSpec, NWBNamespaceBuilder


def export_nwb_namespace_file(
    ns_name: str, ns_path: str, ext_path: str, group_spec: NWBGroupSpec
):
    """
    Generation of NWB namespace file (*.extensions.yaml)
    """

    FILE_CACHE_INTERVAL = 180

    lock_path = ns_path + ".lock"

    with FileLock(lock_path, timeout=10):
        flle_update_elapsed_time = (
            (time.time() - os.path.getmtime(ns_path)) if os.path.exists(ns_path) else 0
        )

        if (not os.path.exists(ns_path)) or (
            flle_update_elapsed_time > FILE_CACHE_INTERVAL
        ):
            ns_builder = NWBNamespaceBuilder(
                f"{ns_name} extensions", ns_name, version="0.1.0"
            )
            ns_builder.add_spec(ext_path, group_spec)
            ns_builder.export(ns_path)
