import { Provider } from "react-redux"

import { createRoot } from "react-dom/client"

import "index.css"

import { ThemeProvider } from "@mui/material/styles"

import App from "App"
import ConsentBanner from "components/common/ConsentBanner"
import ErrorBoundary from "components/common/ErrorBoundary"
import reportWebVitals from "reportWebVitals"
import { store } from "store/store"
import { theme } from "Theme"
import { initAnalyticsConsent } from "utils/analytics"
import { initChunkReloadHandler } from "utils/chunkLoadReload"
import { initErrorReporter } from "utils/errorReporter"

initChunkReloadHandler()
initErrorReporter()
initAnalyticsConsent()

const root = createRoot(document.getElementById("root")!)

root.render(
  <Provider store={store}>
    <ThemeProvider theme={theme}>
      <App />
      <ErrorBoundary fallback={<></>}>
        <ConsentBanner />
      </ErrorBoundary>
    </ThemeProvider>
  </Provider>,
)

// If you want to start measuring performance in your app, pass a function
// to log results (for example: reportWebVitals(console.log))
// or send to an analytics endpoint. Learn more: https://bit.ly/CRA-vitals
reportWebVitals()
