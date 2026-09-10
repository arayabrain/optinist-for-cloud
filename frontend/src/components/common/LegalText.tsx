import { FC, ReactNode } from "react"
import { Link } from "react-router-dom"

import { Typography, styled } from "@mui/material"

import { FONT_WEIGHT } from "const/Style"

export const P: FC<{ children: ReactNode }> = ({ children }) => (
  <Typography sx={{ mb: 2 }}>{children}</Typography>
)

export const H2: FC<{ children: ReactNode }> = ({ children }) => (
  <Typography
    variant="h6"
    component="h2"
    sx={{ mt: 4, mb: 1.5, fontWeight: FONT_WEIGHT.BOLD }}
  >
    {children}
  </Typography>
)

export const H3: FC<{ children: ReactNode }> = ({ children }) => (
  <Typography
    variant="subtitle1"
    component="h3"
    sx={{ mt: 2, mb: 1, fontWeight: FONT_WEIGHT.BOLD }}
  >
    {children}
  </Typography>
)

const linkStyle = {
  color: "inherit",
  textDecorationColor: "inherit",
}

const StyledAnchor = styled("a")(linkStyle)

const StyledLink = styled(Link)(linkStyle)

// mailto: hands off to the mail client, so a new tab would just be left blank
export const ExternalLink: FC<{ href: string; children: ReactNode }> = ({
  href,
  children,
}) => {
  const opensNewTab = href.startsWith("http")
  return (
    <StyledAnchor
      href={href}
      target={opensNewTab ? "_blank" : undefined}
      rel={opensNewTab ? "noopener noreferrer" : undefined}
    >
      {children}
    </StyledAnchor>
  )
}

export const InternalLink: FC<{ to: string; children: ReactNode }> = ({
  to,
  children,
}) => <StyledLink to={to}>{children}</StyledLink>
