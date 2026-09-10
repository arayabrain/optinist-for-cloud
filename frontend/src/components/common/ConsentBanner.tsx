import { FC, useState } from "react"
import { useSelector } from "react-redux"

import Box from "@mui/material/Box"
import Button from "@mui/material/Button"
import Paper from "@mui/material/Paper"
import Typography from "@mui/material/Typography"

import { Z_INDEX } from "const/Layout"
import { selectModeStandalone } from "store/slice/Standalone/StandaloneSeclector"
import {
  ConsentDecision,
  getAnalyticsConsent,
  isGtmEnabled,
  setAnalyticsConsent,
} from "utils/analytics"

const ConsentBanner: FC = () => {
  const isStandalone = useSelector(selectModeStandalone)
  const [decided, setDecided] = useState(() => getAnalyticsConsent() !== null)

  const decide = (decision: ConsentDecision) => {
    setAnalyticsConsent(decision)
    setDecided(true)
  }

  // Only once the backend has confirmed hosted mode: standalone is a local
  // install, and an unresolved mode means the app itself is not usable yet.
  if (!isGtmEnabled() || isStandalone !== false || decided) return null

  return (
    <Paper
      elevation={8}
      role="region"
      aria-label="Analytics cookie consent"
      aria-live="polite"
      data-testid="consent-banner"
      sx={{
        position: "fixed",
        // Bottom-right, one line tall: keeps the notice clear of the fixed
        // controls and toasts that sit in the bottom-left corner.
        bottom: 16,
        right: 16,
        maxWidth: "calc(100vw - 32px)",
        zIndex: Z_INDEX.CONSENT_BANNER,
        px: 2,
        py: 1.5,
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: 2,
      }}
    >
      <Typography variant="body2">
        We use analytics cookies to measure site usage.
      </Typography>
      <Box sx={{ display: "flex", gap: 1, ml: "auto" }}>
        {/* Identical styling on both: a de-emphasised Decline is a dark pattern. */}
        <Button
          size="small"
          variant="outlined"
          onClick={() => decide("denied")}
          data-testid="consent-decline"
        >
          Decline
        </Button>
        <Button
          size="small"
          variant="outlined"
          onClick={() => decide("granted")}
          data-testid="consent-accept"
        >
          Accept
        </Button>
      </Box>
    </Paper>
  )
}

export default ConsentBanner
