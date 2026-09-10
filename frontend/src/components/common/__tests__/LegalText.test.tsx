/* eslint-disable no-undef */
import "@testing-library/jest-dom"
import { MemoryRouter } from "react-router-dom"

import { describe, it } from "@jest/globals"
import { render, screen } from "@testing-library/react"

import { ExternalLink, InternalLink } from "components/common/LegalText"

describe("LegalText links", () => {
  it("opens absolute http links in a new tab", () => {
    render(<ExternalLink href="https://www.araya.org/">araya</ExternalLink>)

    const link = screen.getByRole("link", { name: "araya" })
    expect(link).toHaveAttribute("target", "_blank")
    expect(link).toHaveAttribute("rel", "noopener noreferrer")
  })

  it("leaves mailto links to the mail client", () => {
    render(<ExternalLink href="mailto:support@araya.org">mail</ExternalLink>)

    const link = screen.getByRole("link", { name: "mail" })
    expect(link).not.toHaveAttribute("target")
    expect(link).not.toHaveAttribute("rel")
  })

  it("routes internal links client-side", () => {
    render(
      <MemoryRouter>
        <InternalLink to="/privacy">privacy</InternalLink>
      </MemoryRouter>,
    )

    const link = screen.getByRole("link", { name: "privacy" })
    expect(link).toHaveAttribute("href", "/privacy")
    expect(link).not.toHaveAttribute("target")
  })
})
