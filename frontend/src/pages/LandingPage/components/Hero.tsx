import { useNavigate } from "react-router-dom"

import { Box, styled, Typography } from "@mui/material"

import {
  FONT_SIZE,
  FONT_WEIGHT,
  LETTER_SPACING,
  LINE_HEIGHT,
  TEXT_COLOR,
  ACCENT_COLOR,
  BUTTON_BASE,
} from "const/Style"

export const Hero = () => {
  const navigate = useNavigate()

  return (
    <HeroSection>
      <HeroGrid>
        <HeroContent>
          <HeroText>
            <Label>Visual Data Analysis for Science</Label>
            <HeroTitle>
              Build. Analyze. <GradientText>Collaborate.</GradientText>
            </HeroTitle>
            <HeroSubtitle>
              with <BrandText>OptiNiSt</BrandText>
            </HeroSubtitle>
            <HeroDescription>
              The no-code platform for scientific data analysis.
              <br />
              Build pipelines visually, ensure reproducibility, and collaborate
              seamlessly.
            </HeroDescription>
          </HeroText>
          <HeroButtons>
            <PrimaryButtonLg onClick={() => navigate("/login")}>
              Get Started
            </PrimaryButtonLg>
          </HeroButtons>
          <HeroBadges>
            <Badge>
              <span
                className="material-symbols-outlined"
                style={{ color: ACCENT_COLOR.GREEN.color }}
              >
                check_circle
              </span>
              <span>No coding required</span>
            </Badge>
            <Badge>
              <span
                className="material-symbols-outlined"
                style={{ color: ACCENT_COLOR.GREEN.color }}
              >
                check_circle
              </span>
              <span>NWB (Neurodata Without Borders) compatible</span>
            </Badge>
          </HeroBadges>
        </HeroContent>
        <HeroVisual>
          <HeroImage src="/static/optinist_logo.png" alt="OptiNiSt logo" />
        </HeroVisual>
      </HeroGrid>
    </HeroSection>
  )
}

const HeroSection = styled("section")({
  maxWidth: 1200,
  margin: "0 auto",
  padding: "4rem 1.5rem",
  "@media (min-width: 768px)": {
    padding: "6rem 1.5rem",
  },
})

const HeroGrid = styled(Box)({
  display: "grid",
  gridTemplateColumns: "1fr",
  gap: "3rem",
  alignItems: "center",
  "@media (min-width: 1024px)": {
    gridTemplateColumns: "1fr 1fr",
  },
})

const HeroContent = styled(Box)({
  display: "flex",
  flexDirection: "column",
  gap: "2rem",
})

const HeroText = styled(Box)({
  display: "flex",
  flexDirection: "column",
  gap: "1rem",
})

const Label = styled(Typography)({
  color: TEXT_COLOR.ACCENT,
  fontWeight: FONT_WEIGHT.BOLD,
  letterSpacing: LETTER_SPACING.WIDE,
  fontSize: FONT_SIZE.SMALL,
  textTransform: "uppercase",
})

const HeroTitle = styled("h1")({
  fontSize: FONT_SIZE.HERO_TITLE,
  fontWeight: FONT_WEIGHT.BLACK,
  lineHeight: LINE_HEIGHT.TIGHT,
  letterSpacing: LETTER_SPACING.TIGHT,
  margin: 0,
  "@media (min-width: 768px)": {
    fontSize: FONT_SIZE.HERO_TITLE_MD,
  },
})

const GradientText = styled("span")({
  background:
    `linear-gradient(135deg, ${ACCENT_COLOR.PRIMARY.color} 0%, ` +
    `${ACCENT_COLOR.CYAN.color} 50%, ` +
    `${ACCENT_COLOR.GREEN.color} 100%)`,
  WebkitBackgroundClip: "text",
  WebkitTextFillColor: "transparent",
  backgroundClip: "text",
})

const HeroSubtitle = styled("div")({
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  fontSize: FONT_SIZE.DISPLAY_SM,
  fontWeight: FONT_WEIGHT.REGULAR,
  color: TEXT_COLOR.MUTED,
  marginTop: "0.5rem",
  "@media (min-width: 768px)": {
    fontSize: FONT_SIZE.HERO_SUBTITLE_MD,
  },
})

const BrandText = styled("span")({
  background:
    `linear-gradient(135deg, ${ACCENT_COLOR.CYAN.color} 0%, ` +
    `${ACCENT_COLOR.GREEN.color} 100%)`,
  WebkitBackgroundClip: "text",
  WebkitTextFillColor: "transparent",
  backgroundClip: "text",
  fontWeight: FONT_WEIGHT.BOLD,
  letterSpacing: LETTER_SPACING.TIGHT,
})

const HeroDescription = styled(Typography)({
  fontSize: FONT_SIZE.SECTION_SUBTITLE,
  color: TEXT_COLOR.SECONDARY,
  maxWidth: 500,
  margin: 0,
  lineHeight: LINE_HEIGHT.NORMAL,
})

const HeroButtons = styled(Box)({
  display: "flex",
  flexDirection: "column",
  gap: "1rem",
  "@media (min-width: 640px)": {
    flexDirection: "row",
  },
})

const PrimaryButtonLg = styled("button")({
  ...BUTTON_BASE,
  height: 48,
  padding: "0 2rem",
})

const HeroBadges = styled(Box)({
  display: "flex",
  alignItems: "center",
  gap: "1.5rem",
  paddingTop: "1rem",
})

const Badge = styled(Box)({
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  fontSize: FONT_SIZE.SMALL,
  color: TEXT_COLOR.SECONDARY,
})

const HeroVisual = styled(Box)({
  position: "relative",
  display: "flex",
  justifyContent: "center",
  alignItems: "center",
  "@media (min-width: 1024px)": {
    transform: "scale(1.15)",
    transformOrigin: "center center",
  },
})

const HeroImage = styled("img")({
  width: "100%",
  maxWidth: 600,
  height: "auto",
})
