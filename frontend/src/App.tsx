import { FC, useEffect, useState } from "react"
import { useDispatch, useSelector } from "react-redux"
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom"

import { isAxiosError } from "axios"
import { SnackbarProvider, SnackbarKey, useSnackbar } from "notistack"

import Close from "@mui/icons-material/Close"
import IconButton from "@mui/material/IconButton"

import BackendUnavailable from "components/common/BackendUnavailable"
import ErrorBoundary from "components/common/ErrorBoundary"
import Loading from "components/common/Loading"
import RouteChangeTracker from "components/common/RouteChangeTracker"
import ScrollToTop from "components/common/ScrollToTop"
import Layout from "components/Layout"
import { RETRY_MAX_COUNT, RETRY_WAIT, RETRY_WAIT_LONG } from "const/Mode"
import Account from "pages/Account"
import AccountDelete from "pages/AccountDelete"
import AccountManager from "pages/AccountManager"
import Dashboard from "pages/Dashboard"
import Dataview from "pages/Dataview"
import InvoicesPage from "pages/Invoice"
import LandingPage from "pages/LandingPage"
import Login from "pages/Login"
import Privacy from "pages/Privacy"
import PublicDataview from "pages/PublicDataview"
import RegistrationForm from "pages/Register/MainRegistration"
import ResetPassword from "pages/ResetPassword"
import SubscriptionPage from "pages/Subscription"
import Failed from "pages/Subscription/failed"
import Thanks from "pages/Subscription/thanks"
import Terms from "pages/Terms"
import Workspaces from "pages/Workspace"
import Workspace from "pages/Workspace/Workspace"
import { getModeStandalone } from "store/slice/Standalone/StandaloneActions"
import {
  selectLoading,
  selectModeStandalone,
} from "store/slice/Standalone/StandaloneSeclector"
import { AppDispatch } from "store/store"

/**
 * Returns whether the error from getModeStandalone should be retried.
 *
 * - Treat as "backend down" (retry): network/timeout errors and HTTP 5xx.
 * - Treat as terminal (no retry): HTTP 4xx. Non-axios errors are retried for safety.
 */
const isRetryableBackendError = (error: unknown): boolean => {
  if (!isAxiosError(error)) return true
  if (!error.response) return true
  const status = error.response.status
  return status >= 500 && status < 600
}

const App: FC = () => {
  const dispatch = useDispatch<AppDispatch>()
  const isStandalone = useSelector(selectModeStandalone)
  const loading = useSelector(selectLoading)
  // Show the "backend unavailable" screen instead of the normal layout.
  const [showBackendError, setShowBackendError] = useState(false)
  // false = background polling has stopped (4xx); user must reload manually.
  const [isRetrying, setIsRetrying] = useState(true)

  useEffect(() => {
    let cancelled = false
    let timerId: ReturnType<typeof setTimeout> | undefined
    let retryCount = 0

    const getMode = () => {
      dispatch(getModeStandalone())
        .unwrap()
        .then(() => {
          if (cancelled) return
          // Recovered: hide the error screen.
          setShowBackendError(false)
          setIsRetrying(true)
        })
        .catch((error: unknown) => {
          if (cancelled) return
          if (!isRetryableBackendError(error)) {
            // Terminal error (4xx): stop polling, require manual reload.
            setShowBackendError(true)
            setIsRetrying(false)
            return
          }
          retryCount += 1
          const reachedMax = retryCount >= RETRY_MAX_COUNT
          if (reachedMax) {
            setShowBackendError(true)
          }
          const wait = reachedMax ? RETRY_WAIT_LONG : RETRY_WAIT
          timerId = setTimeout(() => {
            getMode()
          }, wait)
        })
    }

    getMode()

    return () => {
      cancelled = true
      if (timerId !== undefined) clearTimeout(timerId)
    }
    //eslint-disable-next-line
  }, [])

  if (showBackendError) {
    return <BackendUnavailable isRetrying={isRetrying} />
  }

  return loading ? (
    <Loading loading={true} />
  ) : (
    <ErrorBoundary>
      <SnackbarProvider
        maxSnack={5}
        preventDuplicate={true}
        action={(snackbarKey) => (
          <SnackbarCloseButton snackbarKey={snackbarKey} />
        )}
        style={{ maxWidth: "600px" }}
      >
        <BrowserRouter>
          <ScrollToTop />
          <RouteChangeTracker />
          <Routes>
            {/* Landing page - outside Layout */}
            <Route path="/" element={<LandingPage />} />

            {/* All other routes wrapped in Layout */}
            <Route
              path="*"
              element={
                <Layout>
                  {isStandalone ? (
                    <Routes>
                      <Route path="/" element={<Workspace />} />
                      <Route path="*" element={<Navigate replace to="/" />} />
                    </Routes>
                  ) : (
                    <Routes>
                      {/* Public routes */}
                      <Route path="/public" element={<PublicDataview />} />
                      <Route
                        path="/account-deleted"
                        element={<AccountDelete />}
                      />
                      <Route path="/login" element={<Login />} />
                      <Route path="/register" element={<RegistrationForm />} />
                      <Route
                        path="/reset-password"
                        element={<ResetPassword />}
                      />
                      <Route path="/terms" element={<Terms />} />
                      <Route path="/privacy" element={<Privacy />} />

                      {/* Authenticated routes */}
                      <Route path="/dashboard" element={<Dashboard />} />
                      <Route path="/account" element={<Account />} />
                      <Route
                        path="/account-manager"
                        element={<AccountManager />}
                      />

                      <Route path="/dataview">
                        <Route path="" element={<Dataview />} />
                        <Route path=":workspaceId" element={<Dataview />} />
                      </Route>

                      <Route path="/workspaces">
                        <Route path="" element={<Workspaces />} />
                        <Route path=":workspaceId" element={<Workspace />} />
                      </Route>

                      <Route path="/subscription/thanks" element={<Thanks />} />
                      <Route path="/subscription/failed" element={<Failed />} />
                      <Route
                        path="/subscription"
                        element={<SubscriptionPage />}
                      />
                      <Route
                        path="/subscription/manage"
                        element={<InvoicesPage />}
                      />

                      {/* Catch-all */}
                      <Route path="*" element={<Navigate replace to="/" />} />
                    </Routes>
                  )}
                </Layout>
              }
            />
          </Routes>
        </BrowserRouter>
      </SnackbarProvider>
    </ErrorBoundary>
  )
}

const SnackbarCloseButton: FC<{ snackbarKey: SnackbarKey }> = ({
  snackbarKey,
}) => {
  const { closeSnackbar } = useSnackbar()
  return (
    <IconButton onClick={() => closeSnackbar(snackbarKey)} size="large">
      <Close style={{ color: "white" }} />
    </IconButton>
  )
}

export default App
