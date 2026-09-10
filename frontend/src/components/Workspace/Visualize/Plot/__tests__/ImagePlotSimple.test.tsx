import React from "react"
import { Provider } from "react-redux"

import configureStore from "redux-mock-store"
import thunk from "redux-thunk"

import { describe, it, expect, jest, beforeEach } from "@jest/globals"
import { render, screen, fireEvent } from "@testing-library/react"

import {
  ImagePlotSimple,
  ImagePlotSimpleWithLoading,
} from "components/Workspace/Visualize/Plot/ImagePlotSimple"

const mockStore = configureStore([thunk])

// Mock react-plotlyjs-ts to avoid d3-interpolate issues
jest.mock("react-plotlyjs-ts", () => ({
  __esModule: true,
  default: () => <div data-testid="plotly-chart">Plotly Chart</div>,
}))

// Mock getImageData action - return a thunk-like function
const mockGetImageData = jest.fn()
jest.mock("store/slice/DisplayData/DisplayDataActions", () => ({
  getImageData: (params: {
    path: string
    workspaceId: number
    uniqueId?: string
    startIndex: number
    endIndex: number
  }) => {
    mockGetImageData(params)
    // Return a thunk function
    return () => Promise.resolve()
  },
  SYNC_IN_PROGRESS_MESSAGE: "Syncing from cloud storage...",
}))

describe("ImagePlotSimple Component", () => {
  let store: ReturnType<typeof mockStore>

  beforeEach(() => {
    mockGetImageData.mockClear()
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
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Image not synced",
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimple filePath="/test/image.tiff" workspaceId={1} />,
      )

      // Should show error message
      expect(screen.getByText("Image not synced")).toBeDefined()

      // Should show retry button
      const retryButton = screen.getByRole("button")
      expect(retryButton).toBeDefined()
    })

    it("calls getImageData when retry button is clicked", () => {
      const initialState = {
        displayData: {
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Image unavailable",
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimple filePath="/test/image.tiff" workspaceId={1} />,
      )

      // Click retry button
      const retryButton = screen.getByRole("button")
      fireEvent.click(retryButton)

      // Should dispatch getImageData action with correct params
      expect(mockGetImageData).toHaveBeenCalledWith({
        path: "/test/image.tiff",
        workspaceId: 1,
        uniqueId: undefined,
        startIndex: 1,
        endIndex: 1,
      })
    })

    it("includes uniqueId when provided", () => {
      const initialState = {
        displayData: {
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Image unavailable",
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimple
          filePath="/test/image.tiff"
          workspaceId={1}
          uniqueId="workflow-123"
        />,
      )

      // Click retry button
      const retryButton = screen.getByRole("button")
      fireEvent.click(retryButton)

      // Should dispatch getImageData action with uniqueId
      expect(mockGetImageData).toHaveBeenCalledWith({
        path: "/test/image.tiff",
        workspaceId: 1,
        uniqueId: "workflow-123",
        startIndex: 1,
        endIndex: 1,
      })
    })

    it("prevents click propagation when retry button is clicked", () => {
      const initialState = {
        displayData: {
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Error",
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      const mockOnClick = jest.fn()
      renderWithProviders(
        <ImagePlotSimple
          filePath="/test/image.tiff"
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

    it("hides retry button when error contains 'not found'", () => {
      const initialState = {
        displayData: {
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Input image file not found: test.tif",
              errorStatus: 404,
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimple filePath="/test/image.tiff" workspaceId={1} />,
      )

      // Should show error message
      expect(
        screen.getByText("Input image file not found: test.tif"),
      ).toBeDefined()

      // Should NOT show retry button for not-found errors
      expect(screen.queryByRole("button")).toBeNull()
    })

    it("shows retry button for syncing errors", () => {
      const initialState = {
        displayData: {
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Syncing from cloud storage...",
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimple filePath="/test/image.tiff" workspaceId={1} />,
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
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [],
              pending: false,
              fulfilled: false,
              error: "Output file not found",
              errorStatus: 404,
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimple filePath="/test/image.tiff" workspaceId={1} />,
      )

      // Should show error message
      expect(screen.getByText("Output file not found")).toBeDefined()

      // Should NOT show retry button for 404 errors
      expect(screen.queryByRole("button")).toBeNull()
    })

    it("displays custom error message from server response", () => {
      const initialState = {
        displayData: {
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [],
              pending: false,
              fulfilled: false,
              error: "File not found in S3 bucket",
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimple filePath="/test/image.tiff" workspaceId={1} />,
      )

      // Should show the specific error message
      expect(screen.getByText("File not found in S3 bucket")).toBeDefined()
    })
  })

  describe("Loading state", () => {
    it("shows loading indicator when pending", () => {
      const initialState = {
        displayData: {
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [],
              pending: true,
              fulfilled: false,
              error: null,
            },
          },
          loading: true,
          loadingStack: [true],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimple filePath="/test/image.tiff" workspaceId={1} />,
      )

      // Should show loading progress bar
      expect(screen.getByRole("progressbar")).toBeDefined()
    })
  })

  describe("No data state", () => {
    it("shows no data message when filePath is empty", () => {
      const initialState = {
        displayData: {
          image: {},
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(<ImagePlotSimple filePath="" workspaceId={1} />)

      expect(screen.getByText("No data")).toBeDefined()
    })
  })

  describe("Success state", () => {
    it("renders plotly chart when data is available", () => {
      const initialState = {
        displayData: {
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [
                [
                  [1, 2, 3],
                  [4, 5, 6],
                ],
              ],
              pending: false,
              fulfilled: true,
              error: null,
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimple filePath="/test/image.tiff" workspaceId={1} />,
      )

      // Should render plotly chart
      expect(screen.getByTestId("plotly-chart")).toBeDefined()
    })
  })

  describe("Initial data fetch", () => {
    it("fetches data on mount when not initialized", () => {
      const initialState = {
        displayData: {
          image: {},
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimple filePath="/test/image.tiff" workspaceId={1} />,
      )

      // Should dispatch getImageData action on mount
      expect(mockGetImageData).toHaveBeenCalledWith({
        path: "/test/image.tiff",
        workspaceId: 1,
        uniqueId: undefined,
        startIndex: 1,
        endIndex: 1,
      })
    })

    it("fetches data with uniqueId on mount when provided", () => {
      const initialState = {
        displayData: {
          image: {},
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimple
          filePath="/test/image.tiff"
          workspaceId={1}
          uniqueId="workflow-456"
        />,
      )

      // Should dispatch getImageData action with uniqueId on mount
      expect(mockGetImageData).toHaveBeenCalledWith({
        path: "/test/image.tiff",
        workspaceId: 1,
        uniqueId: "workflow-456",
        startIndex: 1,
        endIndex: 1,
      })
    })

    it("does not fetch data when already initialized", () => {
      const initialState = {
        displayData: {
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [[[1, 2, 3]]],
              pending: false,
              fulfilled: true,
              error: null,
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)
      mockGetImageData.mockClear()

      renderWithProviders(
        <ImagePlotSimple filePath="/test/image.tiff" workspaceId={1} />,
      )

      // Should NOT dispatch getImageData action when already initialized
      expect(mockGetImageData).not.toHaveBeenCalled()
    })
  })
})

describe("ImagePlotSimpleWithLoading Component", () => {
  let store: ReturnType<typeof mockStore>

  beforeEach(() => {
    mockGetImageData.mockClear()
  })

  const renderWithProviders = (
    component: React.ReactElement,
    customStore?: ReturnType<typeof mockStore>,
  ) => {
    return render(<Provider store={customStore || store}>{component}</Provider>)
  }

  // The sync overlay is not covered here, or anywhere: it cannot render for
  // these states, because its gate wants a pending fetch on data that is not
  // initialized and the pending selector is false until it is.
  describe("Wrapper states", () => {
    it("renders the inner pending progressbar while a fetch is pending", () => {
      const initialState = {
        displayData: {
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [],
              pending: true,
              fulfilled: false,
              error: null,
            },
          },
          loading: true,
          loadingStack: [true],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimpleWithLoading
          filePath="/test/image.tiff"
          workspaceId={1}
        />,
      )

      expect(screen.getByRole("progressbar")).toBeDefined()
    })

    it("renders the chart once the data is initialized", () => {
      const initialState = {
        displayData: {
          image: {
            "/test/image.tiff": {
              type: "image",
              data: [
                [
                  [1, 2, 3],
                  [4, 5, 6],
                ],
              ],
              pending: false,
              fulfilled: true,
              error: null,
            },
          },
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimpleWithLoading
          filePath="/test/image.tiff"
          workspaceId={1}
        />,
      )

      expect(screen.getByTestId("plotly-chart")).toBeDefined()
    })

    it("passes uniqueId to inner ImagePlotSimple component", () => {
      const initialState = {
        displayData: {
          image: {},
          loading: false,
          loadingStack: [],
        },
      }
      store = mockStore(initialState)

      renderWithProviders(
        <ImagePlotSimpleWithLoading
          filePath="/test/image.tiff"
          workspaceId={1}
          uniqueId="test-workflow-id"
        />,
      )

      // Should dispatch with uniqueId
      expect(mockGetImageData).toHaveBeenCalledWith({
        path: "/test/image.tiff",
        workspaceId: 1,
        uniqueId: "test-workflow-id",
        startIndex: 1,
        endIndex: 1,
      })
    })
  })
})
