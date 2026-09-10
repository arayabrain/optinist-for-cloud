import { Box, styled, Typography } from "@mui/material"

import {
  FONT_SIZE,
  FONT_WEIGHT,
  LINE_HEIGHT,
  TEXT_COLOR,
  ACCENT_COLOR,
  BG_COLOR,
  BORDER_COLOR,
  ICON_BOX,
} from "const/Style"

interface ValueProp {
  icon: string
  title: string
  description: string
  color: "MAGENTA" | "CYAN" | "GREEN" | "YELLOW"
}

const valueProps: ValueProp[] = [
  {
    icon: "drag_pan",
    title: "No Coding Required",
    description:
      "Build complex analysis pipelines by dragging and dropping. " +
      "Focus on science, not syntax.",
    color: "MAGENTA",
  },
  {
    icon: "neurology",
    title: "NWB-Native",
    description:
      "First-class support for NWB (Neurodata Without Borders). " +
      "Import, analyze, and export in the standard format " +
      "for neurophysiology data.",
    color: "CYAN",
  },
  {
    icon: "analytics",
    title: "Complete Analysis Toolkit",
    description:
      "From visual pipeline building to rich visualization and " +
      "ROI analysis \u2014 everything you need to go from raw " +
      "data to publishable results.",
    color: "GREEN",
  },
  {
    icon: "public",
    title: "Share & Collaborate",
    description:
      "Share workspaces, publish analysis results, and let other " +
      "labs reproduce your exact methods. Grow a community " +
      "around comparable science.",
    color: "YELLOW",
  },
]

export const ValueProps = () => {
  return (
    <ValuePropsSection>
      <Container>
        <SectionTitle>Why Choose OptiNiSt</SectionTitle>
        <SectionSubtitle>
          Everything you need to go from raw data to publishable insights,
          without writing a single line of code.
        </SectionSubtitle>
        <ValueGrid>
          {valueProps.map((prop, index) => (
            <ValueCard key={index}>
              <ValueHeader>
                <ValueIcon
                  style={{
                    backgroundColor: ACCENT_COLOR[prop.color].bg,
                    color: ACCENT_COLOR[prop.color].color,
                  }}
                >
                  <span className="material-symbols-outlined">{prop.icon}</span>
                </ValueIcon>
                <ValueTitle>{prop.title}</ValueTitle>
              </ValueHeader>
              <ValueDescription>{prop.description}</ValueDescription>
            </ValueCard>
          ))}
        </ValueGrid>
      </Container>
    </ValuePropsSection>
  )
}

const ValuePropsSection = styled("section")({
  backgroundColor: BG_COLOR.WHITE,
  padding: "5rem 0",
})

const Container = styled(Box)({
  maxWidth: 1200,
  margin: "0 auto",
  padding: "0 1.5rem",
})

const SectionTitle = styled(Typography)({
  fontSize: FONT_SIZE.SECTION_TITLE,
  fontWeight: FONT_WEIGHT.BOLD,
  margin: "0 0 1rem",
  textAlign: "center",
})

const SectionSubtitle = styled(Typography)({
  textAlign: "center",
  color: TEXT_COLOR.SECONDARY,
  fontSize: FONT_SIZE.SECTION_SUBTITLE,
  margin: "0 0 3rem",
  maxWidth: 600,
  marginLeft: "auto",
  marginRight: "auto",
})

const ValueGrid = styled(Box)({
  display: "grid",
  gridTemplateColumns: "1fr",
  gap: "1.5rem",
  "@media (min-width: 768px)": {
    gridTemplateColumns: "repeat(2, 1fr)",
  },
  "@media (min-width: 1024px)": {
    gridTemplateColumns: "repeat(4, 1fr)",
  },
})

const ValueCard = styled(Box)({
  padding: "2rem",
  borderRadius: 12,
  border: `1px solid ${BORDER_COLOR.DEFAULT}`,
  backgroundColor: BG_COLOR.CARD,
})

const ValueHeader = styled(Box)({
  display: "flex",
  alignItems: "flex-start",
  gap: "0.75rem",
  marginBottom: "0.75rem",
})

const ValueIcon = styled(Box)({ ...ICON_BOX })

const ValueTitle = styled(Typography)({
  fontSize: FONT_SIZE.CARD_TITLE,
  fontWeight: FONT_WEIGHT.BOLD,
  margin: "0 0 0.75rem",
})

const ValueDescription = styled(Typography)({
  fontSize: FONT_SIZE.BODY,
  color: TEXT_COLOR.SECONDARY,
  lineHeight: LINE_HEIGHT.NORMAL,
  margin: 0,
})
