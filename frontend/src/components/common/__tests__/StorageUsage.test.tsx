/**
 * Storage usage progress bar colour bands.
 *
 * Red above the critical threshold, orange between the two, blue below the
 * warning one. Boundaries come from the app's thresholds and colours from its
 * theme, so a change to either moves the test with it - and the thresholds
 * themselves are pinned, or a wrong value would move the test too.
 */

import { describe, it, expect, jest, beforeEach } from "@jest/globals"
import "@testing-library/jest-dom"
import { rgbToHex, ThemeProvider } from "@mui/material/styles"
import { render, screen, act } from "@testing-library/react"

import * as StorageAlertsApi from "api/storage/StorageAlerts"
import StorageUsage from "components/common/StorageUsage"
import { SubscriptionAlertThresholds } from "const/Subscription"
import { theme } from "Theme"

jest.mock("api/storage/StorageAlerts")

const mockGetMyStorageUsageApi =
  StorageAlertsApi.getMyStorageUsageApi as jest.MockedFunction<
    typeof StorageAlertsApi.getMyStorageUsageApi
  >

const palette = theme.palette

const usageAt = (percent: number): StorageAlertsApi.StorageUsage => ({
  storage_usage_bytes: 1,
  storage_usage_formatted: "1 GB",
  storage_quota_bytes: 5368709120,
  storage_quota_formatted: "5 GB",
  storage_usage_percent: percent,
  alert_level: null,
  thresholds: { critical: 100, danger: 90 },
})

const renderAt = async (percent: number) => {
  mockGetMyStorageUsageApi.mockResolvedValue(usageAt(percent))
  await act(async () => {
    render(
      <ThemeProvider theme={theme}>
        <StorageUsage />
      </ThemeProvider>,
    )
  })
  // The root carries the track colour; the inner bar carries the fill
  const bar = screen.getByRole("progressbar").firstElementChild as Element
  return rgbToHex(getComputedStyle(bar).backgroundColor)
}

const { WARNING, CRITICAL } = SubscriptionAlertThresholds

describe("StorageUsage progress bar colour", () => {
  beforeEach(() => {
    jest.clearAllMocks()
  })

  // Deriving the inputs below from these constants makes the test track them,
  // which also means it would follow a wrong value. Pin the values themselves.
  it("uses the documented threshold percentages", () => {
    expect([WARNING, CRITICAL]).toEqual([90, 100])
  })

  it.each([
    ["red above the critical threshold", CRITICAL + 10, palette.error.main],
    ["red exactly at the critical threshold", CRITICAL, palette.error.main],
    ["orange at the warning threshold", WARNING, palette.warning.main],
    ["orange just under critical", CRITICAL - 0.1, palette.warning.main],
    [
      "blue just under the warning threshold",
      WARNING - 0.1,
      palette.primary.main,
    ],
    ["blue at zero usage", 0, palette.primary.main],
  ])("is %s", async (label, percent, expected) => {
    expect(await renderAt(percent)).toBe(expected)
  })

  it("caps the bar at 100% while still reporting the real percentage", async () => {
    mockGetMyStorageUsageApi.mockResolvedValue(usageAt(137.4))
    await act(async () => {
      render(<StorageUsage />)
    })

    expect(screen.getByRole("progressbar")).toHaveAttribute(
      "aria-valuenow",
      "100",
    )
    expect(screen.getByText("137.4%")).toBeInTheDocument()
  })
})
