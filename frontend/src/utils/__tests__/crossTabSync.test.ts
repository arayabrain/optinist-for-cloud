import {
  describe,
  it,
  expect,
  beforeEach,
  afterEach,
  jest,
} from "@jest/globals"

import {
  syncActivityAcrossTabs,
  getLastActivityFromAnyTab,
  onActivityFromOtherTab,
  TabSyncService,
} from "utils/crossTabSync"

describe("Activity Sync Functions", () => {
  let mockStorage: Record<string, string>
  let originalLocalStorage: Storage

  beforeEach(() => {
    mockStorage = {}
    originalLocalStorage = window.localStorage

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
  })

  describe("syncActivityAcrossTabs", () => {
    it("should store timestamp in localStorage", () => {
      const timestamp = 1234567890
      syncActivityAcrossTabs(timestamp)

      expect(mockStorage["premium_last_activity"]).toBe(timestamp.toString())
    })

    it("should overwrite previous timestamp", () => {
      syncActivityAcrossTabs(1000)
      syncActivityAcrossTabs(2000)

      expect(mockStorage["premium_last_activity"]).toBe("2000")
    })
  })

  describe("getLastActivityFromAnyTab", () => {
    it("should return 0 when no activity stored", () => {
      const result = getLastActivityFromAnyTab()
      expect(result).toBe(0)
    })

    it("should return stored timestamp", () => {
      mockStorage["premium_last_activity"] = "1234567890"

      const result = getLastActivityFromAnyTab()
      expect(result).toBe(1234567890)
    })

    it("should parse timestamp as integer", () => {
      mockStorage["premium_last_activity"] = "9999999999"

      const result = getLastActivityFromAnyTab()
      expect(typeof result).toBe("number")
      expect(result).toBe(9999999999)
    })
  })

  describe("onActivityFromOtherTab", () => {
    it("should return unsubscribe function", () => {
      const callback = jest.fn()
      const unsubscribe = onActivityFromOtherTab(callback)

      expect(typeof unsubscribe).toBe("function")
      unsubscribe()
    })

    it("should call callback when storage event fires with activity key", () => {
      const callback = jest.fn()
      const unsubscribe = onActivityFromOtherTab(callback)

      const timestamp = 1234567890
      const event = new StorageEvent("storage", {
        key: "premium_last_activity",
        newValue: timestamp.toString(),
      })
      window.dispatchEvent(event)

      expect(callback).toHaveBeenCalledWith(timestamp)

      unsubscribe()
    })

    it("should not call callback for other storage keys", () => {
      const callback = jest.fn()
      const unsubscribe = onActivityFromOtherTab(callback)

      const event = new StorageEvent("storage", {
        key: "some_other_key",
        newValue: "some_value",
      })
      window.dispatchEvent(event)

      expect(callback).not.toHaveBeenCalled()

      unsubscribe()
    })

    it("should not call callback after unsubscribe", () => {
      const callback = jest.fn()
      const unsubscribe = onActivityFromOtherTab(callback)

      unsubscribe()

      const event = new StorageEvent("storage", {
        key: "premium_last_activity",
        newValue: "1234567890",
      })
      window.dispatchEvent(event)

      expect(callback).not.toHaveBeenCalled()
    })

    it("should not call callback when newValue is null", () => {
      const callback = jest.fn()
      const unsubscribe = onActivityFromOtherTab(callback)

      const event = new StorageEvent("storage", {
        key: "premium_last_activity",
        newValue: null,
      })
      window.dispatchEvent(event)

      expect(callback).not.toHaveBeenCalled()

      unsubscribe()
    })
  })
})

// ============================================================================
// TabSyncService Tests (Cases 54-56)
// ============================================================================

describe("TabSyncService", () => {
  let service: TabSyncService
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let mockChannel: any

  beforeEach(() => {
    mockChannel = {
      postMessage: jest.fn(),
      close: jest.fn(),
      onmessage: null,
    }

    // Mock BroadcastChannel
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    global.BroadcastChannel = jest.fn(() => mockChannel) as any

    service = new TabSyncService()
  })

  afterEach(() => {
    service.destroy()
  })

  describe("broadcast", () => {
    it("should post message to channel", () => {
      service.broadcast({ type: "STORAGE_UPDATED" })

      expect(mockChannel.postMessage).toHaveBeenCalledWith({
        type: "STORAGE_UPDATED",
      })
    })

    it("should include payload when provided", () => {
      service.broadcast({
        type: "ALERT_DISMISSED",
        payload: { alertId: "test-alert" },
      })

      expect(mockChannel.postMessage).toHaveBeenCalledWith({
        type: "ALERT_DISMISSED",
        payload: { alertId: "test-alert" },
      })
    })
  })

  describe("convenience methods", () => {
    it("broadcastStorageUpdate should broadcast STORAGE_UPDATED", () => {
      service.broadcastStorageUpdate()

      expect(mockChannel.postMessage).toHaveBeenCalledWith({
        type: "STORAGE_UPDATED",
      })
    })

    it("broadcastAlertDismissed should broadcast with alertId", () => {
      service.broadcastAlertDismissed("my-alert-123")

      expect(mockChannel.postMessage).toHaveBeenCalledWith({
        type: "ALERT_DISMISSED",
        payload: { alertId: "my-alert-123" },
      })
    })

    it("broadcastPremiumReleased should broadcast PREMIUM_RELEASED", () => {
      service.broadcastPremiumReleased()

      expect(mockChannel.postMessage).toHaveBeenCalledWith({
        type: "PREMIUM_RELEASED",
      })
    })

    it("broadcastLogout should broadcast LOGOUT", () => {
      service.broadcastLogout()

      expect(mockChannel.postMessage).toHaveBeenCalledWith({
        type: "LOGOUT",
      })
    })
  })

  describe("on (type-specific handlers)", () => {
    it("should call handler for matching message type", () => {
      const handler = jest.fn()
      service.on("LOGOUT", handler)

      // Simulate incoming message
      if (mockChannel.onmessage) {
        mockChannel.onmessage({
          data: { type: "LOGOUT" },
        } as MessageEvent)
      }

      expect(handler).toHaveBeenCalledWith({ type: "LOGOUT" })
    })

    it("should not call handler for non-matching message type", () => {
      const handler = jest.fn()
      service.on("LOGOUT", handler)

      if (mockChannel.onmessage) {
        mockChannel.onmessage({
          data: { type: "STORAGE_UPDATED" },
        } as MessageEvent)
      }

      expect(handler).not.toHaveBeenCalled()
    })

    it("should return unsubscribe function", () => {
      const handler = jest.fn()
      const unsubscribe = service.on("LOGOUT", handler)

      unsubscribe()

      if (mockChannel.onmessage) {
        mockChannel.onmessage({
          data: { type: "LOGOUT" },
        } as MessageEvent)
      }

      expect(handler).not.toHaveBeenCalled()
    })

    it("should support multiple handlers for same type", () => {
      const handler1 = jest.fn()
      const handler2 = jest.fn()
      service.on("LOGOUT", handler1)
      service.on("LOGOUT", handler2)

      if (mockChannel.onmessage) {
        mockChannel.onmessage({
          data: { type: "LOGOUT" },
        } as MessageEvent)
      }

      expect(handler1).toHaveBeenCalled()
      expect(handler2).toHaveBeenCalled()
    })
  })

  describe("onAny (global handlers)", () => {
    it("should call global handler for any message type", () => {
      const handler = jest.fn()
      service.onAny(handler)

      if (mockChannel.onmessage) {
        mockChannel.onmessage({
          data: { type: "LOGOUT" },
        } as MessageEvent)
        mockChannel.onmessage({
          data: { type: "STORAGE_UPDATED" },
        } as MessageEvent)
      }

      expect(handler).toHaveBeenCalledTimes(2)
    })

    it("should return unsubscribe function", () => {
      const handler = jest.fn()
      const unsubscribe = service.onAny(handler)

      unsubscribe()

      if (mockChannel.onmessage) {
        mockChannel.onmessage({
          data: { type: "LOGOUT" },
        } as MessageEvent)
      }

      expect(handler).not.toHaveBeenCalled()
    })
  })

  describe("error handling", () => {
    it("should not throw if handler throws", () => {
      const errorHandler = jest.fn().mockImplementation(() => {
        throw new Error("Handler error")
      })
      const safeHandler = jest.fn()

      service.on("LOGOUT", errorHandler)
      service.on("LOGOUT", safeHandler)

      expect(() => {
        if (mockChannel.onmessage) {
          mockChannel.onmessage({
            data: { type: "LOGOUT" },
          } as MessageEvent)
        }
      }).not.toThrow()

      // Other handlers should still be called
      expect(safeHandler).toHaveBeenCalled()
    })

    it("should ignore messages without type", () => {
      const handler = jest.fn()
      service.onAny(handler)

      expect(() => {
        if (mockChannel.onmessage) {
          mockChannel.onmessage({
            data: { payload: "no type" },
          } as MessageEvent)
        }
      }).not.toThrow()

      expect(handler).not.toHaveBeenCalled()
    })

    it("should ignore null messages", () => {
      const handler = jest.fn()
      service.onAny(handler)

      expect(() => {
        if (mockChannel.onmessage) {
          mockChannel.onmessage({
            data: null,
          } as MessageEvent)
        }
      }).not.toThrow()

      expect(handler).not.toHaveBeenCalled()
    })
  })

  describe("destroy", () => {
    it("should close the channel", () => {
      service.destroy()

      expect(mockChannel.close).toHaveBeenCalled()
    })

    it("should not broadcast after destroy", () => {
      service.destroy()
      service.broadcast({ type: "LOGOUT" })

      // postMessage should not be called after destroy
      // (only the calls before destroy count)
      expect(mockChannel.postMessage).not.toHaveBeenCalled()
    })
  })
})

describe("TabSyncService without BroadcastChannel support", () => {
  it("should not throw if BroadcastChannel is undefined", () => {
    const originalBroadcastChannel = global.BroadcastChannel
    // @ts-expect-error - Testing undefined case
    delete global.BroadcastChannel

    expect(() => {
      const service = new TabSyncService()
      service.broadcast({ type: "LOGOUT" })
      service.destroy()
    }).not.toThrow()

    global.BroadcastChannel = originalBroadcastChannel
  })
})
