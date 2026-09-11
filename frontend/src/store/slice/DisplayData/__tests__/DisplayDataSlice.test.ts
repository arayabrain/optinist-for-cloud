import { describe, it, expect } from "@jest/globals"

import {
  getRoiData,
  getImageData,
  getCsvData,
} from "store/slice/DisplayData/DisplayDataActions"
import reducer from "store/slice/DisplayData/DisplayDataSlice"

describe("DisplayDataSlice", () => {
  const initialState = {
    timeSeries: {},
    heatMap: {},
    image: {},
    csv: {},
    roi: {},
    scatter: {},
    bar: {},
    html: {},
    histogram: {},
    line: {},
    pie: {},
    polar: {},
    loading: false,
    loadingStack: [],
    statusRoi: {
      temp_add_roi: [],
      temp_delete_roi: [],
      temp_merge_roi: [],
      temp_promote_roi: [],
    },
    isEditRoiCommitting: false,
  }

  describe("getRoiData.rejected", () => {
    it("shows payload message when provided", () => {
      const pendingState = {
        ...initialState,
        loading: true,
        loadingStack: [true],
        roi: {
          "/test/path": {
            type: "roi" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
            roiUniqueList: [],
          },
        },
      }

      const action = {
        type: getRoiData.rejected.type,
        meta: { arg: { path: "/test/path", workspaceId: 1 } },
        payload: {
          message: "Request failed with status code 500",
          status: 500,
        },
        error: { message: "Rejected" },
      }

      const state = reducer(pendingState, action)

      expect(state.roi["/test/path"].error).toBe(
        "Request failed with status code 500",
      )
      expect(state.roi["/test/path"].pending).toBe(false)
      expect(state.roi["/test/path"].fulfilled).toBe(false)
    })

    it("shows 'Data not synced' when payload has no message", () => {
      const pendingState = {
        ...initialState,
        loading: true,
        loadingStack: [true],
        roi: {
          "/test/path": {
            type: "roi" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
            roiUniqueList: [],
          },
        },
      }

      const action = {
        type: getRoiData.rejected.type,
        meta: { arg: { path: "/test/path", workspaceId: 1 } },
        payload: undefined,
        error: { message: "Rejected" },
      }

      const state = reducer(pendingState, action)

      expect(state.roi["/test/path"].error).toBe("Data not synced")
    })

    it("shows syncing message for 503 status", () => {
      const pendingState = {
        ...initialState,
        loading: true,
        loadingStack: [true],
        roi: {
          "/test/path": {
            type: "roi" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
            roiUniqueList: [],
          },
        },
      }

      const action = {
        type: getRoiData.rejected.type,
        meta: { arg: { path: "/test/path", workspaceId: 1 } },
        payload: {
          message: "Data syncing. Please retry.",
          status: 503,
        },
        error: { message: "Rejected" },
      }

      const state = reducer(pendingState, action)

      expect(state.roi["/test/path"].error).toBe(
        "Syncing from cloud storage...",
      )
    })

    it("updates loading state correctly", () => {
      const pendingState = {
        ...initialState,
        loading: true,
        loadingStack: [true],
        roi: {
          "/test/path": {
            type: "roi" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
            roiUniqueList: [],
          },
        },
      }

      const action = {
        type: getRoiData.rejected.type,
        meta: { arg: { path: "/test/path", workspaceId: 1 } },
        payload: { message: "Error" },
        error: { message: "Rejected" },
      }

      const state = reducer(pendingState, action)

      expect(state.loading).toBe(false)
      expect(state.loadingStack).toHaveLength(0)
    })
  })

  describe("getImageData.rejected", () => {
    it("shows payload message when provided", () => {
      const pendingState = {
        ...initialState,
        image: {
          "/test/image.tiff": {
            type: "image" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
          },
        },
      }

      const action = {
        type: getImageData.rejected.type,
        meta: {
          arg: {
            path: "/test/image.tiff",
            workspaceId: 1,
            startIndex: 1,
            endIndex: 1,
          },
        },
        payload: {
          message: "Request failed with status code 500",
          status: 500,
        },
        error: { message: "Rejected" },
      }

      const state = reducer(pendingState, action)

      expect(state.image["/test/image.tiff"].error).toBe(
        "Request failed with status code 500",
      )
      expect(state.image["/test/image.tiff"].pending).toBe(false)
      expect(state.image["/test/image.tiff"].fulfilled).toBe(false)
    })

    it("shows 'Image not synced' when payload has no message", () => {
      const pendingState = {
        ...initialState,
        image: {
          "/test/image.tiff": {
            type: "image" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
          },
        },
      }

      const action = {
        type: getImageData.rejected.type,
        meta: {
          arg: {
            path: "/test/image.tiff",
            workspaceId: 1,
            startIndex: 1,
            endIndex: 1,
          },
        },
        payload: undefined,
        error: { message: "Rejected" },
      }

      const state = reducer(pendingState, action)

      expect(state.image["/test/image.tiff"].error).toBe("Image not synced")
    })

    it("shows syncing message for 503 status", () => {
      const pendingState = {
        ...initialState,
        image: {
          "/test/image.tiff": {
            type: "image" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
          },
        },
      }

      const action = {
        type: getImageData.rejected.type,
        meta: {
          arg: {
            path: "/test/image.tiff",
            workspaceId: 1,
            startIndex: 1,
            endIndex: 1,
          },
        },
        payload: {
          message: "Data syncing. Please retry.",
          status: 503,
        },
        error: { message: "Rejected" },
      }

      const state = reducer(pendingState, action)

      expect(state.image["/test/image.tiff"].error).toBe(
        "Syncing from cloud storage...",
      )
    })
  })

  describe("getRoiData.pending", () => {
    it("sets pending state and adds to loading stack", () => {
      const action = {
        type: getRoiData.pending.type,
        meta: { arg: { path: "/test/path", workspaceId: 1 } },
      }

      const state = reducer(initialState, action)

      expect(state.roi["/test/path"].pending).toBe(true)
      expect(state.roi["/test/path"].fulfilled).toBe(false)
      expect(state.roi["/test/path"].error).toBe(null)
      expect(state.loading).toBe(true)
      expect(state.loadingStack).toHaveLength(1)
    })
  })

  describe("getImageData.pending", () => {
    it("sets pending state for image", () => {
      const action = {
        type: getImageData.pending.type,
        meta: {
          arg: {
            path: "/test/image.tiff",
            workspaceId: 1,
            startIndex: 1,
            endIndex: 1,
          },
        },
      }

      const state = reducer(initialState, action)

      expect(state.image["/test/image.tiff"].pending).toBe(true)
      expect(state.image["/test/image.tiff"].fulfilled).toBe(false)
      expect(state.image["/test/image.tiff"].error).toBe(null)
    })
  })

  describe("getCsvData.pending", () => {
    it("sets pending state for csv", () => {
      const action = {
        type: getCsvData.pending.type,
        meta: { arg: { path: "/test/data.csv", workspaceId: 1 } },
      }

      const state = reducer(initialState, action)

      expect(state.csv["/test/data.csv"].pending).toBe(true)
      expect(state.csv["/test/data.csv"].fulfilled).toBe(false)
      expect(state.csv["/test/data.csv"].error).toBe(null)
    })
  })

  describe("getCsvData.fulfilled", () => {
    it("sets fulfilled state with data", () => {
      const pendingState = {
        ...initialState,
        csv: {
          "/test/data.csv": {
            type: "csv" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
          },
        },
      }

      const action = {
        type: getCsvData.fulfilled.type,
        meta: { arg: { path: "/test/data.csv", workspaceId: 1 } },
        payload: {
          data: [[1, 2, 3]],
          meta: { title: "CSV Data" },
        },
      }

      const state = reducer(pendingState, action)

      expect(state.csv["/test/data.csv"].pending).toBe(false)
      expect(state.csv["/test/data.csv"].fulfilled).toBe(true)
      expect(state.csv["/test/data.csv"].error).toBe(null)
      expect(state.csv["/test/data.csv"].data).toEqual([[1, 2, 3]])
    })
  })

  describe("getCsvData.rejected", () => {
    it("shows payload message when provided", () => {
      const pendingState = {
        ...initialState,
        csv: {
          "/test/data.csv": {
            type: "csv" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
          },
        },
      }

      const action = {
        type: getCsvData.rejected.type,
        meta: { arg: { path: "/test/data.csv", workspaceId: 1 } },
        payload: {
          message: "Request failed with status code 500",
          status: 500,
        },
        error: { message: "Rejected" },
      }

      const state = reducer(pendingState, action)

      expect(state.csv["/test/data.csv"].error).toBe(
        "Request failed with status code 500",
      )
      expect(state.csv["/test/data.csv"].pending).toBe(false)
      expect(state.csv["/test/data.csv"].fulfilled).toBe(false)
    })

    it("shows 'CSV not synced' when payload has no message", () => {
      const pendingState = {
        ...initialState,
        csv: {
          "/test/data.csv": {
            type: "csv" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
          },
        },
      }

      const action = {
        type: getCsvData.rejected.type,
        meta: { arg: { path: "/test/data.csv", workspaceId: 1 } },
        payload: undefined,
        error: { message: "Rejected" },
      }

      const state = reducer(pendingState, action)

      expect(state.csv["/test/data.csv"].error).toBe("CSV not synced")
    })

    it("shows syncing message for 503 status", () => {
      const pendingState = {
        ...initialState,
        csv: {
          "/test/data.csv": {
            type: "csv" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
          },
        },
      }

      const action = {
        type: getCsvData.rejected.type,
        meta: { arg: { path: "/test/data.csv", workspaceId: 1 } },
        payload: {
          message: "Failed to sync input file from cloud storage",
          status: 503,
        },
        error: { message: "Rejected" },
      }

      const state = reducer(pendingState, action)

      expect(state.csv["/test/data.csv"].error).toBe(
        "Syncing from cloud storage...",
      )
      expect(state.csv["/test/data.csv"].errorStatus).toBe(503)
    })

    it("stores errorStatus from payload", () => {
      const pendingState = {
        ...initialState,
        csv: {
          "/test/data.csv": {
            type: "csv" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
          },
        },
      }

      const action = {
        type: getCsvData.rejected.type,
        meta: { arg: { path: "/test/data.csv", workspaceId: 1 } },
        payload: {
          message: "Input CSV file not found: data.csv",
          status: 404,
        },
        error: { message: "Rejected" },
      }

      const state = reducer(pendingState, action)

      expect(state.csv["/test/data.csv"].error).toBe(
        "Input CSV file not found: data.csv",
      )
      expect(state.csv["/test/data.csv"].errorStatus).toBe(404)
    })
  })

  describe("getRoiData.fulfilled", () => {
    it("sets fulfilled state with data", () => {
      const pendingState = {
        ...initialState,
        loading: true,
        loadingStack: [true],
        roi: {
          "/test/path": {
            type: "roi" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
            roiUniqueList: [],
          },
        },
      }

      const action = {
        type: getRoiData.fulfilled.type,
        meta: { arg: { path: "/test/path", workspaceId: 1 } },
        payload: {
          data: [
            [
              [1, 2, 3],
              [4, 5, 6],
            ],
          ],
          meta: { title: "ROI Data" },
        },
      }

      const state = reducer(pendingState, action)

      expect(state.roi["/test/path"].pending).toBe(false)
      expect(state.roi["/test/path"].fulfilled).toBe(true)
      expect(state.roi["/test/path"].error).toBe(null)
      expect(state.roi["/test/path"].data).toHaveLength(1)
      expect(state.loading).toBe(false)
    })
  })

  describe("getImageData.fulfilled", () => {
    it("sets fulfilled state with data", () => {
      const pendingState = {
        ...initialState,
        image: {
          "/test/image.tiff": {
            type: "image" as const,
            data: [],
            pending: true,
            fulfilled: false,
            error: null,
          },
        },
      }

      const action = {
        type: getImageData.fulfilled.type,
        meta: {
          arg: {
            path: "/test/image.tiff",
            workspaceId: 1,
            startIndex: 1,
            endIndex: 1,
          },
        },
        payload: {
          data: [
            [
              [1, 2, 3],
              [4, 5, 6],
            ],
          ],
          meta: { title: "Image Data" },
        },
      }

      const state = reducer(pendingState, action)

      expect(state.image["/test/image.tiff"].pending).toBe(false)
      expect(state.image["/test/image.tiff"].fulfilled).toBe(true)
      expect(state.image["/test/image.tiff"].error).toBe(null)
      expect(state.image["/test/image.tiff"].data).toHaveLength(1)
    })
  })
})
