/**
 * The admin Add/Edit Account modal's field validation.
 *
 * Rendered directly rather than opened from a user row: the row's action buttons
 * sit in the grid's last column, which MUI's DataGrid virtualizes away in jsdom
 * because the viewport measures zero wide. Everything here therefore stops at
 * the `onSubmitEdit` boundary.
 */

import { describe, it, expect, beforeEach, jest } from "@jest/globals"
import "@testing-library/jest-dom"
import { render, screen, fireEvent, waitFor } from "@testing-library/react"

import { AccountEditModal, UserFormDTO } from "pages/AccountManager"

// `id` is numeric on the grid row this modal opens from
const EXISTING_USER = {
  id: 42,
  name: "Operator User",
  email: "operator@example.com",
  role_id: "OPERATOR",
}

const onSubmitEdit = jest.fn()

const field = (name: string) =>
  document.querySelector(`input[name="${name}"]`) as HTMLInputElement

const renderModal = (dataEdit?: UserFormDTO) => {
  render(
    <AccountEditModal
      open
      onSubmitEdit={onSubmitEdit}
      setOpenModal={jest.fn()}
      dataEdit={dataEdit}
    />,
  )
  return screen.getByRole("button", { name: "Ok" })
}

const change = (name: string, value: string) =>
  fireEvent.change(field(name), { target: { name, value } })

describe("Admin edit account modal", () => {
  beforeEach(() => {
    onSubmitEdit.mockReset()
  })

  it("opens on the user's stored name, role and email", () => {
    renderModal(EXISTING_USER)

    expect(screen.getByText("Edit Account")).toBeInTheDocument()
    expect(field("name")).toHaveValue("Operator User")
    expect(field("email")).toHaveValue("operator@example.com")
    expect(screen.getByText("OPERATOR")).toBeInTheDocument()
    // Editing an existing user never asks for a password
    expect(field("password")).toBeNull()
    expect(field("confirmPassword")).toBeNull()
  })

  // Seeded invalid rather than typed invalid. Typing runs onChangeData, which
  // validates as it goes, so the error would already be on screen before Ok is
  // clicked and the assertion would hold with the submit check removed.
  it.each([
    ["an empty name", { name: "" }, "This field is required"],
    ["an invalid email", { email: "not-an-email" }, "Invalid email format"],
    ["no role", { role_id: "" }, "This field is required"],
  ])("refuses %s on submit", async (_label, override, message) => {
    const ok = renderModal({ ...EXISTING_USER, ...override })

    expect(screen.queryByText(message)).not.toBeInTheDocument()
    fireEvent.click(ok)

    await waitFor(() => expect(screen.getByText(message)).toBeInTheDocument())
    expect(onSubmitEdit).not.toHaveBeenCalled()
  })

  it("submits a valid edit with the user's id", async () => {
    // The positive control: without it, "not submitted" above could be a form
    // that never submits at all
    const ok = renderModal(EXISTING_USER)

    change("name", "Renamed User")
    fireEvent.click(ok)

    await waitFor(() => expect(onSubmitEdit).toHaveBeenCalledTimes(1))
    expect(onSubmitEdit).toHaveBeenCalledWith(
      EXISTING_USER.id,
      expect.objectContaining({
        name: "Renamed User",
        email: "operator@example.com",
        role_id: "OPERATOR",
      }),
    )
  })

  it("holds the form disabled while the submit is in flight", async () => {
    // Without the guard a second click sends a second create for the same
    // address, and the admin sees "email already exists" for their own request
    let release: () => void = () => undefined
    onSubmitEdit.mockImplementation(
      () => new Promise<void>((resolve) => (release = resolve)),
    )
    const ok = renderModal(EXISTING_USER)

    fireEvent.click(ok)
    await waitFor(() => expect(onSubmitEdit).toHaveBeenCalledTimes(1))
    fireEvent.click(ok)
    fireEvent.click(ok)

    expect(onSubmitEdit).toHaveBeenCalledTimes(1)
    release()
  })

  describe("adding a new account", () => {
    const NEW_USER = {
      name: "New Operator",
      email: "new@example.com",
      role_id: "OPERATOR",
    }

    const fillNewAccount = (password: string, confirmPassword = password) => {
      const ok = renderModal(NEW_USER)
      change("password", password)
      change("confirmPassword", confirmPassword)
      return ok
    }

    it("asks for a password it does not ask an existing account for", () => {
      renderModal()

      expect(screen.getByText("Add Account")).toBeInTheDocument()
      expect(field("password")).toBeInTheDocument()
      expect(field("confirmPassword")).toBeInTheDocument()
    })

    it("submits with no id, which is what makes it a create", async () => {
      const ok = fillNewAccount("newPass!1")

      fireEvent.click(ok)

      await waitFor(() => expect(onSubmitEdit).toHaveBeenCalledTimes(1))
      expect(onSubmitEdit).toHaveBeenCalledWith(
        undefined,
        expect.objectContaining({
          name: "New Operator",
          email: "new@example.com",
          password: "newPass!1",
        }),
      )
    })

    it("refuses a confirmation that does not match", async () => {
      const ok = fillNewAccount("newPass!1", "newPass!2")

      fireEvent.click(ok)

      await waitFor(() =>
        expect(screen.getByText("password is not match")).toBeInTheDocument(),
      )
      expect(onSubmitEdit).not.toHaveBeenCalled()
    })

    it("refuses a password that breaks the character rule", async () => {
      // Long enough, so the length clause is not what rejects it
      const ok = fillNewAccount("abcdefg")

      fireEvent.click(ok)

      await waitFor(() =>
        expect(
          screen.getAllByText(/must be at least 6 characters long/).length,
        ).toBeGreaterThan(0),
      )
      expect(onSubmitEdit).not.toHaveBeenCalled()
    })

    it("refuses a missing password", async () => {
      const ok = renderModal(NEW_USER)

      fireEvent.click(ok)

      await waitFor(() =>
        expect(
          screen.getAllByText("This field is required").length,
        ).toBeGreaterThan(0),
      )
      expect(onSubmitEdit).not.toHaveBeenCalled()
    })
  })
})
