/**
 * Tests for Sleep Detection Hook (Cases 50-51)
 *
 * Tests verify that the hook correctly detects when a device wakes from sleep
 * by monitoring interval timing gaps, while ignoring false positives from
 * browser background-tab timer throttling.
 */

import {
  describe,
  it,
  expect,
  jest,
  beforeEach,
  afterEach,
} from "@jest/globals"
import { renderHook, act } from "@testing-library/react"

import { useSleepDetection } from "hooks/useSleepDetection"

describe("useSleepDetection (Cases 50-51)", () => {
  let currentTime: number

  beforeEach(() => {
    currentTime = 1000000
    jest.useFakeTimers()
    jest.spyOn(Date, "now").mockImplementation(() => currentTime)
    // Default: tab is visible
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "visible",
    })
  })

  afterEach(() => {
    jest.useRealTimers()
    jest.restoreAllMocks()
  })

  const advanceTime = (ms: number) => {
    currentTime += ms
    jest.advanceTimersByTime(ms)
  }

  const setVisibility = (state: "visible" | "hidden") => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => state,
    })
  }

  /** Change visibility and fire the corresponding event (like a real browser). */
  const changeVisibility = (state: "visible" | "hidden") => {
    setVisibility(state)
    document.dispatchEvent(new Event("visibilitychange"))
  }

  it("should not call onWake during normal interval ticks", () => {
    const onWake = jest.fn()
    renderHook(() => useSleepDetection(onWake))

    act(() => {
      advanceTime(30000)
    })

    expect(onWake).not.toHaveBeenCalled()
  })

  it("should call onWake when interval fires late (simulating sleep)", () => {
    const onWake = jest.fn()
    renderHook(() => useSleepDetection(onWake))

    act(() => {
      // Simulate sleep: advance Date.now() by more than the threshold (150s)
      currentTime += 200000
      jest.advanceTimersByTime(30000)
    })

    expect(onWake).toHaveBeenCalledTimes(1)
  })

  it("should not call onWake when tab is hidden (background throttling)", () => {
    const onWake = jest.fn()
    renderHook(() => useSleepDetection(onWake))

    setVisibility("hidden")

    act(() => {
      // Large gap but tab is hidden — this is browser throttling, not sleep
      currentTime += 200000
      jest.advanceTimersByTime(30000)
    })

    expect(onWake).not.toHaveBeenCalled()
  })

  it("should not trigger on 60s gap (browser background throttle)", () => {
    const onWake = jest.fn()
    renderHook(() => useSleepDetection(onWake))

    act(() => {
      // 60s gap is typical of Chromium background tab throttling
      currentTime += 60000
      jest.advanceTimersByTime(30000)
    })

    expect(onWake).not.toHaveBeenCalled()
  })

  it("should call onWake multiple times for multiple sleep events", () => {
    const onWake = jest.fn()
    renderHook(() => useSleepDetection(onWake))

    // First sleep event
    act(() => {
      currentTime += 200000
      jest.advanceTimersByTime(30000)
    })
    expect(onWake).toHaveBeenCalledTimes(1)

    // Normal tick - no wake
    act(() => {
      advanceTime(30000)
    })
    expect(onWake).toHaveBeenCalledTimes(1)

    // Second sleep event
    act(() => {
      currentTime += 200000
      jest.advanceTimersByTime(30000)
    })
    expect(onWake).toHaveBeenCalledTimes(2)
  })

  it("should use custom check interval when provided", () => {
    const onWake = jest.fn()
    renderHook(() => useSleepDetection(onWake, { checkIntervalMs: 5000 }))

    // Normal tick at 5s interval
    act(() => {
      advanceTime(5000)
    })
    expect(onWake).not.toHaveBeenCalled()

    // Sleep event - time jumped more than 5x the 5s interval (>25s)
    act(() => {
      currentTime += 30000
      jest.advanceTimersByTime(5000)
    })
    expect(onWake).toHaveBeenCalled()
  })

  it("should use custom sleep threshold multiplier when provided", () => {
    const onWake = jest.fn()
    renderHook(() =>
      useSleepDetection(onWake, {
        checkIntervalMs: 10000,
        sleepThresholdMultiplier: 3,
      }),
    )

    // With 3x multiplier, threshold is 30s. 25s elapsed should not trigger.
    act(() => {
      currentTime += 25000
      jest.advanceTimersByTime(10000)
    })
    expect(onWake).not.toHaveBeenCalled()

    // 35s elapsed should trigger (> 30s threshold)
    act(() => {
      currentTime += 35000
      jest.advanceTimersByTime(10000)
    })
    expect(onWake).toHaveBeenCalled()
  })

  it("should not run when disabled", () => {
    const onWake = jest.fn()
    renderHook(() => useSleepDetection(onWake, { enabled: false }))

    act(() => {
      currentTime += 300000
      jest.advanceTimersByTime(60000)
    })

    expect(onWake).not.toHaveBeenCalled()
  })

  it("should not wake after unmount", () => {
    const onWake = jest.fn()
    const { unmount } = renderHook(() => useSleepDetection(onWake))

    unmount()

    // A gap that would wake a mounted hook must fire nothing once unmounted.
    act(() => {
      currentTime += 600000
      jest.advanceTimersByTime(30000)
    })
    expect(onWake).not.toHaveBeenCalled()
  })

  it("should stop listening for visibilitychange after unmount", () => {
    const onWake = jest.fn()
    const { unmount } = renderHook(() => useSleepDetection(onWake))

    act(() => {
      changeVisibility("hidden")
    })
    unmount()

    currentTime += 600000
    act(() => {
      changeVisibility("visible")
    })
    expect(onWake).not.toHaveBeenCalled()
  })

  it("should update onWake callback when it changes", () => {
    const onWake1 = jest.fn()
    const onWake2 = jest.fn()

    const { rerender } = renderHook(
      ({ callback }) => useSleepDetection(callback),
      { initialProps: { callback: onWake1 } },
    )

    rerender({ callback: onWake2 })

    // Simulate sleep event
    act(() => {
      currentTime += 200000
      jest.advanceTimersByTime(30000)
    })

    expect(onWake1).not.toHaveBeenCalled()
    expect(onWake2).toHaveBeenCalled()
  })

  it("should detect sleep-while-hidden via visibilitychange", () => {
    const onWake = jest.fn()
    renderHook(() => useSleepDetection(onWake))

    // Tab goes hidden — hook records hiddenAt timestamp
    act(() => {
      changeVisibility("hidden")
    })

    // Device sleeps — CPU suspended, no interval ticks fire, time passes
    currentTime += 600000 // 10 min

    // User opens laptop, tab becomes visible — no ticks fired while hidden
    // so visibilitychange detects the gap as real sleep
    act(() => {
      changeVisibility("visible")
    })
    expect(onWake).toHaveBeenCalledTimes(1)
  })

  it("should fire onWake when real sleep happens with tab visible", () => {
    const onWake = jest.fn()
    renderHook(() => useSleepDetection(onWake))

    // Laptop lid closes and reopens — tab stays visible the whole time
    // (common when external monitor keeps session alive)
    act(() => {
      currentTime += 600000 // 10 min gap
      jest.advanceTimersByTime(30000)
    })
    expect(onWake).toHaveBeenCalledTimes(1)
  })

  /**
   * Simulates the exact production scenario that caused premium EC2 instances
   * to run overnight (2026-04-01).
   *
   * Chromium throttles setInterval in background tabs to fire at most once per
   * ~60 seconds. With the old threshold (10s interval × 2 = 20s), every
   * throttled tick appeared as a "sleep wake" and fired recordActivity() →
   * sendPremiumHeartbeat() → Lambda update_activity, keeping last_activity
   * perpetually fresh and preventing the cleanup Lambda from ever marking
   * assignments as stale.
   *
   * This test runs 8 hours of simulated Chromium background-tab throttling
   * and verifies that zero false wake events are produced.
   */
  it("should produce zero false wakes during 8h of Chromium background throttling", () => {
    const onWake = jest.fn()
    renderHook(() => useSleepDetection(onWake))

    // User switches to another tab — Chromium throttles timers to ~60s
    setVisibility("hidden")

    // Simulate 8 hours of background throttling:
    // The real setInterval(30000) fires every ~60s in a background tab.
    // Each tick sees a ~60s gap (> 30s interval but < 150s threshold).
    const EIGHT_HOURS_MS = 8 * 60 * 60 * 1000
    const CHROMIUM_THROTTLED_TICK_MS = 60000

    for (
      let elapsed = 0;
      elapsed < EIGHT_HOURS_MS;
      elapsed += CHROMIUM_THROTTLED_TICK_MS
    ) {
      act(() => {
        currentTime += CHROMIUM_THROTTLED_TICK_MS
        jest.advanceTimersByTime(CHROMIUM_THROTTLED_TICK_MS)
      })
    }

    // After 8 hours of background throttling: zero false wakes
    expect(onWake).not.toHaveBeenCalled()

    // User returns — visibilitychange fires. lastTick was kept fresh by
    // throttled ticks (~60s apart), so gap is only ~60s — below threshold.
    act(() => {
      changeVisibility("visible")
    })
    expect(onWake).not.toHaveBeenCalled()
  })
})
