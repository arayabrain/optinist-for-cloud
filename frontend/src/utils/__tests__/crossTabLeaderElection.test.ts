/**
 * Tests for the real CrossTabLeaderElection.
 *
 * Only the leader tab polls the premium endpoints, so every "one request per
 * user, not one per tab" claim rests on this class. The suite drives the real
 * localStorage protocol: who claims, who stands down, when a silent leader is
 * taken over, and that a destroyed instance stops participating.
 */

import {
  afterEach,
  beforeEach,
  describe,
  expect,
  jest,
  test,
} from "@jest/globals"

import { CrossTabLeaderElection } from "utils/crossTabSync"

const LEADER_KEY = "premium_poll_leader"

// The protocol's two timings, asserted rather than imported: a tab that heartbeats
// slower than another tab's takeover window loses leadership it still believes it has.
const HEARTBEAT_MS = 2000
const TIMEOUT_MS = 5000

type Election = {
  election: CrossTabLeaderElection
  becomeLeader: jest.Mock<void, []>
  loseLeadership: jest.Mock<void, []>
}

const live: CrossTabLeaderElection[] = []

const newElection = (): Election => {
  const becomeLeader = jest.fn<void, []>()
  const loseLeadership = jest.fn<void, []>()
  const election = new CrossTabLeaderElection(becomeLeader, loseLeadership)
  live.push(election)
  return { election, becomeLeader, loseLeadership }
}

const storedLeader = (): { timestamp: number; tabId: string } | null => {
  const raw = localStorage.getItem(LEADER_KEY)
  return raw ? JSON.parse(raw) : null
}

/** Writes the entry another tab would have left behind. */
const seedForeignLeader = (ageMs: number, tabId = "other-tab") => {
  localStorage.setItem(
    LEADER_KEY,
    JSON.stringify({ timestamp: Date.now() - ageMs, tabId }),
  )
}

const fireStorageEvent = (newValue: string | null, key = LEADER_KEY) => {
  window.dispatchEvent(new StorageEvent("storage", { key, newValue }))
}

describe("CrossTabLeaderElection", () => {
  beforeEach(() => {
    jest.useFakeTimers()
    localStorage.clear()
  })

  afterEach(() => {
    live.forEach((e) => e.destroy())
    live.length = 0
    jest.clearAllTimers()
    jest.useRealTimers()
  })

  describe("claiming leadership", () => {
    test("an unclaimed key is taken, recorded and announced once", () => {
      const { election, becomeLeader } = newElection()

      expect(election.getIsLeader()).toBe(true)
      expect(becomeLeader).toHaveBeenCalledTimes(1)
      expect(storedLeader()?.timestamp).toBe(Date.now())
      expect(storedLeader()?.tabId).toEqual(expect.any(String))
    })

    test("a corrupt entry is treated as unclaimed", () => {
      localStorage.setItem(LEADER_KEY, "not-json")

      const { election, becomeLeader } = newElection()

      expect(election.getIsLeader()).toBe(true)
      expect(becomeLeader).toHaveBeenCalledTimes(1)
    })

    test("a second tab stands down while the first is heartbeating", () => {
      const first = newElection()
      const firstTabId = storedLeader()!.tabId

      const second = newElection()

      expect(first.election.getIsLeader()).toBe(true)
      expect(second.election.getIsLeader()).toBe(false)
      expect(second.becomeLeader).not.toHaveBeenCalled()
      // The key still belongs to the first tab, so only it will poll.
      expect(storedLeader()?.tabId).toBe(firstTabId)
    })
  })

  // The takeover window is exactly TIMEOUT_MS: one millisecond either side of it
  // decides whether a new tab starts polling alongside the incumbent.
  describe("takeover window", () => {
    test("does not take over a leader one millisecond inside the window", () => {
      seedForeignLeader(TIMEOUT_MS - 1)

      const { election, becomeLeader } = newElection()

      expect(election.getIsLeader()).toBe(false)
      expect(becomeLeader).not.toHaveBeenCalled()
      expect(storedLeader()?.tabId).toBe("other-tab")
    })

    test("takes over a leader one millisecond past the window", () => {
      seedForeignLeader(TIMEOUT_MS + 1)

      const { election, becomeLeader } = newElection()

      expect(election.getIsLeader()).toBe(true)
      expect(becomeLeader).toHaveBeenCalledTimes(1)
      expect(storedLeader()?.tabId).not.toBe("other-tab")
    })

    test("a follower keeps re-checking and takes over once the leader goes silent", () => {
      seedForeignLeader(0)
      const { election, becomeLeader } = newElection()
      expect(election.getIsLeader()).toBe(false)

      // Long enough that the incumbent's entry is well past the window.
      jest.advanceTimersByTime(TIMEOUT_MS * 3)

      expect(election.getIsLeader()).toBe(true)
      expect(becomeLeader).toHaveBeenCalledTimes(1)
    })

    test("a follower never takes over while the leader keeps heartbeating", () => {
      seedForeignLeader(0)
      const { election, becomeLeader } = newElection()

      for (let tick = 0; tick < 20; tick++) {
        jest.advanceTimersByTime(HEARTBEAT_MS)
        seedForeignLeader(0)
      }

      expect(election.getIsLeader()).toBe(false)
      expect(becomeLeader).not.toHaveBeenCalled()
    })
  })

  describe("heartbeat", () => {
    test("the leader refreshes its entry every heartbeat and no sooner", () => {
      const { election } = newElection()
      const first = storedLeader()!.timestamp

      jest.advanceTimersByTime(HEARTBEAT_MS - 1)
      expect(storedLeader()!.timestamp).toBe(first)

      jest.advanceTimersByTime(1)
      expect(storedLeader()!.timestamp).toBe(first + HEARTBEAT_MS)
      expect(election.getIsLeader()).toBe(true)
    })

    test("a follower writes nothing", () => {
      seedForeignLeader(0)
      newElection()
      const before = storedLeader()!.timestamp

      jest.advanceTimersByTime(HEARTBEAT_MS * 2)

      expect(storedLeader()!.timestamp).toBe(before)
      expect(storedLeader()!.tabId).toBe("other-tab")
    })
  })

  describe("losing leadership to another tab", () => {
    test("another tab's claim stands the leader down and stops its heartbeat", () => {
      const { election, loseLeadership } = newElection()
      expect(election.getIsLeader()).toBe(true)

      fireStorageEvent(
        JSON.stringify({ timestamp: Date.now(), tabId: "other-tab" }),
      )

      expect(election.getIsLeader()).toBe(false)
      expect(loseLeadership).toHaveBeenCalledTimes(1)

      // No further writes: two leaders heartbeating would keep both polling.
      localStorage.setItem(LEADER_KEY, "sentinel")
      jest.advanceTimersByTime(HEARTBEAT_MS * 2)
      expect(localStorage.getItem(LEADER_KEY)).toBe("sentinel")
    })

    test("an event echoing the leader's own tab id changes nothing", () => {
      const { election, loseLeadership } = newElection()
      const ownTabId = storedLeader()!.tabId

      fireStorageEvent(
        JSON.stringify({ timestamp: Date.now(), tabId: ownTabId }),
      )

      expect(election.getIsLeader()).toBe(true)
      expect(loseLeadership).not.toHaveBeenCalled()
    })

    test("events for other keys and malformed payloads are ignored", () => {
      const { election, loseLeadership } = newElection()

      fireStorageEvent(
        JSON.stringify({ timestamp: Date.now(), tabId: "other-tab" }),
        "some_other_key",
      )
      fireStorageEvent("not-json")

      expect(election.getIsLeader()).toBe(true)
      expect(loseLeadership).not.toHaveBeenCalled()
    })

    test("a removed key lets a follower claim leadership immediately", () => {
      seedForeignLeader(0)
      const { election, becomeLeader } = newElection()
      expect(election.getIsLeader()).toBe(false)

      localStorage.removeItem(LEADER_KEY)
      fireStorageEvent(null)

      expect(election.getIsLeader()).toBe(true)
      expect(becomeLeader).toHaveBeenCalledTimes(1)
    })
  })

  describe("teardown", () => {
    test("destroy resigns the key, stops the heartbeat and detaches the listeners", () => {
      const { election, becomeLeader, loseLeadership } = newElection()

      election.destroy()

      expect(election.getIsLeader()).toBe(false)
      expect(localStorage.getItem(LEADER_KEY)).toBeNull()

      // A destroyed instance must not come back to life on either path.
      jest.advanceTimersByTime(TIMEOUT_MS * 4)
      fireStorageEvent(null)

      expect(election.getIsLeader()).toBe(false)
      expect(localStorage.getItem(LEADER_KEY)).toBeNull()
      expect(becomeLeader).toHaveBeenCalledTimes(1)
      expect(loseLeadership).not.toHaveBeenCalled()
    })

    test("destroying a follower leaves the leader's entry alone", () => {
      seedForeignLeader(0)
      const { election } = newElection()

      election.destroy()

      expect(storedLeader()?.tabId).toBe("other-tab")
    })

    test("page unload resigns leadership so the next tab can take over at once", () => {
      const { election } = newElection()

      window.dispatchEvent(new Event("beforeunload"))

      expect(election.getIsLeader()).toBe(false)
      expect(localStorage.getItem(LEADER_KEY)).toBeNull()
    })
  })
})
