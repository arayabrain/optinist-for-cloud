import os

from studio.app.common.core.utils.filepath_creater import join_filepath
from studio.app.dir_path import DIRPATH

workspace_id = "default"
# Not "0123": test_experiment.py copies a fixture aa.nwb there, and the route
# returns whichever .nwb the glob hits first.
unique_id = "nwb_download"


def test_nwb_params(client):
    response = client.get("/nwb")
    data = response.json()

    assert response.status_code == 200
    assert isinstance(data, dict)

    assert isinstance(data["session_description"], str)
    assert data["session_description"] == "optinist"


def _write_nwb(*segments):
    """Place a stand-in .nwb where the download route globs for it."""
    directory = join_filepath([DIRPATH.OUTPUT_DIR, *segments])
    os.makedirs(directory, exist_ok=True)
    path = join_filepath([directory, "test.nwb"])
    with open(path, "wb") as f:
        f.write(b"NWB-payload")
    return path


class TestDownloadNwbWhenAbsent:
    """A missing .nwb must be a 404, not a 200 with the body ``false``.

    Both routes previously ``return False`` when the glob found nothing. FastAPI
    serialised that as HTTP 200 with a 5-byte JSON body, so the browser saved a
    file called ``nwb_<name>.nwb`` containing the word ``false``, and the e2e test
    for the NWB download received a download event and passed.

    The two tests that used to live here asserted ``status_code == 200`` and
    ``response.url``, so they passed on the bug and would have passed on any
    status at all.

    The frontend already handles a rejected request correctly - ``NWBDownloadButton``
    catches and shows a "File not found" snackbar - so 404 is what it was written
    against.
    """

    def test_missing_nwb_returns_404(self, client):
        response = client.get(f"/experiments/download/nwb/{workspace_id}/no-such-uid")

        assert response.status_code == 404

    def test_missing_nwb_for_a_node_returns_404(self, client):
        response = client.get(
            f"/experiments/download/nwb/{workspace_id}/no-such-uid/func1"
        )

        assert response.status_code == 404

    def test_the_response_body_is_not_a_downloadable_false(self, client):
        """The specific false positive: a body the browser would happily save as
        a .nwb file."""
        response = client.get(f"/experiments/download/nwb/{workspace_id}/no-such-uid")

        assert response.content != b"false"


class TestDownloadNwbWhenPresent:
    """The positive path, so the 404 assertions above cannot pass merely because
    the route is broken for every input."""

    def test_existing_nwb_is_returned_as_a_file(self, client):
        _write_nwb(workspace_id, unique_id)

        response = client.get(f"/experiments/download/nwb/{workspace_id}/{unique_id}")

        assert response.status_code == 200
        assert response.content == b"NWB-payload"

    def test_existing_node_nwb_is_returned_as_a_file(self, client):
        _write_nwb(workspace_id, unique_id, "func1")

        response = client.get(
            f"/experiments/download/nwb/{workspace_id}/{unique_id}/func1"
        )

        assert response.status_code == 200
        assert response.content == b"NWB-payload"
