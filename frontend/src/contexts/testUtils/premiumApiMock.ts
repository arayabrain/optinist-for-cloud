/**
 * Shared jest double for the premium-assignment API boundary.
 *
 * Lives outside __tests__/ because CRA collects every file under that
 * directory as a suite. Wire it into a suite with:
 *
 *   jest.mock("api/premium/PremiumAssignmentApi", () =>
 *     require("contexts/testUtils/premiumApiMock").mockPremiumApi)
 */

import { jest } from "@jest/globals"

import type {
  PremiumAssignmentResult,
  PremiumHeartbeatResult,
  PremiumReleaseResult,
  PremiumStatusResult,
  RoutingInfo,
} from "api/premium/PremiumAssignmentApi"

export const mockPremiumApi = {
  __esModule: true,
  assignPremiumInstance: jest.fn<Promise<PremiumAssignmentResult>, []>(),
  releasePremiumInstance: jest.fn<Promise<PremiumReleaseResult>, []>(),
  getPremiumStatus: jest.fn<Promise<PremiumStatusResult>, []>(),
  getBeaconTokenApi: jest.fn<Promise<{ data: { token: string } }>, []>(),
  sendPremiumHeartbeat: jest.fn<Promise<PremiumHeartbeatResult>, []>(),
  getRoutingInfo: jest.fn<Promise<RoutingInfo | null>, []>(),
  logPremiumUiEvent: jest.fn<
    Promise<void>,
    [string, Record<string, unknown>?]
  >(),
}

/** Re-installs the ambient resolutions that CRA's resetMocks strips per test. */
export const installPremiumApiDefaults = (): void => {
  mockPremiumApi.getBeaconTokenApi.mockResolvedValue({
    data: { token: "beacon-token" },
  })
  mockPremiumApi.sendPremiumHeartbeat.mockResolvedValue(
    {} as PremiumHeartbeatResult,
  )
  mockPremiumApi.getRoutingInfo.mockResolvedValue(null)
  mockPremiumApi.logPremiumUiEvent.mockResolvedValue(undefined)
}
