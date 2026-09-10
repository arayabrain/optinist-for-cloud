// Re-arm (half-open circuit) configuration for instance-unreachable recovery.
export const INITIAL_PROBE_DELAY_MS = 30000
export const MAX_PROBE_DELAY_MS = 300000
export const PROBE_BACKOFF_MULTIPLIER = 2
export const MAX_FAILED_PROBES = 5

// Window to absorb transient 5xx after a dedicated handoff — the dedicated ALB
// target group can flap while warming up. Every 5xx within the window is
// suppressed (not just the first); the window expires naturally after this many ms.
export const DEDICATED_HANDOFF_GRACE_MS = 15000

// localStorage fallback because BroadcastChannel doesn't replay past messages to new tabs.
export const LS_UNREACHABLE_SNAPSHOT = "premium_unreachable_snapshot"
export const UNREACHABLE_SNAPSHOT_TTL_MS = 60 * 60 * 1000
