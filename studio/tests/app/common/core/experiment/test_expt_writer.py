import os
import shutil

from studio.app.common.core.experiment.experiment import ExptConfig, ExptFunction
from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.experiment.experiment_writer import ExptConfigWriter
from studio.app.common.core.utils.datetime_utils import (
    get_current_datetime,
    parse_datetime_for_timezone,
)
from studio.app.common.core.workflow.workflow import Edge, Node, NodeData, RunItem
from studio.app.common.core.workflow.workflow_writer import WorkflowConfigWriter
from studio.app.const import DATE_FORMAT
from studio.app.dir_path import DIRPATH

workspace_id = "default2"
unique_id = "expt_test"

node_data = NodeData(label="a", param={}, path="", type="")

nodeDict = {
    "test1": Node(
        id="test1",
        type="a",
        data=node_data,
        position={"x": 0, "y": 0},
        style={
            "border": None,
            "borderRadius": 0,
            "height": 100,
            "padding": 0,
            "width": 180,
        },
    )
}

edgeDict = {
    "test2": Edge(
        id="test2",
        type="a",
        animated=False,
        source="",
        sourceHandle="",
        target="",
        targetHandle="",
        style={},
    )
}


def test_create_config() -> ExptConfig:
    runItem = RunItem(
        name="New Flow",
        nodeDict=nodeDict,
        edgeDict=edgeDict,
        snakemakeParam={},
        nwbParam={},
        forceRunList=[],
    )

    expt_config = ExptConfigWriter(
        workspace_id=workspace_id,
        unique_id=unique_id,
        name=runItem.name,
    ).create_config()

    assert isinstance(expt_config, ExptConfig)
    assert isinstance(expt_config.function, dict)
    assert len(expt_config.function) == 0

    assert expt_config


def test_add_run_info():
    expt_config = ExptConfigWriter(
        workspace_id="",
        unique_id="",
        name="",
    ).add_run_info()

    assert expt_config.success == "running"


def test_add_run_info_rewrites_the_recorded_timezone():
    """A rerun rewrites started_at in the new run's zone, so the recorded
    timezone must follow it: a legacy config with no timezone would otherwise
    have its local-time started_at read back as UTC."""
    existing = ExptConfigReader.create_empty_experiment_config()
    existing.timezone = None
    existing.started_at = "2020/01/01 00:00:00"

    writer = ExptConfigWriter(
        workspace_id="",
        unique_id="",
        name="",
        timezone="Asia/Tokyo",
    )
    writer.builder.set_config(existing)
    expt_config = writer.add_run_info()

    assert expt_config.timezone == "Asia/Tokyo"

    started = parse_datetime_for_timezone(
        expt_config.started_at, DATE_FORMAT, expt_config.timezone
    )
    elapsed = get_current_datetime().timestamp() - started.timestamp()
    assert abs(elapsed) < 60, f"started_at read back skewed by {elapsed} sec"


dirpath = f"{DIRPATH.OUTPUT_DIR}/{workspace_id}/{unique_id}"


def test_function_from_nodeDict():
    WorkflowConfigWriter(
        workspace_id=workspace_id,
        unique_id=unique_id,
        nodeDict=nodeDict,
        edgeDict=edgeDict,
    ).write()

    expt_config = ExptConfigWriter(
        workspace_id=workspace_id,
        unique_id=unique_id,
        name="name",
    ).build_function_from_nodeDict()

    assert isinstance(expt_config.function, dict)
    assert isinstance(expt_config.function["test1"], ExptFunction)


def test_new_write():
    if os.path.exists(dirpath):
        shutil.rmtree(dirpath)

    WorkflowConfigWriter(
        workspace_id=workspace_id,
        unique_id=unique_id,
        nodeDict=nodeDict,
        edgeDict=edgeDict,
    ).write()

    ExptConfigWriter(
        workspace_id=workspace_id,
        unique_id=unique_id,
        name="name",
    ).write()

    assert os.path.exists(f"{dirpath}/experiment.yaml")


def test_write_add():
    ExptConfigWriter(
        workspace_id=workspace_id,
        unique_id=unique_id,
        name="name",
    ).write()

    assert os.path.exists(f"{dirpath}/experiment.yaml")
