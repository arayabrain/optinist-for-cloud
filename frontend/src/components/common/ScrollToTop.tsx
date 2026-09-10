import { FC, useEffect } from "react"
import { useLocation, useNavigationType } from "react-router-dom"

// Reset the window scroll position to the top on a new navigation. React Router
// keeps the previous scroll offset across client-side navigations, which
// otherwise makes a new page open partway down (e.g. when navigating from the
// bottom of a long page).
const ScrollToTop: FC = () => {
  const { pathname } = useLocation()
  const navigationType = useNavigationType()

  useEffect(() => {
    // Leave back/forward (POP) to the browser's native scroll restoration;
    // only reset to the top for new navigations (link clicks, redirects).
    if (navigationType !== "POP") {
      window.scrollTo(0, 0)
    }
  }, [pathname, navigationType])

  return null
}

export default ScrollToTop
