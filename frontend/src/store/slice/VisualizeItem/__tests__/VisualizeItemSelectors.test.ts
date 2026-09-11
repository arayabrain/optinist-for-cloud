import { describe, it, expect } from "@jest/globals"

import { DATA_TYPE_SET } from "store/slice/DisplayData/DisplayDataType"
import { selectTimeSeriesItemKeys } from "store/slice/VisualizeItem/VisualizeItemSelectors"
import { VISUALIZE_ITEM_TYPE_SET } from "store/slice/VisualizeItem/VisualizeItemType"
import { RootState } from "store/store"

// Issue #472: the traces a linked time-series box offers are the intersection of
// the time-series records on disk with the ROI ids of the image box's selected
// projection. A ROI-detection node's `fluorescence` holds a row per detected ROI
// - cells and non-cells alike - so a non_cell_roi projection must still resolve
// to traces. Analysis outputs that filter on iscell (eta's `mean` passes
// `cell_numbers = np.where(iscell > 0)`) hold cell records only, and that
// exclusion, not the visualize wiring, is what leaves the box empty.
const IMAGE_ITEM_ID = 0
const TIME_SERIES_ITEM_ID = 1
const ROI_PATH = "/output/ws/uid/suite2p_roi/noncell_roi.json"

const buildState = (recordIds: string[], roiUniqueList: string[]) =>
  ({
    visualaizeItem: {
      items: {
        [IMAGE_ITEM_ID]: {
          itemType: VISUALIZE_ITEM_TYPE_SET.DISPLAY_DATA,
          dataType: DATA_TYPE_SET.IMAGE,
          roiItem: { filePath: ROI_PATH },
        },
        [TIME_SERIES_ITEM_ID]: {
          itemType: VISUALIZE_ITEM_TYPE_SET.DISPLAY_DATA,
          dataType: DATA_TYPE_SET.TIME_SERIES,
          filePath: "/output/ws/uid/suite2p_roi/fluorescence",
          refImageItemId: IMAGE_ITEM_ID,
        },
      },
    },
    displayData: {
      timeSeries: {
        "/output/ws/uid/suite2p_roi/fluorescence": {
          data: Object.fromEntries(recordIds.map((id) => [id, { "0": 1 }])),
        },
      },
      roi: {
        [ROI_PATH]: {
          type: "roi",
          data: [],
          pending: false,
          fulfilled: true,
          error: null,
          roiUniqueList,
        },
      },
    },
  }) as unknown as RootState

describe("selectTimeSeriesItemKeys", () => {
  it("offers the non-cell ROI traces an unfiltered fluorescence output holds", () => {
    const state = buildState(["0", "1", "2", "3"], ["1", "3"])
    expect(selectTimeSeriesItemKeys(TIME_SERIES_ITEM_ID)(state)).toEqual([
      "1",
      "3",
    ])
  })

  it("offers nothing when the output kept only the cell records", () => {
    // eta and friends re-index onto the cells, so no non-cell id is present
    const state = buildState(["0", "2"], ["1", "3"])
    expect(selectTimeSeriesItemKeys(TIME_SERIES_ITEM_ID)(state)).toEqual([])
  })
})
