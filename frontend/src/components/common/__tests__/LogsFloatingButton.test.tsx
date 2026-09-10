/**
 * The floating Show Logs button.
 *
 * Driven against a real reducer rather than a mock store, so the click, the
 * action and the selector the layout gates the dialog on are all exercised.
 */

import { Provider } from "react-redux"

import { describe, it, expect } from "@jest/globals"
import { configureStore } from "@reduxjs/toolkit"
import { render, screen, fireEvent } from "@testing-library/react"

import { LogsFloatingButton } from "components/common/LogsFloatingButton"
import { selectLogsModalIsOpen } from "store/slice/LogsModal/LogsModalSelectors"
import logsModalReducer from "store/slice/LogsModal/LogsModalSlice"
import type { RootState } from "store/store"

const renderButton = () => {
  const store = configureStore({ reducer: { logsModal: logsModalReducer } })
  render(
    <Provider store={store}>
      <LogsFloatingButton />
    </Provider>,
  )
  return store
}

const isOpen = (state: unknown) => selectLogsModalIsOpen(state as RootState)

describe("LogsFloatingButton (416)", () => {
  it("opens the logs dialog when clicked", () => {
    const store = renderButton()
    expect(isOpen(store.getState())).toBe(false)

    fireEvent.click(screen.getByRole("button", { name: "show logs" }))

    expect(isOpen(store.getState())).toBe(true)
  })

  it("stays open when clicked again", () => {
    const store = renderButton()
    const button = screen.getByRole("button", { name: "show logs" })

    fireEvent.click(button)
    fireEvent.click(button)

    expect(isOpen(store.getState())).toBe(true)
  })
})
