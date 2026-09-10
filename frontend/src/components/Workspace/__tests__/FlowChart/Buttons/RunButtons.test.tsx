import React, { useState } from "react"
import { Provider } from "react-redux"

import { SnackbarProvider } from "notistack"
import configureStore from "redux-mock-store"

import {
  describe,
  it,
  beforeEach,
  jest,
  expect,
  afterEach,
} from "@jest/globals"
import "@testing-library/jest-dom"
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { userEvent } from "@testing-library/user-event"

import * as StorageAlertsApi from "api/storage/StorageAlerts"
import {
  RunButtons,
  RUN_REQUEST_DEBOUNCE_MS,
} from "components/Workspace/FlowChart/Buttons/RunButtons"
import {
  RUN_BTN_LABELS,
  RUN_BTN_OPTIONS,
  RUN_BTN_TYPE,
} from "store/slice/Pipeline/PipelineType"

// Mock the storage alert API
jest.mock("api/storage/StorageAlerts")
const mockGetMyStorageAlertApi =
  StorageAlertsApi.getMyStorageAlertApi as jest.MockedFunction<
    typeof StorageAlertsApi.getMyStorageAlertApi
  >

const mockStore = configureStore([])

// `runBtn` belongs under `pipeline`, which is where selectPipelineRunBtn reads
// it. At the root it reads back undefined, and every RUN click then takes the
// RUN_ALREADY branch regardless of what the test asked for.
const storeWith = (runBtn: RUN_BTN_TYPE) =>
  mockStore({
    pipeline: {
      run: {
        status: "StartUninitialized",
      },
      runBtn,
    },
    currentPipeline: {
      uid: "test-uid",
    },
  })

// The app's own label rather than the MUI icon's testid, which changes with the
// selected option and belongs to the icon library either way
const runButtonFor = (runBtn: RUN_BTN_TYPE) =>
  screen.getByRole("button", { name: RUN_BTN_LABELS[runBtn] })

const createMockStorageAlert = (
  overrides: Partial<StorageAlertsApi.StorageAlert> = {},
): StorageAlertsApi.StorageAlert => ({
  user_name: "Test User",
  user_email: "test@example.com",
  alert_level: "critical",
  storage_usage_bytes: 5000000000,
  storage_quota_bytes: 5368709120,
  storage_usage_percent: 93,
  timestamp: new Date().toISOString(),
  message: "Storage usage high",
  subscription_plan: "free",
  ...overrides,
})

describe("RunButtons component", () => {
  let store: ReturnType<typeof mockStore>

  const mockProps = {
    uid: "test-uid",
    runDisabled: false,
    filePathIsUndefined: false,
    algorithmNodeNotExist: false,
    handleCancelPipeline: jest.fn(() => Promise.resolve()),
    handleRunPipeline: jest.fn((_name: string) => undefined),
    handleRunPipelineByUid: jest.fn(() => undefined),
  }

  beforeEach(() => {
    jest.clearAllMocks()
    mockGetMyStorageAlertApi.mockResolvedValue({
      has_alert: false,
      alert: null,
    })

    store = storeWith(RUN_BTN_OPTIONS.RUN_ALREADY)
  })

  afterEach(() => {
    jest.restoreAllMocks()
  })

  it("renders correctly and triggers handleRunPipelineByUid on Run button click", async () => {
    render(
      <Provider store={store}>
        <SnackbarProvider>
          <RunButtons status={"StartUninitialized"} {...mockProps} />
        </SnackbarProvider>
      </Provider>,
    )

    const runButton = runButtonFor(RUN_BTN_OPTIONS.RUN_ALREADY)

    await userEvent.click(runButton)

    // The click awaits the storage check before it reaches the handler
    await waitFor(() => {
      expect(mockProps.handleRunPipelineByUid).toHaveBeenCalledTimes(1)
    })
  })

  it("disables the button after it is clicked", async () => {
    // Use state to simulate the button becoming disabled after a click
    const WrapperComponent = () => {
      const [runDisabled, setRunDisabled] = useState(false)

      const updatedProps = {
        ...mockProps,
        runDisabled,
        handleRunPipelineByUid: () => {
          // Simulate disabling the button after clicking
          setRunDisabled(true)
        },
      }

      return (
        <Provider store={store}>
          <SnackbarProvider>
            <RunButtons status={"StartUninitialized"} {...updatedProps} />
          </SnackbarProvider>
        </Provider>
      )
    }

    render(<WrapperComponent />)

    const runButton = runButtonFor(RUN_BTN_OPTIONS.RUN_ALREADY)

    expect(runButton).not.toBeDisabled()

    await userEvent.click(runButton)

    expect(runButton).toBeDisabled()
  })

  describe("Pre-flight storage check", () => {
    const renderAndClickRun = async () => {
      render(
        <Provider store={store}>
          <SnackbarProvider>
            <RunButtons status={"StartUninitialized"} {...mockProps} />
          </SnackbarProvider>
        </Provider>,
      )
      await userEvent.click(runButtonFor(RUN_BTN_OPTIONS.RUN_ALREADY))
    }

    it("asks before running when the quota could not be read", async () => {
      mockGetMyStorageAlertApi.mockRejectedValue(new Error("Network error"))

      await renderAndClickRun()

      await waitFor(() => {
        expect(screen.getByText("Storage Check Failed")).toBeInTheDocument()
      })
      expect(
        screen.getByText(/Unable to verify your storage quota/),
      ).toBeInTheDocument()
      expect(mockProps.handleRunPipelineByUid).not.toHaveBeenCalled()
    })

    it("does not run when that confirmation is cancelled", async () => {
      mockGetMyStorageAlertApi.mockRejectedValue(new Error("Network error"))

      await renderAndClickRun()
      await waitFor(() => {
        expect(screen.getByText("Storage Check Failed")).toBeInTheDocument()
      })
      await userEvent.click(screen.getByRole("button", { name: /Cancel/i }))

      expect(mockProps.handleRunPipelineByUid).not.toHaveBeenCalled()
    })

    it("runs when the user proceeds despite the failed check", async () => {
      mockGetMyStorageAlertApi.mockRejectedValue(new Error("Network error"))

      await renderAndClickRun()
      await waitFor(() => {
        expect(screen.getByText("Storage Check Failed")).toBeInTheDocument()
      })
      await userEvent.click(
        screen.getByRole("button", { name: /Proceed Anyway/i }),
      )

      await waitFor(() => {
        expect(mockProps.handleRunPipelineByUid).toHaveBeenCalled()
      })
    })

    it("blocks the run outright when the quota is already exceeded", async () => {
      mockGetMyStorageAlertApi.mockResolvedValue({
        has_alert: true,
        alert: createMockStorageAlert({
          alert_level: "danger",
          storage_usage_percent: 105,
        }),
      })

      await renderAndClickRun()

      await waitFor(() => {
        expect(mockProps.handleRunPipelineByUid).not.toHaveBeenCalled()
      })
      // Blocked outright rather than offered as a choice
      expect(screen.queryByText("Storage Check Failed")).not.toBeInTheDocument()
    })

    it("runs without asking when usage is high but under the quota", async () => {
      mockGetMyStorageAlertApi.mockResolvedValue({
        has_alert: true,
        alert: createMockStorageAlert({
          alert_level: "critical",
          storage_usage_percent: 95,
        }),
      })

      await renderAndClickRun()

      await waitFor(() => {
        expect(mockProps.handleRunPipelineByUid).toHaveBeenCalled()
      })
      expect(screen.queryByText("Storage Check Failed")).not.toBeInTheDocument()
    })
  })

  // Both messages are pinned verbatim here because the e2e regex accepts either
  // one, and on a fresh workspace the input-file branch always wins.
  describe("Pre-run validation messages", () => {
    const renderAndClickRun = async (
      overrides: Partial<typeof mockProps>,
    ): Promise<void> => {
      render(
        <Provider store={store}>
          <SnackbarProvider>
            <RunButtons
              status={"StartUninitialized"}
              {...mockProps}
              {...overrides}
            />
          </SnackbarProvider>
        </Provider>,
      )
      await userEvent.click(runButtonFor(RUN_BTN_OPTIONS.RUN_ALREADY))
    }

    it("asks for algorithm nodes when the flowchart has none", async () => {
      await renderAndClickRun({
        algorithmNodeNotExist: true,
        filePathIsUndefined: false,
      })

      expect(
        await screen.findByText(
          "please add some algorithm nodes to the flowchart",
        ),
      ).toBeInTheDocument()
      expect(
        screen.queryByText("please select input file"),
      ).not.toBeInTheDocument()
      // The validation short-circuits before the pre-flight storage check
      expect(mockGetMyStorageAlertApi).not.toHaveBeenCalled()
      expect(mockProps.handleRunPipelineByUid).not.toHaveBeenCalled()
    })

    it("asks for an input file when none is selected", async () => {
      await renderAndClickRun({
        algorithmNodeNotExist: false,
        filePathIsUndefined: true,
      })

      expect(
        await screen.findByText("please select input file"),
      ).toBeInTheDocument()
      expect(
        screen.queryByText("please add some algorithm nodes to the flowchart"),
      ).not.toBeInTheDocument()
      expect(mockGetMyStorageAlertApi).not.toHaveBeenCalled()
      expect(mockProps.handleRunPipelineByUid).not.toHaveBeenCalled()
    })

    // Both missing: the input file is the one the user has to fix first.
    it("reports only the input file when both are missing", async () => {
      await renderAndClickRun({
        algorithmNodeNotExist: true,
        filePathIsUndefined: true,
      })

      expect(
        await screen.findByText("please select input file"),
      ).toBeInTheDocument()
      expect(
        screen.queryByText("please add some algorithm nodes to the flowchart"),
      ).not.toBeInTheDocument()
    })
  })

  // The rapid-click cooldown: a ref that a second click reads before the first
  // has cleared it, so a double-click sends one run rather than two.
  describe("Run request cooldown", () => {
    it("is three seconds long", () => {
      expect(RUN_REQUEST_DEBOUNCE_MS).toBe(3000)
    })

    const renderRunButtons = (
      runBtn: RUN_BTN_TYPE = RUN_BTN_OPTIONS.RUN_ALREADY,
    ) => {
      render(
        <Provider store={storeWith(runBtn)}>
          <SnackbarProvider>
            <RunButtons status={"StartUninitialized"} {...mockProps} />
          </SnackbarProvider>
        </Provider>,
      )
      return runButtonFor(runBtn)
    }

    // The click handler awaits the storage check, so each click needs its
    // microtasks flushed before the next one is meaningful
    const clickAndSettle = async (button: HTMLElement) => {
      await act(async () => {
        fireEvent.click(button)
      })
    }

    const wait = async (ms: number) => {
      await act(async () => {
        jest.advanceTimersByTime(ms)
      })
    }

    beforeEach(() => {
      jest.useFakeTimers()
    })

    afterEach(() => {
      jest.useRealTimers()
    })

    it("sends one run request however many times RUN is clicked", async () => {
      const runButton = renderRunButtons()

      await clickAndSettle(runButton)
      expect(mockProps.handleRunPipelineByUid).toHaveBeenCalledTimes(1)

      for (let click = 0; click < 4; click++) {
        await clickAndSettle(runButton)
      }

      expect(mockProps.handleRunPipelineByUid).toHaveBeenCalledTimes(1)
    })

    it("keeps refusing until the cooldown has fully elapsed", async () => {
      const runButton = renderRunButtons()

      await clickAndSettle(runButton)
      // Just inside the window: still one request
      await wait(RUN_REQUEST_DEBOUNCE_MS - 100)
      await clickAndSettle(runButton)
      expect(mockProps.handleRunPipelineByUid).toHaveBeenCalledTimes(1)

      // Past it: the next click is a new run, so the guard is a cooldown and
      // not a permanent lock
      await wait(100)
      await clickAndSettle(runButton)
      expect(mockProps.handleRunPipelineByUid).toHaveBeenCalledTimes(2)
    })

    // RUN_NEW is the app's default, and it routes through the name dialog rather
    // than straight to a run, so it needs the guard proved separately.
    describe("with the default Run New option", () => {
      const runFromDialog = async () => {
        const dialogRun = screen.getByRole("button", { name: "Run" })
        await act(async () => {
          fireEvent.click(dialogRun)
        })
      }

      it("opens the name dialog rather than running immediately", async () => {
        const runButton = renderRunButtons(RUN_BTN_OPTIONS.RUN_NEW)

        await clickAndSettle(runButton)

        expect(screen.getByRole("button", { name: "Run" })).toBeInTheDocument()
        expect(mockProps.handleRunPipeline).not.toHaveBeenCalled()
        expect(mockProps.handleRunPipelineByUid).not.toHaveBeenCalled()
      })

      it("sends one run request however many times the dialog's Run is clicked", async () => {
        const runButton = renderRunButtons(RUN_BTN_OPTIONS.RUN_NEW)
        await clickAndSettle(runButton)

        await runFromDialog()
        expect(mockProps.handleRunPipeline).toHaveBeenCalledTimes(1)

        // The dialog closes on the first click, so a second arrives only when a
        // rapid double-click beats the close. Re-opening and clicking again
        // inside the window is the reachable version of that.
        await clickAndSettle(runButton)
        await runFromDialog()

        expect(mockProps.handleRunPipeline).toHaveBeenCalledTimes(1)
      })

      it("accepts a second run once the cooldown has elapsed", async () => {
        const runButton = renderRunButtons(RUN_BTN_OPTIONS.RUN_NEW)
        await clickAndSettle(runButton)
        await runFromDialog()

        await wait(RUN_REQUEST_DEBOUNCE_MS)
        await clickAndSettle(runButton)
        await runFromDialog()

        expect(mockProps.handleRunPipeline).toHaveBeenCalledTimes(2)
      })
    })
  })
})
