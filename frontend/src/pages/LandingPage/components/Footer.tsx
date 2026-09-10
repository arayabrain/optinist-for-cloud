import { Link } from "react-router-dom"

import { Box, styled, Typography } from "@mui/material"

import {
  FONT_SIZE,
  FONT_WEIGHT,
  LINE_HEIGHT,
  LETTER_SPACING,
  TEXT_COLOR,
  BG_COLOR,
  BORDER_COLOR,
} from "const/Style"

export const Footer = () => {
  return (
    <FooterWrapper>
      <Container>
        <FooterBrand>
          <Logo>
            <LogoIcon>
              <img
                src="/static/optinist_logo.png"
                alt="OptiNiSt"
                style={{ height: 32, width: "auto" }}
              />
            </LogoIcon>
            <LogoText>ARAYA OptiNiSt</LogoText>
          </Logo>
          <FooterDescription>
            OptiNiSt is the no-code platform for scientific data analysis. Build
            pipelines visually, ensure reproducibility, and collaborate
            seamlessly.
          </FooterDescription>
          <FooterSocial>
            <SocialLink
              href="https://github.com/arayabrain/araya-optinist"
              target="_blank"
              rel="noopener noreferrer"
              aria-label="GitHub"
            >
              <svg
                width="24"
                height="24"
                viewBox="0 0 24 24"
                fill="currentColor"
              >
                <path d="M12 0c-6.626 0-12 5.373-12 12 0 5.302 3.438 9.8 8.207 11.387.599.111.793-.261.793-.577v-2.234c-3.338.726-4.033-1.416-4.033-1.416-.546-1.387-1.333-1.756-1.333-1.756-1.089-.745.083-.729.083-.729 1.205.084 1.839 1.237 1.839 1.237 1.07 1.834 2.807 1.304 3.492.997.107-.775.418-1.305.762-1.604-2.665-.305-5.467-1.334-5.467-5.931 0-1.311.469-2.381 1.236-3.221-.124-.303-.535-1.524.117-3.176 0 0 1.008-.322 3.301 1.23.957-.266 1.983-.399 3.003-.404 1.02.005 2.047.138 3.006.404 2.291-1.552 3.297-1.23 3.297-1.23.653 1.653.242 2.874.118 3.176.77.84 1.235 1.911 1.235 3.221 0 4.609-2.807 5.624-5.479 5.921.43.372.823 1.102.823 2.222v3.293c0 .319.192.694.801.576 4.765-1.589 8.199-6.086 8.199-11.386 0-6.627-5.373-12-12-12z" />
              </svg>
            </SocialLink>
          </FooterSocial>
        </FooterBrand>
        <FooterBottom>
          <FooterAttribution>
            ARAYA OptiNiSt is based on OptiNiSt. OptiNiSt was developed by{" "}
            <LogoLink
              href="https://www.araya.org/"
              target="_blank"
              rel="noopener noreferrer"
            >
              <img
                src="/static/araya_logo.png"
                alt="Araya Inc"
                style={{ height: 20, width: "auto", verticalAlign: "middle" }}
              />
            </LogoLink>{" "}
            and{" "}
            <FooterLink
              href="https://www.oist.jp/"
              target="_blank"
              rel="noopener noreferrer"
            >
              OIST
            </FooterLink>
          </FooterAttribution>
          <FooterLegal>
            <LegalLink to="/terms">Terms of Service</LegalLink>
            <LegalLink to="/privacy">Privacy Policy</LegalLink>
          </FooterLegal>
          <FooterCopyright>
            &copy; {`${new Date().getFullYear()}`} ARAYA Inc.
          </FooterCopyright>
        </FooterBottom>
      </Container>
    </FooterWrapper>
  )
}

const FooterWrapper = styled("footer")({
  backgroundColor: BG_COLOR.WHITE,
  borderTop: `1px solid ${BORDER_COLOR.DEFAULT}`,
  padding: "3rem 0",
})

const Container = styled(Box)({
  maxWidth: 1200,
  margin: "0 auto",
  padding: "0 1.5rem",
})

const FooterBrand = styled(Box)({
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  textAlign: "center",
})

const Logo = styled(Box)({
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
})

const LogoIcon = styled(Box)({
  width: 32,
  height: 32,
  borderRadius: 6,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  color: TEXT_COLOR.ACCENT,
})

const LogoText = styled(Typography)({
  fontSize: FONT_SIZE.CARD_TITLE,
  fontWeight: FONT_WEIGHT.BOLD,
  letterSpacing: LETTER_SPACING.TIGHT,
})

const FooterDescription = styled(Typography)({
  fontSize: FONT_SIZE.SMALL,
  color: TEXT_COLOR.SECONDARY,
  margin: "1rem 0",
  maxWidth: 400,
  lineHeight: LINE_HEIGHT.NORMAL,
})

const FooterSocial = styled(Box)({
  display: "flex",
  gap: "1rem",
})

const SocialLink = styled("a")({
  color: TEXT_COLOR.SECONDARY,
  transition: "color 0.2s",
  "&:hover": {
    color: TEXT_COLOR.ACCENT,
  },
})

const FooterBottom = styled(Box)({
  marginTop: "2rem",
  paddingTop: "1.5rem",
  borderTop: `1px solid ${BORDER_COLOR.DEFAULT}`,
  display: "flex",
  flexDirection: "column",
  gap: "0.5rem",
  alignItems: "center",
  textAlign: "center",
})

const FooterAttribution = styled(Typography)({
  fontSize: FONT_SIZE.SMALL,
  color: TEXT_COLOR.SECONDARY,
  fontStyle: "italic",
})

const FooterLink = styled("a")({
  color: TEXT_COLOR.SECONDARY,
  textDecoration: "underline",
  transition: "color 0.2s",
  "&:hover": {
    color: TEXT_COLOR.ACCENT,
  },
})

// Logo link: fade the image slightly on hover to signal it is a link
const LogoLink = styled(FooterLink)({
  display: "inline-block",
  verticalAlign: "middle",
  fontSize: 0,
  textDecoration: "none",
  "& img": {
    transition: "opacity 0.2s",
  },
  "&:hover img": {
    opacity: 0.7,
  },
})

const FooterCopyright = styled(Typography)({
  fontSize: FONT_SIZE.SMALL,
  color: TEXT_COLOR.SECONDARY,
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
