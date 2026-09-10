/**
 * The Change Password modal's own validation: what it refuses to submit.
 *
 * Everything here stops at the `onSubmit` boundary. The two outcomes that depend
 * on the server - a wrong current password and a successful change - are the
 * Account page's snackbars, and are covered where that page is rendered.
 */

import { describe, it, expect, jest, beforeEach } from "@jest/globals"
import "@testing-library/jest-dom"
import { render, screen, fireEvent } from "@testing-library/react"

import ChangePasswordModal from "components/Account/ChangePasswordModal"

const onSubmit = jest.fn()

// The dialog renders through a portal, so its inputs are not under `container`
const field = (name: string) =>
  document.querySelector(`input[name="${name}"]`) as HTMLInputElement

const renderModal = () => {
  render(<ChangePasswordModal open onClose={jest.fn()} onSubmit={onSubmit} />)
  return {
    old: field("password"),
    next: field("new_password"),
    confirm: field("confirm_password"),
    update: screen.getByRole("button", { name: "UPDATE" }),
  }
}

const fill = (input: HTMLInputElement, value: string) =>
  fireEvent.change(input, { target: { name: input.name, value } })

// The page closes this modal by toggling `open`, never by the Close button, so
// tests about closing have to do the same
const modalWithOpen = (open: boolean) => (
  <ChangePasswordModal open={open} onClose={jest.fn()} onSubmit={onSubmit} />
)

describe("Change password modal", () => {
  beforeEach(() => {
    onSubmit.mockClear()
  })

  it("opens with the three password fields", () => {
    const { old, next, confirm } = renderModal()

    expect(screen.getByText("Change Password")).toBeInTheDocument()
    // All three are masked; a plaintext field here would put the password in
    // the DOM in cleartext
    for (const input of [old, next, confirm]) {
      expect(input).toBeInTheDocument()
      expect(input).toHaveAttribute("type", "password")
    }
    // The rule is stated where the user is asked to satisfy it
    expect(screen.getByText(/At least 6 characters/)).toBeInTheDocument()
  })

  it("refuses an empty form and says which fields are required", () => {
    const { update } = renderModal()

    fireEvent.click(update)

    expect(screen.getAllByText("This field is required")).toHaveLength(3)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("refuses a new password that does not match its confirmation", () => {
    const { old, next, confirm, update } = renderModal()

    fill(old, "oldPass!1")
    fill(next, "newPass!1")
    fill(confirm, "newPass!2")

    expect(screen.getByText("Passwords do not match")).toBeInTheDocument()

    fireEvent.click(update)

    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("submits the old and new password once both match", () => {
    // The positive control: without it, "not submitted" above could be a form
    // that never submits at all
    const { old, next, confirm, update } = renderModal()

    fill(old, "oldPass!1")
    fill(next, "newPass!1")
    fill(confirm, "newPass!1")
    fireEvent.click(update)

    expect(onSubmit).toHaveBeenCalledWith("oldPass!1", "newPass!1")
  })

  it("refuses a new password that is too short", () => {
    const { old, next, confirm, update } = renderModal()

    fill(old, "oldPass!1")
    fill(next, "aB!1")
    fill(confirm, "aB!1")
    fireEvent.click(update)

    expect(
      screen.getAllByText(/must be at least 6 characters long/).length,
    ).toBeGreaterThan(0)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  // Each of these is long enough, so the length clause cannot be what rejects
  // it. Without separating them, weakening the rule to a bare length check goes
  // unnoticed.
  it.each([
    ["no digit and no special character", "abcdefg"],
    ["no special character", "abcdef1"],
    ["no digit", "abcdef!"],
    ["no letter", "123456!"],
  ])("refuses a new password with %s", (_label, password) => {
    const { old, next, confirm, update } = renderModal()

    fill(old, "oldPass!1")
    fill(next, password)
    fill(confirm, password)
    fireEvent.click(update)

    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("refuses a special character outside the allowed set", () => {
    // Satisfies the "letter, digit, allowed special" rule via `!`, so the first
    // check passes and the separate allowed-set check is what rejects `^`
    const { old, next, confirm, update } = renderModal()

    fill(old, "oldPass!1")
    fill(next, "abcd1!^")
    fill(confirm, "abcd1!^")
    fireEvent.click(update)

    expect(
      screen.getAllByText(/Allowed special characters/).length,
    ).toBeGreaterThan(0)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("refuses a new password longer than 255 characters", () => {
    const tooLong = `${"a".repeat(255)}1!`
    const { old, next, confirm, update } = renderModal()

    fill(old, "oldPass!1")
    fill(next, tooLong)
    fill(confirm, tooLong)
    fireEvent.click(update)

    expect(
      screen.getAllByText(/may not be longer than 255 characters/).length,
    ).toBeGreaterThan(0)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("refuses an empty old password even when the new one is valid", () => {
    // The old password is what authorises the change. Submitting without it
    // sends `undefined` as the current password, which the server then has to
    // refuse - and the field is never marked as the reason.
    const { next, confirm, update } = renderModal()

    fill(next, "newPass!1")
    fill(confirm, "newPass!1")
    fireEvent.click(update)

    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByText("This field is required")).toBeInTheDocument()
  })

  it("reports the missing old password even when another field already errored", () => {
    // Gating submission on the live error state rather than on a full
    // revalidation reports only the fields that state happens to hold, so the
    // user fixes the mismatch, resubmits, and only then learns the old password
    // was required all along.
    const { next, confirm, update } = renderModal()

    fill(next, "newPass!1")
    fill(confirm, "newPass!2")
    expect(screen.getByText("Passwords do not match")).toBeInTheDocument()

    fireEvent.click(update)

    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByText("This field is required")).toBeInTheDocument()
  })

  it("flags a mismatch as soon as the new password is edited after confirming", () => {
    // Without re-validating the confirmation on every new-password keystroke, the
    // form looks valid until submit even though the two fields now differ
    const { old, next, confirm } = renderModal()

    fill(old, "oldPass!1")
    fill(next, "newPass!1")
    fill(confirm, "newPass!1")
    expect(screen.queryByText("Passwords do not match")).not.toBeInTheDocument()

    fill(next, "changed!2")

    expect(screen.getByText("Passwords do not match")).toBeInTheDocument()
  })

  it("marks the new password required when left blank on blur", () => {
    // Tabbing past the field reports it immediately rather than at submit
    const { next } = renderModal()

    fireEvent.blur(next)

    expect(screen.getByText("This field is required")).toBeInTheDocument()
  })

  it("drops the validation errors when closed", () => {
    // The page keeps this component mounted and only toggles `open`, so a stale
    // "This field is required" would greet the user on reopen
    const { rerender } = render(modalWithOpen(true))

    fireEvent.click(screen.getByRole("button", { name: "UPDATE" }))
    expect(screen.getAllByText("This field is required")).toHaveLength(3)

    rerender(modalWithOpen(false))
    rerender(modalWithOpen(true))

    expect(screen.queryByText("This field is required")).not.toBeInTheDocument()
  })

  it("does not carry the previous old password into the next attempt", () => {
    // The page closes this modal itself once a submit settles, which never runs
    // the Close button's handler. The inputs are uncontrolled, so whatever stays
    // in state outlives the fields the user can see: the next attempt would post
    // the previous old password from a field that looks empty, and the server
    // would reject it for no visible reason.
    const { rerender } = render(modalWithOpen(true))

    fill(field("password"), "oldPass!1")
    fill(field("new_password"), "newPass!1")
    fill(field("confirm_password"), "newPass!1")
    fireEvent.click(screen.getByRole("button", { name: "UPDATE" }))
    expect(onSubmit).toHaveBeenCalledTimes(1)

    // The page's own close, then reopened
    rerender(modalWithOpen(false))
    rerender(modalWithOpen(true))
    onSubmit.mockClear()

    // Only the new password is supplied this time
    fill(field("new_password"), "second!2")
    fill(field("confirm_password"), "second!2")
    fireEvent.click(screen.getByRole("button", { name: "UPDATE" }))

    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByText("This field is required")).toBeInTheDocument()
  })
})
