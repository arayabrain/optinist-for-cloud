import numpy as np
import pytest
from fastapi import HTTPException

from studio.app.common.core.utils.pickle_handler import PickleWriter
from studio.app.optinist.core.edit_ROI.edit_ROI import CellType, EditROI
from studio.app.optinist.dataclass import EditRoiData, FluoData, IscellData

# im[i] labels ROI i on its own row, NaN elsewhere - the shape EditROI expects.
NUM_ROI = 3


def build_node_dir(tmp_path, iscell):
    node_dirpath = tmp_path / "ws" / "uid" / "suite2p_roi_00000000000"
    node_dirpath.mkdir(parents=True)

    im = np.full((NUM_ROI, 4, 4), np.nan)
    for i in range(NUM_ROI):
        im[i, i, :] = i

    PickleWriter.write(
        pickle_path=str(node_dirpath / "suite2p_roi.pkl"),
        info={
            "edit_roi_data": EditRoiData(images=None, im=im),
            "iscell": IscellData(np.array(iscell)),
            "fluorescence": FluoData(np.zeros((NUM_ROI, 10))),
        },
    )
    return str(node_dirpath / "cell_roi.json")


def test_promote_marks_non_cell_roi_and_reports_it(tmp_path):
    file_path = build_node_dir(
        tmp_path, [CellType.ROI, CellType.NON_ROI, CellType.NON_ROI]
    )

    EditROI(file_path=file_path).promote([2])

    edit_roi = EditROI(file_path=file_path)
    assert edit_roi.tmp_iscell[2] == CellType.TEMP_PROMOTE
    assert edit_roi.get_status().temp_promote_roi == [2]


def test_promote_rejects_roi_that_is_already_a_cell(tmp_path):
    file_path = build_node_dir(
        tmp_path, [CellType.ROI, CellType.NON_ROI, CellType.NON_ROI]
    )

    with pytest.raises(HTTPException) as excinfo:
        EditROI(file_path=file_path).promote([0])
    assert excinfo.value.status_code == 400


def test_promote_rejects_out_of_range_roi(tmp_path):
    file_path = build_node_dir(
        tmp_path, [CellType.ROI, CellType.NON_ROI, CellType.NON_ROI]
    )

    with pytest.raises(HTTPException) as excinfo:
        EditROI(file_path=file_path).promote([NUM_ROI])
    assert excinfo.value.status_code == 400


def test_non_cell_roi_file_name_follows_the_wrapper_that_wrote_it(tmp_path):
    file_path = build_node_dir(
        tmp_path, [CellType.ROI, CellType.NON_ROI, CellType.NON_ROI]
    )
    edit_roi = EditROI(file_path=file_path)
    resolve = edit_roi._EditROI__non_cell_roi_file_name

    assert resolve() is None

    node_dirpath = tmp_path / "ws" / "uid" / "suite2p_roi_00000000000"
    (node_dirpath / "noncell_roi.json").write_text("{}")
    assert resolve() == "noncell_roi"

    (node_dirpath / "non_cell_roi.json").write_text("{}")
    assert resolve() == "non_cell_roi"


def test_deleting_a_pending_merge_restores_its_sources(tmp_path):
    # Merge marks its sources TEMP_DELETE. Deleting the merged ROI before
    # committing has to put them back: every projection is a max-index flatten,
    # so a merged ROI demoted on top of them would occlude them in non_cell_roi
    # and leave them unclickable, and so unpromotable.
    file_path = build_node_dir(tmp_path, [CellType.ROI, CellType.ROI, CellType.NON_ROI])

    EditROI(file_path=file_path).merge([0, 1])
    merged_id = NUM_ROI
    assert EditROI(file_path=file_path).tmp_iscell[0] == CellType.TEMP_DELETE

    EditROI(file_path=file_path).delete([merged_id])

    edit_roi = EditROI(file_path=file_path)
    assert list(edit_roi.tmp_iscell[:2]) == [CellType.ROI, CellType.ROI]
    assert edit_roi.tmp_iscell[merged_id] == CellType.TEMP_DELETE
    # Kept on purpose: commit still needs it to append the merged ROI's trace.
    assert float(merged_id) in edit_roi.tmp_data.temp_merge_roi


def test_promote_rejects_an_roi_with_no_fluorescence_record(tmp_path):
    # The delete-every-ROI path empties fluorescence while im keeps its rows;
    # promoting one of those would put a cell in cell_roi with nothing to plot.
    file_path = build_node_dir(
        tmp_path, [CellType.NON_ROI, CellType.NON_ROI, CellType.NON_ROI]
    )
    edit_roi = EditROI(file_path=file_path)
    edit_roi.output_info["fluorescence"] = FluoData(np.zeros((0, 10)))

    with pytest.raises(HTTPException) as excinfo:
        edit_roi.promote([1])
    assert excinfo.value.status_code == 400


def test_cancel_drops_a_pending_promotion_from_cell_roi(tmp_path):
    # cancel() rebuilds cell_roi from `!= NON_ROI`, and TEMP_PROMOTE is nonzero,
    # so without mapping it back the cancelled ROI stayed drawn as a cell while
    # the persisted iscell said otherwise.
    file_path = build_node_dir(
        tmp_path, [CellType.ROI, CellType.NON_ROI, CellType.NON_ROI]
    )
    EditROI(file_path=file_path).promote([2])

    edit_roi = EditROI(file_path=file_path)
    edit_roi.cancel()

    assert edit_roi.tmp_iscell[2] == CellType.NON_ROI
    assert CellType.TEMP_PROMOTE not in edit_roi.tmp_iscell


def test_promote_roi_route_refuses_a_traversing_path(client):
    # The guard belongs to the router, so assert it through the route rather
    # than re-testing path_guard, which owns its own coverage.
    response = client.post(
        "/api/visualizations/image/%2e%2e/%2e%2e/etc/passwd/promote_roi"
        "?workspace_id=1",
        json={"ids": [0]},
    )
    assert response.status_code == 400
    # EditROI answers a bare 400 for a missing pickle, so the message is what
    # separates a rejected path from merely having reached a nonexistent node.
    assert response.json()["detail"] == "Invalid path parameter"
