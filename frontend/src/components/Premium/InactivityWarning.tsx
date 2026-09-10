/**
 * Inactivity Warning Component
 *
 * Shows a warning snackbar when premium users have been idle for the warning
 * threshold, counting down the remaining time to the auto-release threshold.
 */

import React, { useEffect, useRef, useState } from "react"

import { AxiosError } from "axios"

import { Alert, Button, Snackbar } from "@mui/material"

import { PremiumTiming } from "const/Subscription"
import { usePremiumAssignment } from "contexts/PremiumAssignmentContext"
import { useLogout } from "hooks/useLogout"

const IDLE_MINUTES = PremiumTiming.INACTIVITY_WARNING_MINUTES
const COUNTDOWN_MINUTES =
  PremiumTiming.INACTIVITY_RELEASE_MINUTES - IDLE_MINUTES

const InactivityWarning: React.FC = () => {
  const { showInactivityWarning, dismissInactivityWarning, recordActivity } =
    usePremiumAssignment()
  const { performLogout } = useLogout()
  const [countdown, setCountdown] = useState<number>(COUNTDOWN_MINUTES)

  // Countdown timer for the warning
  useEffect(() => {
    if (!showInactivityWarning) {
      setCountdown(COUNTDOWN_MINUTES)
      return
    }

    const countdownInterval = setInterval(() => {
      setCountdown((prev) => {
        if (prev <= 1) {
          // Time's up - the context should handle auto-release
          return 0
        }
        return prev - 1
      })
    }, PremiumTiming.WARNING_UPDATE_INTERVAL_MS)

    return () => clearInterval(countdownInterval)
  }, [showInactivityWarning])

  const logoutTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    return () => {
      if (logoutTimeoutRef.current) {
        clearTimeout(logoutTimeoutRef.current)
      }
    }
  }, [])

  const [sessionExpired, setSessionExpired] = useState(false)

  const handleStayActive = async () => {
    try {
      await recordActivity()
      dismissInactivityWarning()
    } catch (error) {
      // Check if session has expired (401 error)
      if (error instanceof AxiosError && error.response?.status === 401) {
        setSessionExpired(true)
        if (!logoutTimeoutRef.current) {
          logoutTimeoutRef.current = setTimeout(() => {
            performLogout()
          }, 2000)
        }
      } else {
        // eslint-disable-next-line no-console
        console.warn("Failed to record activity:", error)
        // Still dismiss the warning even if heartbeat failed
        dismissInactivityWarning()
      }
    }
  }

  const formatTime = (minutes: number) => {
    const hours = Math.floor(minutes / 60)
    const mins = minutes % 60
    if (hours > 0) {
      return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`
    }
    return `${mins}m`
  }

  return (
    <Snackbar
      open={showInactivityWarning}
      anchorOrigin={{ vertical: "top", horizontal: "center" }}
      // Don't auto-hide - user must interact
    >
      <Alert
        severity={sessionExpired ? "error" : "warning"}
        variant="filled"
        action={
          !sessionExpired && (
            <Button
              color="inherit"
              size="small"
              onClick={handleStayActive}
              sx={{ fontWeight: "bold" }}
            >
              Stay Active
            </Button>
          )
        }
        sx={{
          minWidth: "400px",
          "& .MuiAlert-message": {
            fontSize: "14px",
            lineHeight: 1.4,
          },
        }}
      >
        {sessionExpired ? (
          <>
            <strong>Session Expired</strong>
            <br />
            Your session has expired. Redirecting to login...
          </>
        ) : (
          <>
            <strong>Premium Instance Inactivity Warning</strong>
            <br />
            You&apos;ve been inactive for {formatTime(IDLE_MINUTES)}. Your
            premium instance will be automatically released in{" "}
            {formatTime(countdown)} if no activity is detected.
            <br />
            <small>
              Click &quot;Stay Active&quot; or interact with the page to keep
              your instance active.
            </small>
          </>
        )}
      </Alert>
    </Snackbar>
  )
}

export default InactivityWarning
