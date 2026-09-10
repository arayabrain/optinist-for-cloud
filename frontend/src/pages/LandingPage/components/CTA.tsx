import { useNavigate } from "react-router-dom"

import { Box, styled, Typography } from "@mui/material"

import {
  FONT_SIZE,
  FONT_WEIGHT,
  TEXT_COLOR,
  BG_COLOR,
  BORDER_COLOR,
  ACCENT_COLOR,
  BUTTON_BASE,
} from "const/Style"

export const CTA = () => {
  const navigate = useNavigate()

  return (
    <CTASection>
      <Container>
        <CTAWrapper>
          <CTAGlow1 />
          <CTAGlow2 />
          <CTATitle>Ready to Transform Your Research?</CTATitle>
          <CTADescription>
            Join researchers worldwide using OptiNiSt to accelerate their
            scientific discoveries.
          </CTADescription>
          <CTAButtons>
            <CTAPrimaryButton onClick={() => navigate("/login")}>
              Get Started
            </CTAPrimaryButton>
            <CTASecondaryLink
              href="https://araya-optinist.readthedocs.io/en/latest/"
              target="_blank"
              rel="noopener noreferrer"
            >
              View Documentation
            </CTASecondaryLink>
          </CTAButtons>
        </CTAWrapper>
      </Container>
    </CTASection>
  )
}

const CTASection = styled("section")({
  padding: "5rem 0",
})

const Container = styled(Box)({
  maxWidth: 1200,
  margin: "0 auto",
  padding: "0 1.5rem",
})

const CTAWrapper = styled(Box)({
  backgroundColor: ACCENT_COLOR.PRIMARY.bg,
  borderRadius: 24,
  padding: "3rem",
  textAlign: "center",
  color: TEXT_COLOR.PRIMARY,
  position: "relative",
  overflow: "hidden",
})

const CTAGlow1 = styled(Box)({
  position: "absolute",
  width: 256,
  height: 256,
  borderRadius: "50%",
  filter: "blur(60px)",
  top: 0,
  right: 0,
  backgroundColor: "rgba(37, 99, 235, 0.08)",
  transform: "translate(30%, -30%)",
})

const CTAGlow2 = styled(Box)({
  position: "absolute",
  width: 256,
  height: 256,
  borderRadius: "50%",
  filter: "blur(60px)",
  bottom: 0,
  left: 0,
  backgroundColor: ACCENT_COLOR.CYAN.bg,
  transform: "translate(-30%, 30%)",
})

const CTATitle = styled(Typography)({
  fontSize: FONT_SIZE.SECTION_TITLE,
  fontWeight: FONT_WEIGHT.BOLD,
  margin: "0 0 1.5rem",
  position: "relative",
  zIndex: 10,
})

const CTADescription = styled(Typography)({
  color: TEXT_COLOR.SECONDARY,
  maxWidth: 600,
  margin: "0 auto 2.5rem",
  fontSize: FONT_SIZE.SECTION_SUBTITLE,
  position: "relative",
  zIndex: 10,
})

const CTAButtons = styled(Box)({
  display: "flex",
  flexDirection: "column",
  gap: "1rem",
  justifyContent: "center",
  position: "relative",
  zIndex: 10,
  "@media (min-width: 640px)": {
    flexDirection: "row",
  },
})

const ctaButtonBase = {
  ...BUTTON_BASE,
  height: 56,
  padding: "0 2.5rem",
  borderRadius: 12,
  textDecoration: "none",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
} as const

const CTAPrimaryButton = styled("button")({
  ...ctaButtonBase,
})

const CTASecondaryLink = styled("a")({
  ...ctaButtonBase,
  backgroundColor: BG_COLOR.WHITE,
  color: TEXT_COLOR.ACCENT,
  border: `1px solid ${BORDER_COLOR.DEFAULT}`,
  "&:hover": {
    backgroundColor: "#f3f4f6",
  },
})
