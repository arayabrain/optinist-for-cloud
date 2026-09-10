import React from "react"
import { Provider } from "react-redux"

import configureStore from "redux-mock-store"
import thunk from "redux-thunk"

import { describe, it, expect, jest, beforeEach } from "@jest/globals"
import { render, screen, fireEvent } from "@testing-library/react"

import {
  RoiPlotSimple,
  RoiPlotSimpleWithLoading,
} from "components/Workspace/Visualize/Plot/RoiPlotSimple"

const mockStore = configureStore([thunk])

// Mock react-plotlyjs-ts to avoid d3-interpolate issues
jest.mock("react-plotlyjs-ts", () => ({
  __esModule: true,
  default: () => <div data-testid="plotly-chart">Plotly Chart</div>,
}))

// Mock getRoiData action - return a thunk-like function
const mockGetRoiData = jest.fn()
jest.mock("store/slice/DisplayData/DisplayDataActions", () => ({
  getRoiData: (params: {
    path: string
    workspaceId: number
    uniqueId?: string
  }) => {
    mockGetRoiData(params)
    // Return a thunk function
    return () => Promise.resolve()
  },
  SYNC_IN_PROGRESS_MESSAGE: "Syncing from cloud storage...",
}))

describe("RoiPlotSimple Component", () => {
  let store: ReturnType<typeof mockStore>

  beforeEach(() => {
    mockGetRoiData.mockClear()
  })

  const renderWithProviders = (
    component: React.ReactElement,
    customStore?: ReturnType<typeof mockStore>,
  ) => {
    return render(<Provider store={customStore || store}>{component}</Provider>)
  }

  describe("Error state with retry button", () => {
    it("shows error message and retry button when error occurs", () => {
      const initialState = {
        displayData: {
          roi: {
            "/test/path": {
              type: "roi",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Data not synced",
              roiUniqueList: [],
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimple filePath="/test/path" workspaceId={1} />,
      )

      // Should show error message
      expect(screen.getByText("Data not synced")).toBeDefined()

      // Should show retry button
      const retryButton = screen.getByRole("button")
      expect(retryButton).toBeDefined()
    })

    it("calls getRoiData when retry button is clicked", () => {
      const initialState = {
        displayData: {
          roi: {
            "/test/path": {
              type: "roi",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Data unavailable",
              roiUniqueList: [],
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimple filePath="/test/path" workspaceId={1} />,
      )

      // Click retry button
      const retryButton = screen.getByRole("button")
      fireEvent.click(retryButton)

      // Should dispatch getRoiData action
      expect(mockGetRoiData).toHaveBeenCalledWith({
        path: "/test/path",
        workspaceId: 1,
        uniqueId: undefined,
      })
    })

    it("includes uniqueId when provided", () => {
      const initialState = {
        displayData: {
          roi: {
            "/test/path": {
              type: "roi",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Data unavailable",
              roiUniqueList: [],
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimple
          filePath="/test/path"
          workspaceId={1}
          uniqueId="workflow-123"
        />,
      )

      // Click retry button
      const retryButton = screen.getByRole("button")
      fireEvent.click(retryButton)

      // Should dispatch getRoiData action with uniqueId
      expect(mockGetRoiData).toHaveBeenCalledWith({
        path: "/test/path",
        workspaceId: 1,
        uniqueId: "workflow-123",
      })
    })

    it("hides retry button when error contains 'not found'", () => {
      const initialState = {
        displayData: {
          roi: {
            "/test/path": {
              type: "roi",
              data: [],
              pending: false,
              fulfilled: false,
              error:
                "Output file not found. Analysis may not have generated this file.",
              errorStatus: 404,
              roiUniqueList: [],
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimple filePath="/test/path" workspaceId={1} />,
      )

      // Should show error message
      expect(
        screen.getByText(
          "Output file not found. Analysis may not have generated this file.",
        ),
      ).toBeDefined()

      // Should NOT show retry button for not-found errors
      expect(screen.queryByRole("button")).toBeNull()
    })

    it("shows retry button for syncing errors", () => {
      const initialState = {
        displayData: {
          roi: {
            "/test/path": {
              type: "roi",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Syncing from cloud storage...",
              roiUniqueList: [],
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimple filePath="/test/path" workspaceId={1} />,
      )

      // Should show syncing message
      expect(screen.getByText("Syncing from cloud storage...")).toBeDefined()

      // Should show retry button (syncing is retryable)
      const retryButton = screen.getByRole("button")
      expect(retryButton).toBeDefined()
    })

    it("hides retry button for 404 errors based on errorStatus", () => {
      const initialState = {
        displayData: {
          roi: {
            "/test/path": {
              type: "roi",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Output file not found",
              errorStatus: 404,
              roiUniqueList: [],
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimple filePath="/test/path" workspaceId={1} />,
      )

      // Should show error message
      expect(screen.getByText("Output file not found")).toBeDefined()

      // Should NOT show retry button for 404 errors
      expect(screen.queryByRole("button")).toBeNull()
    })

    it("prevents click propagation when retry button is clicked", () => {
      const initialState = {
        displayData: {
          roi: {
            "/test/path": {
              type: "roi",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Error",
              roiUniqueList: [],
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      const mockOnClick = jest.fn()
      renderWithProviders(
        <RoiPlotSimple
          filePath="/test/path"
          workspaceId={1}
          onClick={mockOnClick}
        />,
      )

      // Click retry button - should not trigger parent onClick
      const retryButton = screen.getByRole("button")
      fireEvent.click(retryButton)

      // Parent onClick should NOT be called
      expect(mockOnClick).not.toHaveBeenCalled()
    })
  })

  describe("Loading state", () => {
    it("shows loading indicator when pending", () => {
      const initialState = {
        displayData: {
          roi: {
            "/test/path": {
              type: "roi",
              data: [],
              pending: true,
              fulfilled: false,
              error: null,
              roiUniqueList: [],
            },
          },
          loading: true,
          loadingStack: [true],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimple filePath="/test/path" workspaceId={1} />,
      )

      // Should show loading progress bar
      expect(screen.getByRole("progressbar")).toBeDefined()
    })
  })

  describe("No data state", () => {
    it("shows no data message when filePath is empty", () => {
      const initialState = {
        displayData: {
          roi: {},
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(<RoiPlotSimple filePath="" workspaceId={1} />)

      expect(screen.getByText("No data")).toBeDefined()
    })
  })

  describe("Success state", () => {
    it("renders plotly chart when data is available", () => {
      const initialState = {
        displayData: {
          roi: {
            "/test/path": {
              type: "roi",
              data: [
                [
                  [1, 2, 3],
                  [4, 5, 6],
                ],
              ],
              pending: false,
              fulfilled: true,
              error: null,
              roiUniqueList: ["1", "2", "3", "4", "5", "6"],
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimple filePath="/test/path" workspaceId={1} />,
      )

      // Should render plotly chart
      expect(screen.getByTestId("plotly-chart")).toBeDefined()
    })
  })

  describe("Initial data fetch", () => {
    it("fetches data on mount", () => {
      const initialState = {
        displayData: {
          roi: {},
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimple filePath="/test/path" workspaceId={1} />,
      )

      // Should dispatch getRoiData action on mount
      expect(mockGetRoiData).toHaveBeenCalledWith({
        path: "/test/path",
        workspaceId: 1,
        uniqueId: undefined,
      })
    })

    it("fetches data with uniqueId on mount when provided", () => {
      const initialState = {
        displayData: {
          roi: {},
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimple
          filePath="/test/path"
          workspaceId={1}
          uniqueId="workflow-456"
        />,
      )

      // Should dispatch getRoiData action with uniqueId on mount
      expect(mockGetRoiData).toHaveBeenCalledWith({
        path: "/test/path",
        workspaceId: 1,
        uniqueId: "workflow-456",
      })
    })
  })
})

describe("RoiPlotSimpleWithLoading Component", () => {
  let store: ReturnType<typeof mockStore>

  beforeEach(() => {
    mockGetRoiData.mockClear()
  })

  const renderWithProviders = (
    component: React.ReactElement,
    customStore?: ReturnType<typeof mockStore>,
  ) => {
    return render(<Provider store={customStore || store}>{component}</Provider>)
  }

  // The sync overlay is not covered here, or anywhere: it cannot render for
  // these states, because its gate wants a pending fetch with no roi data and
  // the roi selector answers an empty array rather than nothing.
  describe("Wrapper states", () => {
    it("renders the inner pending progressbar while a fetch is pending", () => {
      const initialState = {
        displayData: {
          roi: {
            "/test/path": {
              type: "roi",
              data: [],
              pending: true,
              fulfilled: false,
              error: null,
              roiUniqueList: [],
            },
          },
          loading: true,
          loadingStack: [true],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimpleWithLoading filePath="/test/path" workspaceId={1} />,
      )

      expect(screen.getByRole("progressbar")).toBeDefined()
    })

    it("renders the chart once the data is loaded", () => {
      const initialState = {
        displayData: {
          roi: {
            "/test/path": {
              type: "roi",
              data: [
                [
                  [1, 2, 3],
                  [4, 5, 6],
                ],
              ],
              pending: false,
              fulfilled: true,
              error: null,
              roiUniqueList: ["1", "2", "3", "4", "5", "6"],
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimpleWithLoading filePath="/test/path" workspaceId={1} />,
      )

      expect(screen.getByTestId("plotly-chart")).toBeDefined()
    })

    it("passes uniqueId to inner RoiPlotSimple component", () => {
      const initialState = {
        displayData: {
          roi: {},
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <RoiPlotSimpleWithLoading
          filePath="/test/path"
          workspaceId={1}
          uniqueId="test-workflow-id"
        />,
      )

      // Should dispatch with uniqueId
      expect(mockGetRoiData).toHaveBeenCalledWith({
        path: "/test/path",
        workspaceId: 1,
        uniqueId: "test-workflow-id",
      })
    })
  })
})
