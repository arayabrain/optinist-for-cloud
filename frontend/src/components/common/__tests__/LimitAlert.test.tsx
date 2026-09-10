import { MemoryRouter } from "react-router-dom"

import { SnackbarProvider } from "notistack"

import {
  describe,
  it,
  expect,
  beforeEach,
  jest,
  afterEach,
} from "@jest/globals"
import { render, screen, act, waitFor, fireEvent } from "@testing-library/react"

import * as StorageAlertsApi from "api/storage/StorageAlerts"
import LimitAlert from "components/common/LimitAlert"
import { LimitAlertType } from "const/Subscription"

// Mock the API module
jest.mock("api/storage/StorageAlerts")

// Mock auth utils
jest.mock("utils/auth/AuthUtils", () => ({
  getToken: () => "mock-token",
}))

const mockGetMyLimitAlertApi =
  StorageAlertsApi.getMyLimitAlertApi as jest.MockedFunction<
    typeof StorageAlertsApi.getMyLimitAlertApi
  >

const createMockAlert = (
  overrides: Partial<StorageAlertsApi.LimitAlert> = {},
): StorageAlertsApi.LimitAlert => ({
  has_alert: true,
  alert_type: LimitAlertType.GRACE,
  message: "Your subscription has expired",
  days_remaining: 7,
  deletion_date: "2026-02-15",
  storage_usage_bytes: 5905580032,
  storage_usage_gb: 5.5,
  storage_quota_bytes: 3221225472,
  storage_quota_gb: 3.0,
  excess_data_bytes: 2684354560,
  excess_data_gb: 2.5,
  ...overrides,
})

const renderLimitAlert = (props = {}) => {
  return render(
    <MemoryRouter>
      <SnackbarProvider>
        <LimitAlert {...props} />
      </SnackbarProvider>
    </MemoryRouter>,
  )
}

describe("LimitAlert", () => {
  let mockStorage: Record<string, string>

  beforeEach(() => {
    mockStorage = {}

    const localStorageMock = {
      getItem: (key: string) => mockStorage[key] || null,
      setItem: (key: string, value: string) => {
        mockStorage[key] = value
      },
      removeItem: (key: string) => {
        delete mockStorage[key]
      },
      clear: () => {
        mockStorage = {}
      },
      length: 0,
      key: () => null,
    }

    Object.defineProperty(window, "localStorage", {
      value: localStorageMock,
      writable: true,
    })

    jest.clearAllMocks()
  })

  afterEach(() => {
    jest.restoreAllMocks()
  })

  describe("Cross-Tab Alert Dismissal (Case 31)", () => {
    it("should sync dismissal from other tabs via storage event", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(createMockAlert())

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByText("Your subscription has expired")).toBeTruthy()
      })

      // Simulate dismissal from another tab
      act(() => {
        const event = new StorageEvent("storage", {
          key: "dismissedAlerts",
          newValue: JSON.stringify({ limitAlert: true }),
        })
        window.dispatchEvent(event)
      })

      // Alert should be dismissed
      await waitFor(() => {
        expect(screen.queryByText("Your subscription has expired")).toBeFalsy()
      })
    })

    it("should ignore storage events for other keys", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(createMockAlert())

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByText("Your subscription has expired")).toBeTruthy()
      })

      // Simulate storage event for different key
      act(() => {
        const event = new StorageEvent("storage", {
          key: "someOtherKey",
          newValue: JSON.stringify({ limitAlert: true }),
        })
        window.dispatchEvent(event)
      })

      // Alert should still be visible
      expect(screen.getByText("Your subscription has expired")).toBeTruthy()
    })

    it("should handle invalid JSON in storage event gracefully", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(createMockAlert())

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByText("Your subscription has expired")).toBeTruthy()
      })

      // Simulate storage event with invalid JSON
      act(() => {
        const event = new StorageEvent("storage", {
          key: "dismissedAlerts",
          newValue: "invalid-json",
        })
        window.dispatchEvent(event)
      })

      // Alert should still be visible (error handled gracefully)
      expect(screen.getByText("Your subscription has expired")).toBeTruthy()
    })
  })

  describe("Negative Days Remaining (Case 35)", () => {
    it("should render progress bar for negative days", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ days_remaining: -5 }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByRole("progressbar")).toBeTruthy()
      })
    })

    it("should render progress bar for zero days", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ days_remaining: 0 }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByRole("progressbar")).toBeTruthy()
      })
    })

    it("should display 0 days for negative values", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ days_remaining: -3 }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByText("0 days")).toBeTruthy()
      })
    })

    it("should render progress bar and display 0 days for zero remaining", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ days_remaining: 0 }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByRole("progressbar")).toBeTruthy()
        expect(screen.getByText("0 days")).toBeTruthy()
      })
    })

    it("should not show progress bar when days_remaining is undefined", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ days_remaining: undefined as unknown as number }),
      )

      renderLimitAlert()

      // The alert itself must have rendered, otherwise the absent progress bar
      // proves nothing.
      expect(
        await screen.findByText("Your subscription has expired"),
      ).toBeTruthy()
      expect(screen.queryByRole("progressbar")).toBeFalsy()
      expect(screen.queryByText("Time Remaining")).toBeFalsy()
    })
  })

  describe("Progress Bar Rendering for Different Day Thresholds", () => {
    it("should render progress bar for CRITICAL days (<=0)", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ days_remaining: 0 }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByRole("progressbar")).toBeTruthy()
        expect(screen.getByText("0 days")).toBeTruthy()
      })
    })

    it("should render progress bar for WARNING days (8-14)", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ days_remaining: 10 }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByRole("progressbar")).toBeTruthy()
        expect(screen.getByText("10 days")).toBeTruthy()
      })
    })

    it("should render progress bar for safe days (>14)", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ days_remaining: 20 }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByRole("progressbar")).toBeTruthy()
        expect(screen.getByText("20 days")).toBeTruthy()
      })
    })
  })

  describe("OVERDUE Alert Acknowledgment (Case 33)", () => {
    const overdueAlert = createMockAlert({
      alert_type: LimitAlertType.OVERDUE,
      message: "Your data will be deleted soon",
      days_remaining: 0,
    })

    it("should not show close button for OVERDUE alerts", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(overdueAlert)

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByText("Your data will be deleted soon")).toBeTruthy()
      })

      // Close icon button should not be present for OVERDUE alerts
      const closeButtons = screen.queryAllByRole("button")
      const closeIconButton = closeButtons.find(
        (btn) => btn.querySelector("[data-testid=\"CloseIcon\"]") !== null,
      )
      expect(closeIconButton).toBeFalsy()
    })

    it("should show acknowledgment checkbox for OVERDUE alerts", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(overdueAlert)

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByRole("checkbox")).toBeTruthy()
      })

      expect(
        screen.getByText(/I understand my data will be deleted/i),
      ).toBeTruthy()
    })

    it("should show Action Required warning for OVERDUE alerts", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(overdueAlert)

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByText("Action Required")).toBeTruthy()
      })
    })

    it("should disable dismiss button until acknowledgment", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(overdueAlert)

      renderLimitAlert()

      await waitFor(() => {
        const dismissButton = screen.getByRole("button", { name: /dismiss/i })
        expect(dismissButton).toBeTruthy()
        expect(dismissButton.hasAttribute("disabled")).toBe(true)
      })
    })

    it("should enable dismiss button after acknowledgment", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(overdueAlert)

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByRole("checkbox")).toBeTruthy()
      })

      // Check the acknowledgment checkbox
      const checkbox = screen.getByRole("checkbox")
      fireEvent.click(checkbox)

      // Dismiss button should now be enabled
      const dismissButton = screen.getByRole("button", { name: /dismiss/i })
      expect(dismissButton.hasAttribute("disabled")).toBe(false)
    })

    it("should not show acknowledgment for non-OVERDUE alerts", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ alert_type: LimitAlertType.GRACE }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByText("Your subscription has expired")).toBeTruthy()
      })

      // No checkbox should be present
      expect(screen.queryByRole("checkbox")).toBeFalsy()
    })

    it("should show a working close button for non-OVERDUE alerts", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ alert_type: LimitAlertType.STORAGE }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByText("Your subscription has expired")).toBeTruthy()
      })

      const closeIconButton = screen
        .queryAllByRole("button")
        .find((btn) => btn.querySelector("[data-testid=\"CloseIcon\"]") !== null)
      expect(closeIconButton).toBeTruthy()

      fireEvent.click(closeIconButton!)

      await waitFor(() => {
        expect(screen.queryByText("Your subscription has expired")).toBeFalsy()
      })
    })
  })

  describe("Time Remaining Formatting (Case 89)", () => {
    it("should display hours when less than 1 day remaining", async () => {
      // Set deletion_date to 12 hours from now
      const twelveHoursFromNow = new Date(Date.now() + 12 * 60 * 60 * 1000)
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({
          days_remaining: 0.5,
          deletion_date: twelveHoursFromNow.toISOString(),
        }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByText(/hour/i)).toBeTruthy()
      })
    })

    it("should display days for 1 or more days remaining", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ days_remaining: 5 }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByText("5 days")).toBeTruthy()
      })
    })

    it("should display singular day for exactly 1 day", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(
        createMockAlert({ days_remaining: 1 }),
      )

      renderLimitAlert()

      await waitFor(() => {
        expect(screen.getByText("1 day")).toBeTruthy()
      })
    })
  })

  describe("OVERDUE Alert Modal (Case 33)", () => {
    const overdueAlert = createMockAlert({
      alert_type: LimitAlertType.OVERDUE,
      message: "Your data will be deleted soon",
      days_remaining: 0,
    })

    it("should show urgent dialog title for OVERDUE modal", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(overdueAlert)

      renderLimitAlert({ showAsModal: true })

      await waitFor(() => {
        expect(screen.getByText("Urgent: Data Deletion Imminent")).toBeTruthy()
      })
    })

    it("should require acknowledgment before dismissing modal", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(overdueAlert)

      renderLimitAlert({ showAsModal: true })

      await waitFor(() => {
        const dismissButton = screen.getByRole("button", {
          name: /remind me later/i,
        })
        expect(dismissButton.hasAttribute("disabled")).toBe(true)
      })
    })

    it("should enable dismiss after checking acknowledgment in modal", async () => {
      mockGetMyLimitAlertApi.mockResolvedValue(overdueAlert)

      renderLimitAlert({ showAsModal: true })

      await waitFor(() => {
        expect(screen.getByRole("checkbox")).toBeTruthy()
      })

      fireEvent.click(screen.getByRole("checkbox"))

      const dismissButton = screen.getByRole("button", {
        name: /remind me later/i,
      })
      expect(dismissButton.hasAttribute("disabled")).toBe(false)
    })
  })
})
