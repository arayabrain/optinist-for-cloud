"""Polling a run whose `experiment.yaml` is unusable must not answer 500.

`ConfigReader.read` returns `{}` for a missing *or* empty file, and
`ExptConfigReader.read` asserts on that, so every caller has to decide what an
unreadable config means. During polling it means "no status yet": the run's
process can die before writing one (a backend restart kills the snakemake
subprocess), it can die mid-write and leave a torn file, and the output directory
can be cleaned up under a finished run. Before this guard the frontend's status
poll answered 500 in a loop instead.

The same absence means the opposite at finalization, which is why
`observe_overall()` is deliberately left unguarded - there, no config is a real
failure and should surface.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from studio.app.common.core.experiment.experiment_reader import ExptConfigReader
from studio.app.common.core.workflow.workflow_result import WorkflowResult
from studio.app.dir_path import DIRPATH

MODULE = "studio.app.common.core.workflow.workflow_result"

# A workspace/uid pair with nothing on disk, so the config really is absent
# rather than mocked away.
ABSENT = ("999999", "no-such-run")


@pytest.fixture()
def torn_config(tmp_path):
    """Write a real `experiment.yaml` and hand back a poller pointed at it.

    The interesting failures are what a partially-flushed file does to the reader,
    so the file goes through `ConfigReader` for real rather than the reader being
    replaced by a mock raising a chosen exception.
    """

    def write(contents):
        workspace_id, unique_id = "888888", "torn-run"
        directory = Path(DIRPATH.OUTPUT_DIR) / workspace_id / unique_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / DIRPATH.EXPERIMENT_YML).write_text(contents)
        return WorkflowResult(workspace_id, unique_id)

    yield write

    directory = Path(DIRPATH.OUTPUT_DIR) / "888888"
    if directory.exists():
        for path in sorted(directory.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        directory.rmdir()


class TestPollingAnAbsentConfig:
    @pytest.mark.asyncio
    async def test_a_poll_reports_no_results_instead_of_raising(self):
        node_results = await WorkflowResult(*ABSENT).observe(["some_node_id"])

        assert node_results == {}

    @pytest.mark.asyncio
    async def test_a_poll_with_no_nodes_does_not_need_the_config_at_all(self):
        """The early return moved above the read, so an empty poll no longer
        depends on a file it never looks at."""
        with patch.object(ExptConfigReader, "read") as read:
            node_results = await WorkflowResult(*ABSENT).observe([])

        assert node_results == {}
        read.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_torn_write_reports_no_results_rather_than_raising(
        self, torn_config
    ):
        """A process killed mid-write leaves YAML that does not parse. This is the
        same "no status yet" as an absent file and reaches the poller the same
        way, so catching only the absent case still 500s in a loop here."""
        poller = torn_config("function:\n  node1: {name: 'unterminated\n")

        assert await poller.observe(["some_node_id"]) == {}

    @pytest.mark.asyncio
    async def test_a_partially_flushed_config_reports_no_results(self, torn_config):
        """The nastier torn write: valid YAML, but flushed before the keys the
        reader requires were written, so it raises `KeyError` rather than a parse
        error."""
        poller = torn_config("workspace_id: '888888'\nunique_id: torn-run\n")

        assert await poller.observe(["some_node_id"]) == {}

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_still_surfaces(self):
        """The guard names the failures that mean "not ready". Anything else is a
        real fault and must not be swallowed, or this becomes a blanket `except`
        that turns every bug in the reader into an empty poll."""
        with patch.object(
            ExptConfigReader, "read", side_effect=PermissionError("output dir locked")
        ):
            with pytest.raises(PermissionError):
                await WorkflowResult(*ABSENT).observe(["some_node_id"])

    @pytest.mark.asyncio
    async def test_a_readable_config_still_reaches_the_observation(self):
        """The positive control: without it, "returns {}" above could be a
        function that always returns {}."""
        sentinel = {"some_node_id": "observed"}
        with patch.object(
            ExptConfigReader, "read", return_value=object()
        ), patch.object(
            WorkflowResult, "_WorkflowResult__observe_nodes", return_value=sentinel
        ), patch.object(
            WorkflowResult,
            "_WorkflowResult__is_workflow_observation_ongoing",
            return_value=False,
        ), patch(
            f"{MODULE}.SmkStatusLogger.get_error_content", return_value=None
        ):
            node_results = await WorkflowResult(*ABSENT).observe(["some_node_id"])

        assert node_results == sentinel
