import { describe, it, expect } from "@jest/globals"

import {
  INITIAL_PROBE_DELAY_MS,
  MAX_FAILED_PROBES,
  MAX_PROBE_DELAY_MS,
  PROBE_BACKOFF_MULTIPLIER,
} from "contexts/premium/unreachableConstants"
import {
  INITIAL_UNREACHABLE_STATE,
  computeNextProbeDelayMs,
  computeProbeFailure,
  hasReachedProbeCap,
  shouldClearUnreachableForAssignment,
  shouldFlipToUnreachable,
  shouldHydrateFromSnapshot,
  shouldPoll,
  unreachableMachineReducer,
} from "contexts/premium/unreachableMachine"

// Pure helpers, no React. Pins the polling gate and state-machine guards
// without needing to mount the provider.

describe("shouldPoll", () => {
  const premium = true
  const leader = true
  const dedicated = { assigned: true, is_shared: false }
  const shared = { assigned: true, is_shared: true }

  it("does not poll when dedicated and healthy", () => {
    expect(shouldPoll(premium, leader, dedicated, false)).toBe(false)
  })

  it("polls when dedicated but instance is unreachable", () => {
    expect(shouldPoll(premium, leader, dedicated, true)).toBe(true)
  })

  it("polls while on a shared instance", () => {
    expect(shouldPoll(premium, leader, shared, false)).toBe(true)
  })

  it("does not poll when no assignment exists", () => {
    expect(shouldPoll(premium, leader, null, false)).toBe(false)
  })

  it("does not poll when tab is not leader", () => {
    expect(shouldPoll(premium, false, dedicated, true)).toBe(false)
  })

  it("does not poll when user is not premium", () => {
    expect(shouldPoll(false, leader, dedicated, true)).toBe(false)
  })

  it("does not poll for shared when not leader", () => {
    expect(shouldPoll(premium, false, shared, false)).toBe(false)
  })
})

describe("shouldFlipToUnreachable", () => {
  it("flips when dedicated and not already unreachable", () => {
    expect(
      shouldFlipToUnreachable({ assigned: true, is_shared: false }, false),
    ).toBe(true)
  })

  it("does not flip on a shared assignment", () => {
    expect(
      shouldFlipToUnreachable({ assigned: true, is_shared: true }, false),
    ).toBe(false)
  })

  it("does not flip when no assignment", () => {
    expect(shouldFlipToUnreachable(null, false)).toBe(false)
  })

  it("does not flip when assignment not yet assigned", () => {
    expect(
      shouldFlipToUnreachable({ assigned: false, is_shared: false }, false),
    ).toBe(false)
  })

  it("does not re-flip when already unreachable", () => {
    expect(
      shouldFlipToUnreachable({ assigned: true, is_shared: false }, true),
    ).toBe(false)
  })
})

describe("computeNextProbeDelayMs", () => {
  // Literals, not expressions over the constants: the sign-off sheet quotes a
  // wall clock ("~T0+30s, ~T0+90s"), and a derived expectation stays green
  // with the initial delay set to 3s.
  it("pins the constants the ladder is built from", () => {
    expect(INITIAL_PROBE_DELAY_MS).toBe(30_000)
    expect(MAX_PROBE_DELAY_MS).toBe(300_000)
    expect(PROBE_BACKOFF_MULTIPLIER).toBe(2)
  })

  it("walks 30s, 60s, 120s, 240s and then holds at the 300s cap", () => {
    expect([0, 1, 2, 3, 4, 5].map(computeNextProbeDelayMs)).toEqual([
      30_000, 60_000, 120_000, 240_000, 300_000, 300_000,
    ])
  })

  it("treats negative counts as zero rather than shrinking the delay", () => {
    expect(computeNextProbeDelayMs(-5)).toBe(30_000)
  })
})

describe("hasReachedProbeCap", () => {
  it("returns false below the cap", () => {
    expect(hasReachedProbeCap(0)).toBe(false)
    expect(hasReachedProbeCap(MAX_FAILED_PROBES - 1)).toBe(false)
  })

  it("returns true at the cap", () => {
    expect(hasReachedProbeCap(MAX_FAILED_PROBES)).toBe(true)
  })

  it("returns true beyond the cap", () => {
    expect(hasReachedProbeCap(MAX_FAILED_PROBES + 10)).toBe(true)
  })
})

describe("computeProbeFailure", () => {
  it("increments failed count and does not mark terminal below cap", () => {
    expect(computeProbeFailure(0)).toEqual({
      nextFailed: 1,
      nextTerminal: false,
    })
    expect(computeProbeFailure(MAX_FAILED_PROBES - 2)).toEqual({
      nextFailed: MAX_FAILED_PROBES - 1,
      nextTerminal: false,
    })
  })

  it("marks terminal on the step that reaches the cap", () => {
    expect(computeProbeFailure(MAX_FAILED_PROBES - 1)).toEqual({
      nextFailed: MAX_FAILED_PROBES,
      nextTerminal: true,
    })
  })

  it("stays terminal beyond the cap", () => {
    expect(computeProbeFailure(MAX_FAILED_PROBES + 3)).toEqual({
      nextFailed: MAX_FAILED_PROBES + 4,
      nextTerminal: true,
    })
  })
})

describe("shouldHydrateFromSnapshot", () => {
  const dedicated = {
    assigned: true,
    is_shared: false,
    instance_id: "inst-a",
  }

  it("hydrates when assignment is dedicated and snapshot matches", () => {
    expect(
      shouldHydrateFromSnapshot(dedicated, { instance_id: "inst-a" }),
    ).toBe(true)
  })

  it("does not hydrate when the snapshot is null", () => {
    expect(shouldHydrateFromSnapshot(dedicated, null)).toBe(false)
  })

  it("does not hydrate when instance_id differs", () => {
    expect(
      shouldHydrateFromSnapshot(dedicated, { instance_id: "inst-b" }),
    ).toBe(false)
  })

  it("does not hydrate when assignment is shared", () => {
    expect(
      shouldHydrateFromSnapshot(
        { assigned: true, is_shared: true, instance_id: "inst-a" },
        { instance_id: "inst-a" },
      ),
    ).toBe(false)
  })

  it("does not hydrate when assignment is not assigned", () => {
    expect(
      shouldHydrateFromSnapshot(
        { assigned: false, is_shared: false },
        { instance_id: "inst-a" },
      ),
    ).toBe(false)
  })

  it("does not hydrate when assignment is null", () => {
    expect(shouldHydrateFromSnapshot(null, { instance_id: "inst-a" })).toBe(
      false,
    )
  })

  it("matches when both sides treat missing instance_id as null", () => {
    expect(
      shouldHydrateFromSnapshot(
        { assigned: true, is_shared: false },
        { instance_id: null },
      ),
    ).toBe(true)
  })
})

describe("unreachableMachineReducer", () => {
  const degraded = {
    instanceUnreachable: true,
    unreachableSince: 5000,
    failedProbes: 2,
    isUnreachableTerminal: false,
  }

  it("CLEAR from initial state returns the same reference (noop)", () => {
    const next = unreachableMachineReducer(INITIAL_UNREACHABLE_STATE, {
      type: "CLEAR",
    })
    expect(next).toBe(INITIAL_UNREACHABLE_STATE)
  })

  it("CLEAR from a degraded state resets all fields", () => {
    expect(unreachableMachineReducer(degraded, { type: "CLEAR" })).toEqual(
      INITIAL_UNREACHABLE_STATE,
    )
  })

  it("FLIP_TO_UNREACHABLE from HEALTHY sets flag + timestamp", () => {
    expect(
      unreachableMachineReducer(INITIAL_UNREACHABLE_STATE, {
        type: "FLIP_TO_UNREACHABLE",
        since: 123,
      }),
    ).toEqual({
      ...INITIAL_UNREACHABLE_STATE,
      instanceUnreachable: true,
      unreachableSince: 123,
    })
  })

  it("FLIP_TO_UNREACHABLE is idempotent when already unreachable", () => {
    const next = unreachableMachineReducer(degraded, {
      type: "FLIP_TO_UNREACHABLE",
      since: 9999,
    })
    expect(next).toBe(degraded)
  })

  it("PROBE_FAILURE increments failedProbes and stops short of terminal", () => {
    expect(
      unreachableMachineReducer(degraded, { type: "PROBE_FAILURE" }),
    ).toEqual({
      ...degraded,
      failedProbes: 3,
      isUnreachableTerminal: false,
    })
  })

  it("PROBE_FAILURE promotes to terminal on the step that hits the cap", () => {
    const atCap = { ...degraded, failedProbes: MAX_FAILED_PROBES - 1 }
    expect(unreachableMachineReducer(atCap, { type: "PROBE_FAILURE" })).toEqual(
      {
        ...atCap,
        failedProbes: MAX_FAILED_PROBES,
        isUnreachableTerminal: true,
      },
    )
  })

  it("APPLY_PEER_UNREACHABLE overrides all four fields", () => {
    expect(
      unreachableMachineReducer(INITIAL_UNREACHABLE_STATE, {
        type: "APPLY_PEER_UNREACHABLE",
        payload: {
          unreachable_since: 100,
          failed_probes: 3,
          is_terminal: false,
        },
      }),
    ).toEqual({
      instanceUnreachable: true,
      unreachableSince: 100,
      failedProbes: 3,
      isUnreachableTerminal: false,
    })
  })

  it("APPLY_PEER_UNREACHABLE treats missing payload fields as zero/false", () => {
    const next = unreachableMachineReducer(INITIAL_UNREACHABLE_STATE, {
      type: "APPLY_PEER_UNREACHABLE",
      payload: {},
    })
    expect(next.instanceUnreachable).toBe(true)
    expect(next.failedProbes).toBe(0)
    expect(next.isUnreachableTerminal).toBe(false)
    // unreachable_since falls back to Date.now() — can't assert exact value
    expect(next.unreachableSince).not.toBeNull()
  })

  it("APPLY_PEER_PROBE_UPDATE updates only probe fields", () => {
    expect(
      unreachableMachineReducer(degraded, {
        type: "APPLY_PEER_PROBE_UPDATE",
        payload: { failed_probes: MAX_FAILED_PROBES, is_terminal: true },
      }),
    ).toEqual({
      ...degraded,
      failedProbes: MAX_FAILED_PROBES,
      isUnreachableTerminal: true,
    })
  })

  it("MANUAL_RETRY from degraded resets probe budget, keeps unreachable flag", () => {
    const terminal = {
      instanceUnreachable: true,
      unreachableSince: 5000,
      failedProbes: MAX_FAILED_PROBES,
      isUnreachableTerminal: true,
    }
    expect(
      unreachableMachineReducer(terminal, { type: "MANUAL_RETRY" }),
    ).toEqual({
      instanceUnreachable: true,
      unreachableSince: 5000,
      failedProbes: 0,
      isUnreachableTerminal: false,
    })
  })

  it("MANUAL_RETRY is a noop when not unreachable", () => {
    const next = unreachableMachineReducer(INITIAL_UNREACHABLE_STATE, {
      type: "MANUAL_RETRY",
    })
    expect(next).toBe(INITIAL_UNREACHABLE_STATE)
  })

  it("HYDRATE_FROM_SNAPSHOT fully replaces state with the snapshot", () => {
    expect(
      unreachableMachineReducer(INITIAL_UNREACHABLE_STATE, {
        type: "HYDRATE_FROM_SNAPSHOT",
        payload: {
          unreachable_since: 777,
          failed_probes: 4,
          is_terminal: false,
        },
      }),
    ).toEqual({
      instanceUnreachable: true,
      unreachableSince: 777,
      failedProbes: 4,
      isUnreachableTerminal: false,
    })
  })
})

describe("shouldClearUnreachableForAssignment", () => {
  it("clears when assignment is null", () => {
    expect(shouldClearUnreachableForAssignment(null)).toBe(true)
  })

  it("clears when assignment is shared", () => {
    expect(
      shouldClearUnreachableForAssignment({ assigned: true, is_shared: true }),
    ).toBe(true)
  })

  it("clears when assignment is not yet assigned", () => {
    expect(
      shouldClearUnreachableForAssignment({
        assigned: false,
        is_shared: false,
      }),
    ).toBe(true)
  })

  it("does not clear when assignment is dedicated and assigned", () => {
    expect(
      shouldClearUnreachableForAssignment({
        assigned: true,
        is_shared: false,
      }),
    ).toBe(false)
  })
})
