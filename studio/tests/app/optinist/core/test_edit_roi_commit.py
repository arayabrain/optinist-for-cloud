import numpy as np

from studio.app.optinist.core.edit_ROI.edit_ROI import CellType
from studio.app.optinist.core.edit_ROI.wrappers.vacant_roi_edit_roi import commit_edit
from studio.app.optinist.dataclass import EditRoiData, FluoData

# vacant_roi, lccd and caiman share this commit implementation verbatim.
NUM_COMMITTED = 3
FRAMES = 5
SHAPE = (4, 4)


def build_state(iscell):
    """Three committed ROIs plus one appended by a pending merge of ROI 0 and 1."""
    im = np.full((NUM_COMMITTED + 1, *SHAPE), np.nan)
    for i in range(NUM_COMMITTED):
        im[i, i, :] = i
    im[NUM_COMMITTED, 0:2, :] = NUM_COMMITTED

    data = EditRoiData(images=None, im=im)
    data.temp_merge_roi[float(NUM_COMMITTED)] = [0, 1]

    images = np.arange(FRAMES * SHAPE[0] * SHAPE[1], dtype=float).reshape(
        FRAMES, *SHAPE
    )
    return images, data, FluoData(np.ones((NUM_COMMITTED, FRAMES))), np.array(iscell)


def test_merge_promotes_the_new_roi_and_demotes_its_sources(tmp_path):
    images, data, fluorescence, iscell = build_state(
        [CellType.TEMP_DELETE, CellType.TEMP_DELETE, CellType.ROI, CellType.TEMP_ADD]
    )

    info = commit_edit(images, data, fluorescence, iscell, str(tmp_path), "vacant_roi")

    assert list(info["iscell"].data) == [
        CellType.NON_ROI,
        CellType.NON_ROI,
        CellType.ROI,
        CellType.ROI,
    ]


def test_deleting_a_pending_merge_leaves_it_demoted_with_a_real_trace(tmp_path):
    # The user merged ROI 0 and 1, then deleted the merged ROI before committing.
    images, data, fluorescence, iscell = build_state(
        [
            CellType.TEMP_DELETE,
            CellType.TEMP_DELETE,
            CellType.ROI,
            CellType.TEMP_DELETE,
        ]
    )

    info = commit_edit(images, data, fluorescence, iscell, str(tmp_path), "vacant_roi")

    # The merged ROI stays demoted rather than being resurrected by its still
    # pending temp_merge_roi entry, and its sources are non-cell ROIs the user
    # can promote back separately.
    assert list(info["iscell"].data) == [CellType.NON_ROI] * 2 + [
        CellType.ROI,
        CellType.NON_ROI,
    ]

    # It still gets a real trace, so fluorescence stays one row per im row and
    # the non_cell_roi view can serve it.
    new_fluorescences = info["fluorescence"].data
    assert len(new_fluorescences) == len(data.im)
    expected = np.mean(images[:, ~np.isnan(data.im[NUM_COMMITTED])], axis=1)
    assert np.allclose(new_fluorescences[NUM_COMMITTED], expected)
    assert not np.allclose(new_fluorescences[NUM_COMMITTED], 0)


def test_a_deleted_pending_merge_commits_with_its_sources_kept(tmp_path):
    # The state EditROI.delete leaves for an undone merge: sources restored,
    # merged ROI marked for deletion, and its temp_merge_roi entry kept so the
    # trace is still appended and fluorescence stays aligned with im.
    images, data, fluorescence, iscell = build_state(
        [CellType.ROI, CellType.ROI, CellType.ROI, CellType.TEMP_DELETE]
    )

    info = commit_edit(images, data, fluorescence, iscell, str(tmp_path), "vacant_roi")

    assert list(info["iscell"].data) == [CellType.ROI] * 3 + [CellType.NON_ROI]
    assert len(info["fluorescence"].data) == len(data.im)


def test_a_retained_non_cell_is_not_resurrected_when_its_trace_is_recomputed(
    tmp_path,
):
    # The delete-every-ROI path leaves fluorescence empty while im keeps every
    # row, so "appended in this session" cannot be inferred from the row range
    # alone. Every row still needs a trace to keep F aligned with im, but only
    # the ones actually marked TEMP_ADD may become cells.
    images, data, fluorescence, iscell = build_state(
        [
            CellType.NON_ROI,
            CellType.NON_ROI,
            CellType.NON_ROI,
            CellType.TEMP_ADD,
        ]
    )
    empty = FluoData(np.zeros((0, FRAMES)))

    info = commit_edit(images, data, empty, iscell, str(tmp_path), "vacant_roi")

    assert list(info["iscell"].data) == [CellType.NON_ROI] * 3 + [CellType.ROI]
    assert len(info["fluorescence"].data) == len(data.im)
