import { FC, useEffect, useRef } from "react"
import { useSelector } from "react-redux"
import { useLocation } from "react-router-dom"

import { selectModeStandalone } from "store/slice/Standalone/StandaloneSeclector"
import { normalizePath, trackEvent } from "utils/analytics"

const RouteChangeTracker: FC = () => {
  const { pathname } = useLocation()
  const isStandalone = useSelector(selectModeStandalone)
  // Raw pathname, so /workspaces/12 -> /workspaces/13 still counts as a change.
  const trackedPathname = useRef<string | null>(null)

  useEffect(() => {
    if (isStandalone) return
    if (trackedPathname.current === pathname) return
    trackedPathname.current = pathname

    // page_location is supplied sanitized so the GA4 tag never has to fall back
    // to document.location.href, which carries emails and Stripe session ids.
    const pagePath = normalizePath(pathname)
    trackEvent("route_change", {
      page_path: pagePath,
      page_location: `${window.location.origin}${pagePath}`,
    })
  }, [pathname, isStandalone])

  return null
}

export default RouteChangeTracker
