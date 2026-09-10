import { Provider } from "react-redux"
import { MemoryRouter } from "react-router-dom"

import configureStore from "redux-mock-store"

import { describe, it, expect, beforeEach, jest } from "@jest/globals"
import { render, screen, fireEvent } from "@testing-library/react"

import RegistrationForm from "pages/Register/MainRegistration"

const mockRegisterUser = jest.fn()

jest.mock("store/slice/Registration/RegistrationActions", () => {
  const registerUser = (...args: unknown[]) => {
    mockRegisterUser(...args)
    return { type: "registration/registerUser/mock" }
  }
  registerUser.fulfilled = { match: () => false }
  return {
    registerUser,
    resendVerificationEmail: () => ({ type: "registration/resend/mock" }),
  }
})

const mockStoreCreator = configureStore([])

const store = mockStoreCreator({
  user: { currentUser: null },
  registration: {
    registration: { loading: false, success: false, error: null, user: null },
    verificationStatus: { loading: false, error: null, data: null },
    resendEmail: { loading: false, success: false, error: null },
  },
})

const renderForm = () =>
  render(
    <Provider store={store}>
      <MemoryRouter>
        <RegistrationForm />
      </MemoryRouter>
    </Provider>,
  )

const fillValidFields = () => {
  const fill = (name: string, value: string) =>
    fireEvent.change(document.querySelector(`input[name=${name}]`)!, {
      target: { value },
    })

  fill("name", "Test User")
  fill("email", "test@example.com")
  fill("password", "Passw0rd!")
  fill("confirmPassword", "Passw0rd!")
}

const AGREEMENT_ERROR =
  "You must agree to the Terms of Service and Privacy Policy"

describe("registration terms agreement gate", () => {
  beforeEach(() => {
    mockRegisterUser.mockClear()
  })

  it("blocks submission when the agreement box is unchecked", () => {
    renderForm()
    fillValidFields()

    fireEvent.click(screen.getByRole("button", { name: "Sign Up" }))

    expect(screen.getByText(AGREEMENT_ERROR)).toBeTruthy()
    expect(mockRegisterUser).not.toHaveBeenCalled()
  })

  it("submits once the agreement box is checked", () => {
    renderForm()
    fillValidFields()
    fireEvent.click(screen.getByLabelText(/I agree to the/))

    fireEvent.click(screen.getByRole("button", { name: "Sign Up" }))

    expect(screen.queryByText(AGREEMENT_ERROR)).toBeNull()
    expect(mockRegisterUser).toHaveBeenCalledTimes(1)
  })
})
