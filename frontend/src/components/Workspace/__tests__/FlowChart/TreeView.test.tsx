/* eslint-disable no-undef */
import React from "react"
import { Provider } from "react-redux"

import configureStore from "redux-mock-store"
import thunk from "redux-thunk"

import { describe, it, beforeEach } from "@jest/globals"
import { render, screen } from "@testing-library/react"
import { userEvent } from "@testing-library/user-event"

import { mockStoreData } from "components/Workspace/__tests__/FlowChart/mockStoreData.json"
import { AlgorithmTreeView } from "components/Workspace/FlowChart/TreeView"
import { getAlgoList } from "store/slice/AlgorithmList/AlgorithmListActions"
import { addAlgorithmNode } from "store/slice/FlowElement/FlowElementActions"

// The mount effect dispatches the real thunk, so the API it calls is stubbed
// rather than the action creator: comparing thunks by identity cannot work, as
// createAsyncThunk returns a fresh function on every call.
jest.mock("api/algolist/AlgoList", () => ({
  getAlgoListApi: jest.fn(),
}))

jest.mock("react-dnd", () => ({
  ...jest.requireActual("react-dnd"),
  useDrag: () => [{ isDragging: false }, null],
}))

const mockStore = configureStore([thunk])

describe("AlgorithmTreeView", () => {
  let store: ReturnType<typeof mockStore>

  beforeEach(() => {
    store = mockStore(mockStoreData)
    store.dispatch = jest.fn()
  })

  it("renders the AlgorithmTreeView component", async () => {
    render(
      <Provider store={store}>
        <AlgorithmTreeView />
      </Provider>,
    )
    expect(screen.getByText("Data")).toBeInTheDocument()
    expect(screen.getByText("Algorithm")).toBeInTheDocument()
  })

  it("renders the AlgorithmTree Data TreeItems", async () => {
    render(
      <Provider store={store}>
        <AlgorithmTreeView />
      </Provider>,
    )

    // Click on the "Data" TreeItem to expand it
    const dataTreeLabel = screen.getByText("Data")
    await userEvent.click(dataTreeLabel)

    // Check if all the TreeItems are rendered
    expect(screen.getByText("image")).toBeInTheDocument()
    expect(screen.getByText("csv")).toBeInTheDocument()
    expect(screen.getByText("hdf5")).toBeInTheDocument()
    expect(screen.getByText("fluo")).toBeInTheDocument()
    expect(screen.getByText("behavior")).toBeInTheDocument()
    expect(screen.getByText("matlab")).toBeInTheDocument()
    expect(screen.getByText("microscope")).toBeInTheDocument()
  })

  it("renders the AlgorithmTree Algorithm TreeItems", async () => {
    render(
      <Provider store={store}>
        <AlgorithmTreeView />
      </Provider>,
    )

    // Click on the "Data" TreeItem to expand it
    const algorithmTreeLabel = screen.getByText("Algorithm")
    await userEvent.click(algorithmTreeLabel)

    // Check if all the TreeItems are rendered
    expect(screen.getByText("caiman")).toBeInTheDocument()
    expect(screen.getByText("suite2p")).toBeInTheDocument()
    expect(screen.getByText("lccd")).toBeInTheDocument()
    expect(screen.getByText("optinist")).toBeInTheDocument()
  })

  // These two use the store's real dispatch rather than the jest.fn() the other
  // tests install, so the thunk reaches the middleware and its lifecycle action
  // lands in getActions(). Asserting on the action type is the only workable
  // check: `getAlgoList()` builds a new function on every call, so comparing what
  // was dispatched against a second call can never match.
  describe("fetching the algorithm list on mount", () => {
    const renderWith = (algorithmList: Record<string, unknown>) => {
      const realStore = mockStore({ ...mockStoreData, algorithmList })
      render(
        <Provider store={realStore}>
          <AlgorithmTreeView />
        </Provider>,
      )
      return realStore
    }

    const fetchActions = (realStore: ReturnType<typeof mockStore>) =>
      realStore
        .getActions()
        .filter((action: { type: string }) =>
          action.type.startsWith(`${getAlgoList.typePrefix}/`),
        )

    it("fetches when the cached list is not the latest", () => {
      const realStore = renderWith({ tree: {}, isLatest: false })

      expect(fetchActions(realStore)).not.toHaveLength(0)
    })

    it("does not fetch again when the cached list is already the latest", () => {
      // Without the isLatest guard every mount of the drawer refetches the whole
      // algorithm list
      const realStore = renderWith({ tree: {}, isLatest: true })

      expect(fetchActions(realStore)).toHaveLength(0)
    })
  })

  it("dispatches the correct action when the Image node add button is clicked", async () => {
    render(
      <Provider store={store}>
        <AlgorithmTreeView />
      </Provider>,
    )

    const dataTreeLabel = screen.getByText("Data")
    await userEvent.click(dataTreeLabel)

    // Click on the "image" TreeItem to select it
    const addButton = screen.getAllByLabelText("add")[0]
    await userEvent.click(addButton)

    // Verify that the action was dispatched with the expected payload
    expect(store.dispatch).toHaveBeenCalledWith(
      expect.objectContaining({
        type: "flowElement/addInputNode",
        payload: {
          node: expect.any(Object),
          fileType: "image",
        },
      }),
    )
  })

  it("dispatches the correct action when the algorithm add button is clicked", async () => {
    // Spy on the `addAlgorithmNode` action for this test
    // eslint-disable-next-line @typescript-eslint/no-unused-vars
    const addAlgorithmNodeMock = jest
      .spyOn(
        // eslint-disable-next-line @typescript-eslint/no-var-requires
        require("store/slice/FlowElement/FlowElementActions"),
        "addAlgorithmNode",
      )
      .mockImplementation(jest.fn())

    render(
      <Provider store={store}>
        <AlgorithmTreeView />
      </Provider>,
    )

    // Click the "Algorithm" label to expand the node
    const algorithmTreeLabel = screen.getByText("Algorithm")
    await userEvent.click(algorithmTreeLabel)

    // Ensure the "caiman" node exists and click it
    const caimanTreeLabel = screen.getByText("caiman")
    await userEvent.click(caimanTreeLabel)

    // Click the add button for the "caiman" node (using a more specific selector if needed)
    const addButton = screen.getAllByTestId("AddIcon")[0]
    await userEvent.click(addButton)

    // Verify that the addAlgorithmNode action was dispatched
    expect(addAlgorithmNode).toHaveBeenCalledWith({
      node: {
        data: { label: "caiman_mc", type: "algorithm" },
        id: expect.any(String), // Use `expect.any(String)` if the id is dynamically generated,
        position: undefined,
        type: "AlgorithmNode",
      },
      name: "caiman_mc",
      functionPath: "caiman/caiman_mc",
      runAlready: true,
    })
  })
})
