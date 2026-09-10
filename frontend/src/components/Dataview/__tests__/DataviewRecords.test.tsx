import React from "react"
import { Provider } from "react-redux"
import { MemoryRouter } from "react-router-dom"

import configureStore from "redux-mock-store"
import thunk from "redux-thunk"

import {
  describe,
  it,
  expect,
  jest,
  beforeAll,
  beforeEach,
} from "@jest/globals"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"

import { UserDTO } from "api/users/UsersApiDTO"
import DataviewRecords from "components/Dataview/DataviewRecords"
import { DataviewType } from "store/slice/Dataview/DataviewType"

const mockStore = configureStore([thunk])

// Mock notistack so snackbar calls can be asserted.
// (mock-prefixed name is required to reference it inside the jest.mock factory)
const mockEnqueueSnackbar = jest.fn()
jest.mock("notistack", () => ({
  enqueueSnackbar: (...args: unknown[]) => mockEnqueueSnackbar(...args),
}))

// Mock Dataview Actions
jest.mock("store/slice/Dataview/DataviewActions", () => ({
  getDataviewRecords: () => ({ type: "GET_DATAVIEW_RECORDS" }),
  getPublicDataviewRecords: () => ({ type: "GET_PUBLIC_DATAVIEW_RECORDS" }),
  postPublish: () => ({ type: "POST_PUBLISH" }),
  postPublishAll: () => ({ type: "POST_PUBLISH_ALL" }),
}))

// ThumbnailImage fetches through this; without it every PNG thumbnail falls
// into its error branch and no <img> is ever rendered.
const mockGetThumbnailBlobUrl = jest.fn<Promise<string>, unknown[]>()

jest.mock("api/visualizations/Outputs", () => ({
  getThumbnailBlobUrl: (...args: unknown[]) => mockGetThumbnailBlobUrl(...args),
}))

// Mock react-plotlyjs-ts to avoid d3-interpolate issues
jest.mock("react-plotlyjs-ts", () => ({
  __esModule: true,
  default: () => <div data-testid="plotly-chart">Plotly Chart</div>,
}))

jest.mock("components/Dataview/InputsView", () => {
  const MockInputsView = ({
    open,
    handleClose,
  }: {
    open: boolean
    handleClose: () => void
  }) => (
    <div data-testid="inputs-view" data-open={open}>
      <button onClick={handleClose}>Close</button>
    </div>
  )
  MockInputsView.displayName = "InputsView"
  return MockInputsView
})

jest.mock("components/Dataview/OutputsView", () => {
  const MockOutputsView = ({
    open,
    handleClose,
  }: {
    open: boolean
    handleClose: () => void
  }) => (
    <div data-testid="outputs-view" data-open={open}>
      <button onClick={handleClose}>Close</button>
    </div>
  )
  MockOutputsView.displayName = "OutputsView"
  return MockOutputsView
})

jest.mock("components/Dataview/WorkflowDetailsView", () => {
  const MockWorkflowDetailsView = ({
    open,
    onClose,
  }: {
    open: boolean
    onClose: () => void
  }) => (
    <div data-testid="workflow-details-view" data-open={open}>
      <button onClick={onClose}>Close</button>
    </div>
  )
  MockWorkflowDetailsView.displayName = "WorkflowDetailsView"
  return { WorkflowDetailsView: MockWorkflowDetailsView }
})

jest.mock("components/common/Loading", () => {
  const MockLoading = ({ loading }: { loading: boolean }) =>
    loading ? <div data-testid="loading">Loading...</div> : null
  MockLoading.displayName = "Loading"
  return MockLoading
})

jest.mock("components/common/PaginationCustom", () => {
  const MockPaginationCustom = ({
    handlePage,
    handleLimit,
  }: {
    handlePage: (e: React.ChangeEvent<unknown>, page: number) => void
    handleLimit: (e: React.ChangeEvent<HTMLSelectElement>) => void
  }) => (
    <div data-testid="pagination">
      <button onClick={(e) => handlePage(e as React.ChangeEvent<unknown>, 2)}>
        Page 2
      </button>
      <select onChange={handleLimit}>
        <option value="25">25</option>
        <option value="50">50</option>
      </select>
    </div>
  )
  MockPaginationCustom.displayName = "PaginationCustom"
  return MockPaginationCustom
})

jest.mock("components/Workspace/Visualize/Plot/ImagePlotSimple", () => ({
  ImagePlotSimple: () => <div data-testid="image-plot">Image Plot</div>,
  ImagePlotSimpleWithLoading: () => (
    <div data-testid="image-plot-with-loading">Image Plot With Loading</div>
  ),
}))

jest.mock("components/Workspace/Visualize/Plot/RoiPlotSimple", () => ({
  RoiPlotSimple: () => <div data-testid="roi-plot">ROI Plot</div>,
  RoiPlotSimpleWithLoading: () => (
    <div data-testid="roi-plot-with-loading">ROI Plot With Loading</div>
  ),
}))

jest.mock("components/common/SwitchCustom", () => {
  const MockSwitchCustom = ({
    checked,
    onChange,
  }: {
    checked: boolean
    onChange: () => void
  }) => (
    <input
      type="checkbox"
      checked={checked}
      onChange={onChange}
      data-testid="switch-custom"
    />
  )
  MockSwitchCustom.displayName = "SwitchCustom"
  return { __esModule: true, default: MockSwitchCustom }
})

jest.mock("components/common/ConfirmDialog", () => ({
  ConfirmDialog: ({
    open,
    onClose,
    onConfirm,
  }: {
    open: boolean
    onClose: () => void
    onConfirm: () => void
  }) =>
    open ? (
      <div data-testid="confirm-dialog">
        <button onClick={onClose} data-testid="cancel-button">
          Cancel
        </button>
        <button onClick={onConfirm} data-testid="confirm-button">
          Confirm
        </button>
      </div>
    ) : null,
}))

const mockDataviewRecord: DataviewType = {
  id: 1,
  uid: "test-uid-123",
  name: "Test Workflow",
  analyzed_at: "2023-01-01T00:00:00Z",
  created_at: "2023-01-01T00:00:00Z",
  updated_at: "2023-01-01T00:00:00Z",
  publish_status: 0,
  workspace: {
    id: 1,
    name: "Test Workspace",
  },
  owner: {
    name: "Test User",
  },
  thumbnails: {
    image_url: "/test/image.png",
    roi_url: "/test/roi.png",
  },
  attributes: {},
}

// Record with PNG thumbnails (new format)
const mockDataviewRecordWithPngThumbs: DataviewType = {
  ...mockDataviewRecord,
  id: 2,
  uid: "test-uid-png",
  thumbnails: {
    image_url: "/test/input_thumb.png",
    roi_url: "/test/roi_thumb.png",
  },
}

// Record with legacy TIFF/JSON thumbnails
const mockDataviewRecordWithLegacyThumbs: DataviewType = {
  ...mockDataviewRecord,
  id: 3,
  uid: "test-uid-legacy",
  thumbnails: {
    image_url: "/test/image.tiff",
    roi_url: "/test/roi.json",
  },
}

// Record whose run produced no thumbnails at all
const mockDataviewRecordWithoutThumbs: DataviewType = {
  ...mockDataviewRecord,
  id: 4,
  uid: "test-uid-no-thumb",
  thumbnails: {},
}

const mockUser: UserDTO = {
  id: 1,
  name: "Test User",
  email: "test@example.com",
  data_usage: 0,
}

const stateWithPrivateItems = (items: DataviewType[]) => ({
  dataview: {
    data: {
      private: { items, total: items.length, offset: 0, limit: 50 },
      public: { items: [], total: 0, offset: 0, limit: 50 },
    },
    loading: false,
    error: { private: null, public: null },
  },
  inputNode: {},
  flowElement: {
    flowNodes: [],
    flowEdges: [],
    flowPosition: { x: 0, y: 0 },
    elementCoord: {},
    loading: false,
  },
  flowElements: {
    nodeDict: {},
  },
  algorithmNode: {},
  pipeline: {
    nodeDict: {},
    run: { status: "SUCCESS", runResult: {} },
    runBtn: false,
    currentPipeline: null,
  },
  experiments: {
    status: "fulfilled",
    experimentList: {},
  },
})

// The DataGrid sizes its column render window from the scroller's clientWidth,
// which is 0 in jsdom, so without this only the first three columns exist.
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", {
    configurable: true,
    get: () => 1800,
  })
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get: () => 900,
  })
})

describe("DataviewRecords Component", () => {
  let store: ReturnType<typeof mockStore>

  beforeEach(() => {
    // Per test, not at the jest.fn(): CRA's jest preset sets resetMocks, which
    // drops an implementation given at declaration. Deriving the URL from the
    // requested thumbType keeps the two cards distinguishable - and a bare
    // jest.fn() resolves undefined, which still renders an <img>, just with an
    // empty src, so alt text alone holds even if no URL ever reaches it.
    mockGetThumbnailBlobUrl.mockImplementation((...args) =>
      Promise.resolve(`blob:${String(args[2])}`),
    )

    const initialState = {
      dataview: {
        data: {
          private: {
            items: [mockDataviewRecord],
            total: 1,
            offset: 0,
            limit: 50,
          },
          public: {
            items: [mockDataviewRecord],
            total: 1,
            offset: 0,
            limit: 50,
          },
        },
        loading: false,
        error: { private: null, public: null },
      },
      inputNode: {},
      flowElement: {
        flowNodes: [],
        flowEdges: [],
        flowPosition: { x: 0, y: 0 },
        elementCoord: {},
        loading: false,
      },
      flowElements: {
        nodeDict: {},
      },
      algorithmNode: {},
      pipeline: {
        nodeDict: {},
        run: { status: "SUCCESS", runResult: {} },
        runBtn: false,
        currentPipeline: null,
      },
      experiments: {
        status: "fulfilled",
        experimentList: {},
      },
    }
    store = mockStore(initialState)
    store.dispatch = jest.fn()
  })

  const renderWithProviders = (component: React.ReactElement) => {
    return render(
      <Provider store={store}>
        <MemoryRouter
          future={{
            v7_startTransition: true,
            v7_relativeSplatPath: true,
          }}
        >
          {component}
        </MemoryRouter>
      </Provider>,
    )
  }

  describe("Private view (with user)", () => {
    it("renders DataGrid with workflow records", () => {
      renderWithProviders(<DataviewRecords user={mockUser} />)

      expect(screen.getByRole("grid")).toBeDefined()
      expect(screen.getByText("test-uid-123")).toBeDefined()
      expect(screen.getByText("Test Workflow")).toBeDefined()
    })

    it("shows publish/unpublish buttons when not readonly", () => {
      renderWithProviders(<DataviewRecords user={mockUser} readonly={false} />)

      const publishButtons = screen.getAllByLabelText(/bulk/i)
      expect(publishButtons).toHaveLength(2)
    })

    it("does not show publish buttons when readonly", () => {
      renderWithProviders(<DataviewRecords user={mockUser} readonly={true} />)

      const publishButtons = screen.queryAllByRole("button", { name: /bulk/i })
      expect(publishButtons).toHaveLength(0)
    })

    it("shows checkboxes when not readonly", () => {
      renderWithProviders(<DataviewRecords user={mockUser} readonly={false} />)

      const checkboxes = screen.getAllByRole("checkbox")
      expect(checkboxes.length).toBeGreaterThan(0)
    })
  })

  describe("Public view (no user)", () => {
    it("renders DataGrid with public workflow records", () => {
      renderWithProviders(<DataviewRecords />)

      expect(screen.getByRole("grid")).toBeDefined()
      expect(screen.getByText("test-uid-123")).toBeDefined()
      expect(screen.getByText("Test Workflow")).toBeDefined()
    })

    it("does not show publish buttons in public view", () => {
      renderWithProviders(<DataviewRecords />)

      const publishButtons = screen.queryAllByRole("button", { name: /bulk/i })
      expect(publishButtons).toHaveLength(0)
    })

    it("shows owner column in public view", () => {
      renderWithProviders(<DataviewRecords />)

      expect(screen.getByText("Test User")).toBeDefined()
    })
  })

  describe("Loading state", () => {
    it("shows loading component when loading is true", () => {
      const loadingState = {
        dataview: {
          data: {
            private: {
              items: [mockDataviewRecord],
              total: 1,
              offset: 0,
              limit: 50,
            },
            public: {
              items: [mockDataviewRecord],
              total: 1,
              offset: 0,
              limit: 50,
            },
          },
          loading: true,
          error: { private: null, public: null },
        },
        inputNode: {},
        flowElement: {
          flowNodes: [],
          flowEdges: [],
          flowPosition: { x: 0, y: 0 },
          elementCoord: {},
          loading: false,
        },
        flowElements: {
          nodeDict: {},
        },
        algorithmNode: {},
        pipeline: {
          nodeDict: {},
          run: { status: "SUCCESS", runResult: {} },
          runBtn: false,
          currentPipeline: null,
        },
        experiments: {
          status: "fulfilled",
          experimentList: {},
        },
      }
      store = mockStore(loadingState)

      renderWithProviders(<DataviewRecords user={mockUser} />)

      expect(screen.getByTestId("loading")).toBeDefined()
    })
  })

  describe("Workspace filtering", () => {
    it("shows workspace chip when workspaceId is provided and workspace name is available", () => {
      const stateWithWorkspace = {
        dataview: {
          data: {
            private: {
              items: [mockDataviewRecord],
              total: 1,
              offset: 0,
              limit: 50,
              header: {
                workspace_name: "Test Workspace",
              },
            },
            public: {
              items: [mockDataviewRecord],
              total: 1,
              offset: 0,
              limit: 50,
            },
          },
          loading: false,
          error: { private: null, public: null },
        },
        inputNode: {},
        flowElement: {
          flowNodes: [],
          flowEdges: [],
          flowPosition: { x: 0, y: 0 },
          elementCoord: {},
          loading: false,
        },
        flowElements: {
          nodeDict: {},
        },
        algorithmNode: {},
        pipeline: {
          nodeDict: {},
          run: { status: "SUCCESS", runResult: {} },
          runBtn: false,
          currentPipeline: null,
        },
        experiments: {
          status: "fulfilled",
          experimentList: {},
        },
      }
      store = mockStore(stateWithWorkspace)

      renderWithProviders(<DataviewRecords user={mockUser} workspaceId="1" />)

      // Scoped to the chip: the workspace column renders the same text.
      expect(
        screen.getByText("Test Workspace", { selector: ".MuiChip-label" }),
      ).toBeDefined()
    })
  })

  describe("Pagination", () => {
    it("shows pagination when there are records", () => {
      renderWithProviders(<DataviewRecords user={mockUser} />)

      expect(screen.getByTestId("pagination")).toBeDefined()
    })

    it("handles page change", () => {
      renderWithProviders(<DataviewRecords user={mockUser} />)

      const pageButton = screen.getByText("Page 2")
      fireEvent.click(pageButton)

      // Since we're testing with mocked store, we can verify dispatch was called
      expect(store.dispatch).toHaveBeenCalled()
    })
  })

  describe("Row click handling", () => {
    it("calls handleRowClick with the clicked row", () => {
      const mockHandleRowClick = jest.fn()
      renderWithProviders(
        <DataviewRecords user={mockUser} handleRowClick={mockHandleRowClick} />,
      )

      fireEvent.click(screen.getByText("Test Workflow"))

      expect(mockHandleRowClick).toHaveBeenCalledTimes(1)
      expect(mockHandleRowClick.mock.calls[0][0]).toEqual(
        expect.objectContaining({
          id: mockDataviewRecord.id,
          row: expect.objectContaining({ uid: "test-uid-123" }),
        }),
      )
    })

    it("does not break when no handleRowClick is provided", () => {
      renderWithProviders(<DataviewRecords user={mockUser} />)

      expect(() =>
        fireEvent.click(screen.getByText("Test Workflow")),
      ).not.toThrow()
    })
  })

  describe("Error state", () => {
    it("shows error alert when private error exists", () => {
      const errorState = {
        dataview: {
          data: {
            private: {
              items: [mockDataviewRecord],
              total: 1,
              offset: 0,
              limit: 50,
            },
            public: {
              items: [mockDataviewRecord],
              total: 1,
              offset: 0,
              limit: 50,
            },
          },
          loading: false,
          error: { private: "Failed to load dataview records", public: null },
        },
        inputNode: {},
        flowElement: {
          flowNodes: [],
          flowEdges: [],
          flowPosition: { x: 0, y: 0 },
          elementCoord: {},
          loading: false,
        },
        flowElements: {
          nodeDict: {},
        },
        algorithmNode: {},
        pipeline: {
          nodeDict: {},
          run: { status: "SUCCESS", runResult: {} },
          runBtn: false,
          currentPipeline: null,
        },
        experiments: {
          status: "fulfilled",
          experimentList: {},
        },
      }
      store = mockStore(errorState)

      renderWithProviders(<DataviewRecords user={mockUser} />)

      expect(screen.getByRole("alert")).toBeDefined()
      expect(screen.getByText("Failed to load dataview records")).toBeDefined()
    })

    it("shows error alert when public error exists", () => {
      const errorState = {
        dataview: {
          data: {
            private: {
              items: [mockDataviewRecord],
              total: 1,
              offset: 0,
              limit: 50,
            },
            public: {
              items: [mockDataviewRecord],
              total: 1,
              offset: 0,
              limit: 50,
            },
          },
          loading: false,
          error: { private: null, public: "Failed to load public records" },
        },
        inputNode: {},
        flowElement: {
          flowNodes: [],
          flowEdges: [],
          flowPosition: { x: 0, y: 0 },
          elementCoord: {},
          loading: false,
        },
        flowElements: {
          nodeDict: {},
        },
        algorithmNode: {},
        pipeline: {
          nodeDict: {},
          run: { status: "SUCCESS", runResult: {} },
          runBtn: false,
          currentPipeline: null,
        },
        experiments: {
          status: "fulfilled",
          experimentList: {},
        },
      }
      store = mockStore(errorState)

      // Public view (no user)
      renderWithProviders(<DataviewRecords />)

      expect(screen.getByRole("alert")).toBeDefined()
      expect(screen.getByText("Failed to load public records")).toBeDefined()
    })

    it("does not show error alert when loading", () => {
      const loadingWithErrorState = {
        dataview: {
          data: {
            private: {
              items: [mockDataviewRecord],
              total: 1,
              offset: 0,
              limit: 50,
            },
            public: {
              items: [mockDataviewRecord],
              total: 1,
              offset: 0,
              limit: 50,
            },
          },
          loading: true,
          error: { private: "Some error", public: null },
        },
        inputNode: {},
        flowElement: {
          flowNodes: [],
          flowEdges: [],
          flowPosition: { x: 0, y: 0 },
          elementCoord: {},
          loading: false,
        },
        flowElements: {
          nodeDict: {},
        },
        algorithmNode: {},
        pipeline: {
          nodeDict: {},
          run: { status: "SUCCESS", runResult: {} },
          runBtn: false,
          currentPipeline: null,
        },
        experiments: {
          status: "fulfilled",
          experimentList: {},
        },
      }
      store = mockStore(loadingWithErrorState)

      renderWithProviders(<DataviewRecords user={mockUser} />)

      // Error alert should not be shown while loading
      expect(screen.queryByRole("alert")).toBeNull()
    })
  })

  describe("Thumbnail rendering", () => {
    it("renders PNG thumbnails as img tags for input_thumb.png pattern", async () => {
      store = mockStore(
        stateWithPrivateItems([mockDataviewRecordWithPngThumbs]),
      )

      renderWithProviders(<DataviewRecords user={mockUser} />)

      // The fetched URL has to reach the src, and each card has to ask for its
      // own thumbType - an <img> with an empty src is a broken thumbnail, and
      // both cards requesting "input" would show the same picture twice
      expect(await screen.findByAltText("Input thumbnail")).toHaveAttribute(
        "src",
        "blob:input",
      )
      expect(await screen.findByAltText("ROI thumbnail")).toHaveAttribute(
        "src",
        "blob:roi",
      )

      // Should not use the WithLoading plot components for PNG thumbnails
      expect(screen.queryByTestId("image-plot-with-loading")).toBeNull()
      expect(screen.queryByTestId("roi-plot-with-loading")).toBeNull()
    })

    it("does not render PNG img tags for legacy TIFF/JSON thumbnails", () => {
      store = mockStore(
        stateWithPrivateItems([mockDataviewRecordWithLegacyThumbs]),
      )

      renderWithProviders(<DataviewRecords user={mockUser} />)

      // The legacy path routes to the on-demand-sync plot components instead
      expect(screen.getByTestId("image-plot-with-loading")).toBeDefined()
      expect(screen.getByTestId("roi-plot-with-loading")).toBeDefined()
      expect(screen.queryByAltText("Input thumbnail")).toBeNull()
      expect(screen.queryByAltText("ROI thumbnail")).toBeNull()
    })

    // A run with no thumbnails must still give the user a way into the record.
    it("falls back to a clickable placeholder icon when a record has no thumbnails", () => {
      store = mockStore(
        stateWithPrivateItems([mockDataviewRecordWithoutThumbs]),
      )

      renderWithProviders(<DataviewRecords user={mockUser} />)

      const placeholders = screen.getAllByTestId("ImageIcon")
      expect(placeholders).toHaveLength(2)
      expect(screen.queryByAltText("Input thumbnail")).toBeNull()
      expect(screen.queryByTestId("image-plot-with-loading")).toBeNull()
      expect(screen.queryByTestId("roi-plot-with-loading")).toBeNull()

      fireEvent.click(placeholders[0])
      expect(screen.getByTestId("inputs-view")).toHaveAttribute(
        "data-open",
        "true",
      )
    })
  })

  describe("Publish error handling", () => {
    it("shows an error snackbar with the backend detail when bulk publish is rejected", async () => {
      mockEnqueueSnackbar.mockClear()
      // dispatch(...).unwrap() rejects with a FastAPI-shaped error body
      store.dispatch = jest.fn().mockReturnValue({
        unwrap: () =>
          Promise.reject({
            response: {
              data: {
                detail:
                  "Some experiments cannot be published:\n- exp: corrupted",
              },
            },
          }),
      }) as unknown as typeof store.dispatch

      renderWithProviders(<DataviewRecords user={mockUser} readonly={false} />)

      // Select records (header select-all checkbox is always rendered)
      fireEvent.click(screen.getAllByRole("checkbox")[0])

      // Open the bulk-publish confirm dialog (label is on the Tooltip wrapper;
      // click the inner button), then confirm.
      const bulkPublish = screen.getAllByLabelText(/bulk publish/i)[0]
      fireEvent.click(bulkPublish.querySelector("button") ?? bulkPublish)
      fireEvent.click(screen.getByTestId("confirm-button"))

      await waitFor(() =>
        expect(mockEnqueueSnackbar).toHaveBeenCalledWith(
          "Some experiments cannot be published:\n- exp: corrupted",
          expect.objectContaining({
            variant: "error",
            // multi-line backend detail must keep its line breaks
            style: { whiteSpace: "pre-line" },
          }),
        ),
      )
    })

    it("uses an action-aware fallback message when bulk unpublish is rejected without detail", async () => {
      mockEnqueueSnackbar.mockClear()
      // Reject without a `detail` -> the fallback message is used
      store.dispatch = jest.fn().mockReturnValue({
        unwrap: () => Promise.reject({ message: "network error" }),
      }) as unknown as typeof store.dispatch

      renderWithProviders(<DataviewRecords user={mockUser} readonly={false} />)

      fireEvent.click(screen.getAllByRole("checkbox")[0])

      const bulkUnpublish = screen.getAllByLabelText(/bulk unpublish/i)[0]
      fireEvent.click(bulkUnpublish.querySelector("button") ?? bulkUnpublish)
      fireEvent.click(screen.getByTestId("confirm-button"))

      await waitFor(() =>
        expect(mockEnqueueSnackbar).toHaveBeenCalledWith(
          "Failed to unpublish experiments",
          expect.objectContaining({ variant: "error" }),
        ),
      )
    })
  })
})
