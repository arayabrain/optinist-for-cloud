/* eslint-disable no-undef */
import "@testing-library/jest-dom"
import { Provider } from "react-redux"
import { MemoryRouter } from "react-router-dom"

import configureMockStore from "redux-mock-store"

import { describe, it } from "@jest/globals"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"

import Tooltips from "components/Layout/Tooltips"

jest.mock("notistack", () => ({
  useSnackbar: () => ({
    enqueueSnackbar: jest.fn(),
  }),
}))

const mockStore = configureMockStore([])

const renderWithTab = (
  workspaceId: number | undefined,
  selectedTab: number,
) => {
  const store = mockStore({
    workspace: { currentWorkspace: { workspaceId, selectedTab } },
  })
  return render(
    <Provider store={store}>
      <MemoryRouter>
        <Tooltips />
      </MemoryRouter>
    </Provider>,
  )
}

const openDocumentationMenu = () => {
  fireEvent.click(screen.getByTestId("MenuBookIcon").closest("button")!)
}

describe("Tooltips documentation menu", () => {
  it("disables 'Import Sample Data' when not on the Record tab", () => {
    renderWithTab(1, 0)
    openDocumentationMenu()

    const item = screen.getByText("Import Sample Data").closest("li")
    expect(item).toHaveAttribute("aria-disabled", "true")
  })

  it("enables 'Import Sample Data' on the Record tab", () => {
    renderWithTab(1, 2)
    openDocumentationMenu()

    const item = screen.getByText("Import Sample Data").closest("li")
    expect(item).not.toHaveAttribute("aria-disabled", "true")
  })

  it("closes the menu when opening the import dialog so its backdrop stops blocking clicks", async () => {
    renderWithTab(1, 2)
    openDocumentationMenu()

    fireEvent.click(screen.getByText("Import Sample Data"))

    expect(screen.getByText("Import sample data?")).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByText("User Guide")).not.toBeInTheDocument(),
    )
  })
})
