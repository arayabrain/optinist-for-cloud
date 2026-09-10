import { Provider } from "react-redux"
import { MemoryRouter, Route, Routes } from "react-router-dom"

import configureStore from "redux-mock-store"

import { describe, it, expect } from "@jest/globals"
import { render, screen, fireEvent } from "@testing-library/react"

import LegalPageLayout from "components/common/LegalPageLayout"

const store = configureStore([])({ user: { currentUser: null } })

const renderAt = (entries: string[]) =>
  render(
    <Provider store={store}>
      <MemoryRouter initialEntries={entries}>
        <Routes>
          <Route path="/" element={<div>landing page</div>} />
          <Route
            path="/terms"
            element={
              <LegalPageLayout
                title="Terms of Service"
                lastUpdated="2026-08-01"
              >
                <p>body</p>
              </LegalPageLayout>
            }
          />
          <Route path="/account" element={<div>account page</div>} />
        </Routes>
      </MemoryRouter>
    </Provider>,
  )

const clickBack = () =>
  fireEvent.click(screen.getByRole("button", { name: "Back" }))

describe("LegalPageLayout", () => {
  it("renders the last updated date", () => {
    renderAt(["/terms"])
    expect(screen.getByText("Last updated: 2026-08-01")).toBeTruthy()
  })

  it("Back returns to the previous in-app page", () => {
    renderAt(["/account", "/terms"])
    clickBack()
    expect(screen.getByText("account page")).toBeTruthy()
  })

  it("Back falls back to the landing page when opened in a new tab", () => {
    renderAt(["/terms"])
    clickBack()
    expect(screen.getByText("landing page")).toBeTruthy()
  })
})
