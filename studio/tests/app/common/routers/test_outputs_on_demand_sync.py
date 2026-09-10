"""On-demand input sync at each ``outputs.py`` call site, per file format.

Covers the CSV path, the TIFF path, and the re-fetch after a cache cleanup.

These had been pinned to ``test_input_file_lock.py::TestEnsureInputFileSynced``,
whose two cases cover only the already-cached fast path and an exception
propagation, and neither varies file format.

The public tier serves published experiments from an EFS cache that
``public_cleanup`` wipes daily (see ``test_public_instance_config.py``), so the
"file is absent locally" branch is the *normal* path here, not an edge case. If a
call site stops reaching for the remote copy, the visualization silently 404s the
day after a cleanup run.

These assert the wiring at each call site. ``ensure_input_file_synced``'s own
behaviour is ``test_input_file_lock.py``'s; the real S3 round-trip is manual.
"""

import inspect
import os
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException

from studio.app.common.core.storage.remote_storage_controller import (
    RemoteStorageDownloadUtils,
)

MODULE = "studio.app.common.routers.outputs"

WORKSPACE_ID = "1"
REMOTE_BUCKET = "optinist-test-bucket"


@pytest.fixture
def sync_call():
    """Patch ``ensure_input_file_synced`` and report whether it was awaited."""
    with patch.object(
        RemoteStorageDownloadUtils, "ensure_input_file_synced", new_callable=AsyncMock
    ) as mock_sync:
        yield mock_sync


class TestCsvCallSiteSyncsOnDemand:
    """A CSV input absent from the local cache is re-fetched."""

    @pytest.mark.asyncio
    async def test_sync_is_awaited_for_the_requested_csv(self, sync_call):
        from studio.app.common.routers.outputs import get_csv

        sync_call.return_value = False  # not recoverable, so the read is skipped

        with pytest.raises(HTTPException):
            await get_csv(
                filepath="measurements.csv",
                workspace_id=WORKSPACE_ID,
                remote_bucket_name=REMOTE_BUCKET,
            )

        sync_call.assert_awaited_once_with(
            WORKSPACE_ID, "measurements.csv", REMOTE_BUCKET
        )

    @pytest.mark.asyncio
    async def test_a_csv_that_cannot_be_synced_is_404_not_500(self, sync_call):
        """``pd.read_csv`` on a missing path raises ``FileNotFoundError``, which
        would surface as a bare 500. The SPA distinguishes 404 from 5xx."""
        from studio.app.common.routers.outputs import get_csv

        sync_call.return_value = False

        with pytest.raises(HTTPException) as excinfo:
            await get_csv(
                filepath="measurements.csv",
                workspace_id=WORKSPACE_ID,
                remote_bucket_name=REMOTE_BUCKET,
            )

        assert excinfo.value.status_code == 404
        assert "measurements.csv" in excinfo.value.detail

    @pytest.mark.asyncio
    async def test_a_remote_storage_error_is_503_not_404(self, sync_call):
        """503 tells the client to retry; a 404 would read as "this file does not
        exist" and the SPA would stop asking."""
        from studio.app.common.routers.outputs import get_csv

        sync_call.side_effect = RuntimeError("S3 unreachable")

        with pytest.raises(HTTPException) as excinfo:
            await get_csv(
                filepath="measurements.csv",
                workspace_id=WORKSPACE_ID,
                remote_bucket_name=REMOTE_BUCKET,
            )

        assert excinfo.value.status_code == 503

    @pytest.mark.asyncio
    async def test_the_local_file_is_read_once_the_sync_reports_success(
        self, sync_call, tmp_path
    ):
        """The success path, so the failure assertions above are not passing
        merely because this route always raises."""
        from studio.app.common.routers.outputs import get_csv

        sync_call.return_value = True
        csv_path = tmp_path / "measurements.csv"
        csv_path.write_text("1,2\n3,4\n")

        with patch(f"{MODULE}.DIRPATH") as dirpath, patch(
            f"{MODULE}.JsonWriter.write_as_split"
        ), patch(f"{MODULE}.JsonReader.read_as_output", return_value="output"), patch(
            f"{MODULE}.create_directory"
        ):
            dirpath.INPUT_DIR = str(tmp_path)
            result = await get_csv(
                filepath="measurements.csv",
                workspace_id="",
                remote_bucket_name=REMOTE_BUCKET,
            )

        assert result == "output"
        sync_call.assert_awaited_once()


class TestTiffCallSiteSyncsOnDemand:
    """A TIFF input absent from the local cache is re-fetched.

    The TIFF branch is reached only when the path has no workspace prefix, which
    is how ``outputs.py`` distinguishes a raw input image from an analysis output.
    """

    @pytest.mark.asyncio
    async def test_sync_is_awaited_for_an_input_tiff(self, sync_call):
        from studio.app.common.routers.outputs import get_image

        sync_call.return_value = False

        with pytest.raises(HTTPException):
            await get_image(
                filepath="raw_stack.tif",
                workspace_id=WORKSPACE_ID,
                start_index=0,
                end_index=10,
                remote_bucket_name=REMOTE_BUCKET,
            )

        sync_call.assert_awaited_once_with(WORKSPACE_ID, "raw_stack.tif", REMOTE_BUCKET)

    @pytest.mark.asyncio
    async def test_an_unsyncable_input_tiff_is_404(self, sync_call):
        from studio.app.common.routers.outputs import get_image

        sync_call.return_value = False

        with pytest.raises(HTTPException) as excinfo:
            await get_image(
                filepath="raw_stack.tif",
                workspace_id=WORKSPACE_ID,
                start_index=0,
                end_index=10,
                remote_bucket_name=REMOTE_BUCKET,
            )

        assert excinfo.value.status_code == 404
        assert "raw_stack.tif" in excinfo.value.detail

    @pytest.mark.asyncio
    async def test_a_remote_storage_error_on_a_tiff_is_503(self, sync_call):
        from studio.app.common.routers.outputs import get_image

        sync_call.side_effect = RuntimeError("S3 unreachable")

        with pytest.raises(HTTPException) as excinfo:
            await get_image(
                filepath="raw_stack.tif",
                workspace_id=WORKSPACE_ID,
                start_index=0,
                end_index=10,
                remote_bucket_name=REMOTE_BUCKET,
            )

        assert excinfo.value.status_code == 503

    @pytest.mark.asyncio
    async def test_an_output_tiff_does_not_take_the_input_sync_path(self, sync_call):
        """A path prefixed with the workspace id is an analysis output, synced by
        ``_ensure_visualization_synced`` instead. Calling the input helper for it
        would look for the file under the wrong prefix."""
        from studio.app.common.routers.outputs import get_image

        with patch(
            f"{MODULE}._ensure_visualization_synced", new_callable=AsyncMock
        ), patch(f"{MODULE}.os.path.exists", return_value=True), patch(
            f"{MODULE}.JsonReader.read_as_output", return_value="output"
        ):
            result = await get_image(
                filepath=f"{WORKSPACE_ID}/exp1/node1/plot.tif",
                workspace_id=WORKSPACE_ID,
                start_index=0,
                end_index=10,
                remote_bucket_name=REMOTE_BUCKET,
            )

        assert result == "output"
        sync_call.assert_not_awaited()


class TestStructuredCallSiteRefetchesAfterCleanup:
    """An input re-fetched after the cache was cleared.

    Inputs are wiped independently of outputs by the daily ``public_cleanup``
    Lambda, so this re-fetch must key on the input file itself and not on the
    experiment's output-sync status - otherwise a fully synced experiment whose
    input cache was wiped reports the file as permanently missing.
    """

    @staticmethod
    def _node(path="raw.hdf5"):
        node = Mock()
        node.data.path = path
        node.data.hdf5Path = None
        node.data.matPath = None
        return node

    @pytest.mark.asyncio
    async def test_a_missing_input_triggers_a_refetch(self, sync_call):
        from studio.app.common.routers.outputs import get_structured_data

        config = Mock(nodeDict={"node1": self._node()})

        with patch(f"{MODULE}.WorkflowConfigReader.read", return_value=config), patch(
            f"{MODULE}.os.path.exists", return_value=False
        ):
            with pytest.raises(HTTPException) as excinfo:
                await get_structured_data(
                    workspace_id=WORKSPACE_ID,
                    unique_id="exp1",
                    node_id="node1",
                    remote_bucket_name=REMOTE_BUCKET,
                )

        sync_call.assert_awaited_once_with(WORKSPACE_ID, "raw.hdf5", REMOTE_BUCKET)
        assert excinfo.value.status_code == 404

    @pytest.mark.asyncio
    async def test_a_cached_input_is_not_refetched(self, sync_call):
        """The fast path. A re-fetch on every request would put an S3 round trip
        in front of every visualization load on the public tier."""
        from studio.app.common.routers.outputs import get_structured_data

        config = Mock(nodeDict={"node1": self._node()})

        with patch(f"{MODULE}.WorkflowConfigReader.read", return_value=config), patch(
            f"{MODULE}.os.path.exists", return_value=True
        ), patch(f"{MODULE}.h5py.File"):
            try:
                await get_structured_data(
                    workspace_id=WORKSPACE_ID,
                    unique_id="exp1",
                    node_id="node1",
                    remote_bucket_name=REMOTE_BUCKET,
                )
            except HTTPException:
                pass  # the read itself is not the subject here

        sync_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_remote_storage_error_during_refetch_is_503(self, sync_call):
        from studio.app.common.routers.outputs import get_structured_data

        config = Mock(nodeDict={"node1": self._node()})
        sync_call.side_effect = RuntimeError("EFS mount gone")

        with patch(f"{MODULE}.WorkflowConfigReader.read", return_value=config), patch(
            f"{MODULE}.os.path.exists", return_value=False
        ):
            with pytest.raises(HTTPException) as excinfo:
                await get_structured_data(
                    workspace_id=WORKSPACE_ID,
                    unique_id="exp1",
                    node_id="node1",
                    remote_bucket_name=REMOTE_BUCKET,
                )

        assert excinfo.value.status_code == 503

    @pytest.mark.asyncio
    async def test_a_list_valued_node_path_syncs_its_first_entry(self, sync_call):
        """``node.data.path`` is a list for multi-file nodes; the sync must be
        keyed on a filename, not on the repr of a list."""
        from studio.app.common.routers.outputs import get_structured_data

        config = Mock(
            nodeDict={"node1": self._node(path=["first.hdf5", "second.hdf5"])}
        )

        with patch(f"{MODULE}.WorkflowConfigReader.read", return_value=config), patch(
            f"{MODULE}.os.path.exists", return_value=False
        ):
            with pytest.raises(HTTPException):
                await get_structured_data(
                    workspace_id=WORKSPACE_ID,
                    unique_id="exp1",
                    node_id="node1",
                    remote_bucket_name=REMOTE_BUCKET,
                )

        assert sync_call.await_args.args[1] == "first.hdf5"


def test_the_three_call_sites_are_all_covered():
    """Guards this file against a new ``ensure_input_file_synced`` call site
    landing in ``outputs.py`` with no test - the failure mode that left these
    paths credited to a helper-level test in the first place.
    """
    import studio.app.common.routers.outputs as outputs_module

    source = inspect.getsource(outputs_module)
    call_sites = source.count("ensure_input_file_synced(")

    assert call_sites == 3, (
        f"outputs.py now has {call_sites} ensure_input_file_synced call sites; "
        f"add a case for the new one (CSV, TIFF and structured cover three)"
    )


@pytest.mark.asyncio
async def test_the_helper_resolves_the_path_under_the_input_dir():
    """The call sites pass a bare filename and let the helper build the path.

    Asserted through the helper's own behaviour rather than by reading its
    source: a call site that pre-joined ``INPUT_DIR`` would double the prefix and
    silently miss the cached file.
    """
    from studio.app.dir_path import DIRPATH

    with patch(
        "studio.app.common.core.storage.remote_storage_controller.os.path.exists"
    ) as exists:
        exists.return_value = True
        assert await RemoteStorageDownloadUtils.ensure_input_file_synced(
            WORKSPACE_ID, "raw.hdf5", REMOTE_BUCKET
        )

    checked = exists.call_args.args[0]
    assert checked == os.path.join(DIRPATH.INPUT_DIR, WORKSPACE_ID, "raw.hdf5")
