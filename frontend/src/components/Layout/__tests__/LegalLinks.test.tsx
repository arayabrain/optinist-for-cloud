/* eslint-disable no-undef */
import "@testing-library/jest-dom"
import { ReactNode } from "react"
import { Provider } from "react-redux"
import { MemoryRouter } from "react-router-dom"

import configureMockStore from "redux-mock-store"

import { describe, it } from "@jest/globals"
import { render, screen, fireEvent } from "@testing-library/react"

import Header from "components/Layout/Header"
import Tooltips from "components/Layout/Tooltips"

jest.mock("notistack", () => ({
  useSnackbar: () => ({ enqueueSnackbar: jest.fn() }),
}))

jest.mock("components/Layout/Profile", () => {
  const MockProfile = () => <div>profile</div>
  return MockProfile
})

const mockStore = configureMockStore([])

const renderWithStore = (ui: ReactNode) =>
  render(
    <Provider
      store={mockStore({
        mode: { mode: false, loading: false },
        pipeline: { run: { status: "" } },
        workspace: { currentWorkspace: { workspaceId: 1, selectedTab: 0 } },
      })}
    >
      <MemoryRouter>{ui}</MemoryRouter>
    </Provider>,
  )

describe("legal links in the documentation menu", () => {
  it("links to the privacy and terms pages", () => {
    renderWithStore(<Tooltips />)
    fireEvent.click(screen.getByTestId("MenuBookIcon").closest("button")!)

    expect(
      screen.getByRole("menuitem", { name: "Privacy Policy" }),
    ).toHaveAttribute("href", "/privacy")
    expect(
      screen.getByRole("menuitem", { name: "Terms of Service" }),
    ).toHaveAttribute("href", "/terms")
  })

  it("does not surface the legal links in the header bar", () => {
    const { container } = renderWithStore(
      <Header handleDrawerOpen={jest.fn()} />,
    )

    expect(container.querySelector("a[href='/terms']")).toBeNull()
    expect(container.querySelector("a[href='/privacy']")).toBeNull()
  })
})
