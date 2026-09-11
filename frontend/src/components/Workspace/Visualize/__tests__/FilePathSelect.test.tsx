import { Provider } from "react-redux"

import configureStore from "redux-mock-store"

import { describe, it, expect, jest } from "@jest/globals"
import { fireEvent, render, screen } from "@testing-library/react"

import { FilePathSelect } from "components/Workspace/Visualize/FilePathSelect"
import {
  DATA_TYPE,
  DATA_TYPE_SET,
} from "store/slice/DisplayData/DisplayDataType"

const mockStore = configureStore([])

const ETA_A = "eta_gvgs8v0lta"
const ETA_B = "eta_abwqe9qgh1"

const output = (path: string, type: DATA_TYPE) => ({
  path,
  type,
  data_shape: [],
})

const etaOutputs = (nodeId: string) => ({
  status: "success",
  name: "eta",
  outputPaths: {
    mean: output(`/output/${nodeId}/mean.json`, DATA_TYPE_SET.TIME_SERIES),
    mean_heatmap: output(
      `/output/${nodeId}/mean_heatmap.json`,
      DATA_TYPE_SET.HEAT_MAP,
    ),
  },
})

const buildState = ({
  flowNodes = [] as { id: string; data: { label: string; type: string } }[],
  runResult = {} as Record<string, unknown>,
  inputNode = {} as Record<string, unknown>,
} = {}) => ({
  inputNode,
  flowElement: { flowNodes, flowEdges: [] },
  pipeline: {
    currentPipeline: { uid: "uid-1" },
    run: {
      uid: "uid-1",
      status: "Finished",
      runPostData: {},
      runResult,
    },
    runBtn: 1,
  },
})

const twoEtaNodesState = buildState({
  flowNodes: [
    { id: ETA_A, data: { label: "eta", type: "algorithm" } },
    { id: ETA_B, data: { label: "eta", type: "algorithm" } },
  ],
  runResult: {
    [ETA_A]: etaOutputs(ETA_A),
    [ETA_B]: etaOutputs(ETA_B),
    // every run carries this synthetic result; it has no node on the canvas
    post_process_0: {
      status: "success",
      name: "post_process",
      outputPaths: {},
    },
  },
})

const renderSelect = (
  state: ReturnType<typeof buildState>,
  props: Partial<Parameters<typeof FilePathSelect>[0]> = {},
) => {
  const onSelect = jest.fn()
  const view = render(
    <Provider store={mockStore(state)}>
      <FilePathSelect
        selectedNodeId={null}
        selectedFilePath={null}
        onSelect={onSelect}
        {...props}
      />
    </Provider>,
  )
  return { onSelect, view }
}

const openMenu = () => fireEvent.mouseDown(screen.getByRole("combobox"))

describe("FilePathSelect", () => {
  it("distinguishes two nodes sharing a label by their unique node id", () => {
    renderSelect(twoEtaNodesState)
    openMenu()

    expect(screen.getByText(ETA_A)).toBeInTheDocument()
    expect(screen.getByText(ETA_B)).toBeInTheDocument()
    expect(screen.getAllByText("mean")).toHaveLength(2)
    expect(screen.getAllByText("mean_heatmap")).toHaveLength(2)
  })

  it("omits the synthetic post_process result, which has no outputs", () => {
    renderSelect(twoEtaNodesState)
    openMenu()

    expect(screen.queryByText("post_process_0")).not.toBeInTheDocument()
    expect(screen.getAllByRole("option")).toHaveLength(6) // 2 headers + 4 outputs
  })

  it("labels the select for assistive technology", () => {
    renderSelect(twoEtaNodesState, { label: "Select Roi" })

    expect(
      screen.getByRole("combobox", { name: "Select Roi" }),
    ).toBeInTheDocument()
  })

  it("shows the node id of the selected output in the closed select", () => {
    const { onSelect, view } = renderSelect(twoEtaNodesState)
    openMenu()

    // second `mean`, i.e. the one belonging to ETA_B
    fireEvent.click(screen.getAllByRole("option", { name: "mean" })[1])

    expect(onSelect).toHaveBeenCalledWith(
      ETA_B,
      `/output/${ETA_B}/mean.json`,
      DATA_TYPE_SET.TIME_SERIES,
      "mean",
    )

    // the mock store does not reduce, so feed the selection back as props
    view.rerender(
      <Provider store={mockStore(twoEtaNodesState)}>
        <FilePathSelect
          selectedNodeId={ETA_B}
          selectedFilePath={`/output/${ETA_B}/mean.json`}
          onSelect={onSelect}
        />
      </Provider>,
    )

    expect(screen.getByRole("combobox")).toHaveTextContent(`mean (${ETA_B})`)
    expect(screen.getByRole("combobox")).toHaveAttribute(
      "title",
      `mean (${ETA_B})`,
    )
  })

  // input node ids (`input_<nanoid>`) carry no information, so input items keep
  // the bare file name that `09-visualize.spec.ts` selects them by
  it("keeps the file name as the label for input nodes", () => {
    const state = buildState({
      flowNodes: [
        {
          id: "input_kt62vwavq2",
          data: { label: "sample_mouse2p_image.tiff", type: "input" },
        },
      ],
      inputNode: {
        input_kt62vwavq2: {
          fileType: "csv",
          selectedFilePath: "/input/sample_mouse2p_image.tiff",
          param: {},
        },
      },
    })
    const { onSelect } = renderSelect(state, {
      selectedNodeId: "input_kt62vwavq2",
      selectedFilePath: "/input/sample_mouse2p_image.tiff",
    })

    expect(screen.getByRole("combobox")).toHaveTextContent(
      "sample_mouse2p_image.tiff",
    )

    openMenu()
    fireEvent.click(
      screen.getByRole("option", { name: "sample_mouse2p_image.tiff" }),
    )
    expect(onSelect).toHaveBeenCalledWith(
      "input_kt62vwavq2",
      "/input/sample_mouse2p_image.tiff",
      DATA_TYPE_SET.CSV,
      undefined,
    )
  })

  it("lists every file of a multi-file input node", () => {
    const state = buildState({
      flowNodes: [
        { id: "input_zz1", data: { label: "image.tiff", type: "input" } },
      ],
      inputNode: {
        input_zz1: {
          fileType: "image",
          selectedFilePath: ["/input/image1.tiff", "/input/image2.tiff"],
          param: {},
        },
      },
    })
    renderSelect(state)
    openMenu()

    expect(
      screen.getByRole("option", { name: "image1.tiff" }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole("option", { name: "image2.tiff" }),
    ).toBeInTheDocument()
  })

  it("omits nodes whose outputs are all filtered out by dataType", () => {
    renderSelect(twoEtaNodesState, { dataType: DATA_TYPE_SET.ROI })

    expect(screen.getByText("no data")).toBeInTheDocument()

    openMenu()
    expect(screen.queryAllByRole("option")).toHaveLength(0)
    expect(screen.queryByText("eta")).not.toBeInTheDocument()
  })

  it("keeps a node whose outputs partially match the dataType", () => {
    renderSelect(twoEtaNodesState, { dataType: DATA_TYPE_SET.HEAT_MAP })
    openMenu()

    expect(screen.getByText(ETA_A)).toBeInTheDocument()
    expect(screen.getByText(ETA_B)).toBeInTheDocument()
    expect(screen.getAllByText("mean_heatmap")).toHaveLength(2)
    expect(screen.queryByText("mean")).not.toBeInTheDocument()
  })

  it("renders an empty value when the selection is no longer in the store", () => {
    renderSelect(twoEtaNodesState, {
      selectedNodeId: "eta_fromanotherworkflow",
      selectedFilePath: "/output/stale/mean.json",
    })

    expect(screen.getByRole("combobox")).toHaveAttribute("title", "")
    expect(screen.getByRole("combobox")).not.toHaveTextContent("mean")
  })

  it("keeps the node name visible when the id has no name prefix", () => {
    const legacyId = "V1StGXR8_Z5jdHi6B"
    const state = buildState({
      flowNodes: [{ id: legacyId, data: { label: "eta", type: "algorithm" } }],
      runResult: { [legacyId]: etaOutputs(legacyId) },
    })
    renderSelect(state)
    openMenu()

    expect(screen.getByText(`eta (${legacyId})`)).toBeInTheDocument()
  })
})
