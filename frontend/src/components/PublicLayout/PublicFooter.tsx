import { FC } from "react"
import { Link } from "react-router-dom"

import { Box, Divider, styled, Typography } from "@mui/material"

import { FONT_SIZE, TEXT_COLOR } from "const/Style"

// Fixed footer height so the public page can reserve space for the footer.
export const PUBLIC_FOOTER_HEIGHT = 71

const PublicFooter: FC = () => {
  return (
    <FooterRoot>
      <Divider />
      <FooterContent>
        <FooterLegal>
          <LegalLink to="/terms">Terms of Service</LegalLink>
          <LegalLink to="/privacy">Privacy Policy</LegalLink>
        </FooterLegal>
        <FooterCopyright>
          &copy; {`${new Date().getFullYear()}`} ARAYA Inc.
        </FooterCopyright>
      </FooterContent>
    </FooterRoot>
  )
}

const FooterRoot = styled(Box)({
  height: PUBLIC_FOOTER_HEIGHT,
  boxSizing: "border-box",
  paddingTop: 8,
})

const FooterContent = styled(Box)({
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  gap: "0.5rem",
  textAlign: "center",
  marginTop: 8,
})

const FooterLegal = styled(Box)({
  display: "flex",
  gap: "1rem",
})

const LegalLink = styled(Link)({
  fontSize: FONT_SIZE.SMALL,
  color: TEXT_COLOR.SECONDARY,
  textDecoration: "underline",
  transition: "color 0.2s",
  "&:hover": {
    color: TEXT_COLOR.ACCENT,
  },
})

const FooterCopyright = styled(Typography)({
  fontSize: FONT_SIZE.SMALL,
  color: TEXT_COLOR.SECONDARY,
})

export default PublicFooter
