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

interface AudienceCard {
  icon: string
  title: string
  description: string
  features: string[]
  color: "PRIMARY" | "CYAN" | "GREEN"
}

const audiences: AudienceCard[] = [
  {
    icon: "psychology",
    title: "Neuroscience Labs",
    description:
      "Analyze calcium imaging, electrophysiology, and behavioral " +
      "data with specialized tools.",
    features: [
      "Calcium imaging pipelines",
      "ROI extraction & analysis",
      "NWB (Neurodata Without Borders) export",
    ],
    color: "PRIMARY",
  },
  {
    icon: "biotech",
    title: "Microscopy Researchers",
    description:
      "Build image processing pipelines for various microscopes " +
      "without coding.",
    features: [
      "Multi-format image support",
      "Batch processing",
      "LCCD cell detection algorithm",
    ],
    color: "CYAN",
  },
  {
    icon: "school",
    title: "Educators & Students",
    description:
      "Teach data analysis concepts using our sample data and " +
      "tools. No coding required for you or your students.",
    features: [
      "Visual learning interface",
      "Shareable workspaces",
      "Focus on science, not syntax",
    ],
    color: "GREEN",
  },
]

export const Audience = () => {
  return (
    <AudienceSection id="audience">
      <Container>
        <SectionHeaderCenter>
          <SectionLabel>
            <span
              className="material-symbols-outlined"
              style={{ fontSize: FONT_SIZE.CARD_TITLE }}
            >
              groups
            </span>
            Audience
          </SectionLabel>
          <SectionTitle>Who It&apos;s For</SectionTitle>
          <SectionSubtitle>
            Empowering researchers and educators worldwide.
          </SectionSubtitle>
        </SectionHeaderCenter>
        <AudienceGrid>
          {audiences.map((audience, index) => (
            <AudienceCardWrapper key={index}>
              <AudienceHeader>
                <AudienceIcon
                  style={{
                    backgroundColor: ACCENT_COLOR[audience.color].bg,
                    color: ACCENT_COLOR[audience.color].color,
                  }}
                >
                  <span className="material-symbols-outlined">
                    {audience.icon}
                  </span>
                </AudienceIcon>
                <AudienceTitle>{audience.title}</AudienceTitle>
              </AudienceHeader>
              <AudienceDescription>{audience.description}</AudienceDescription>
              <AudienceFeatures>
                {audience.features.map((feature, featureIndex) => (
                  <AudienceFeature key={featureIndex}>
                    <span
                      className="material-symbols-outlined"
                      style={{
                        color: ACCENT_COLOR[audience.color].color,
                      }}
                    >
                      check_circle
                    </span>
                    <span>{feature}</span>
                  </AudienceFeature>
                ))}
              </AudienceFeatures>
            </AudienceCardWrapper>
          ))}
        </AudienceGrid>
      </Container>
    </AudienceSection>
  )
}

const AudienceSection = styled("section")({
  backgroundColor: BG_COLOR.PAGE,
  padding: "5rem 0",
})

const Container = styled(Box)({
  maxWidth: 1200,
  margin: "0 auto",
  padding: "0 1.5rem",
})

const SectionHeaderCenter = styled(Box)({
  display: "flex",
  flexDirection: "column",
  textAlign: "center",
  marginBottom: "4rem",
})

const SectionLabel = styled(Box)({
  display: "inline-flex",
  alignSelf: "flex-start",
  alignItems: "center",
  gap: "0.5rem",
  backgroundColor: ACCENT_COLOR.PRIMARY.bg,
  color: TEXT_COLOR.ACCENT,
  fontWeight: FONT_WEIGHT.BOLD,
  fontSize: FONT_SIZE.SMALL,
  padding: "0.5rem 1rem",
  borderRadius: 9999,
  width: "fit-content",
})

const SectionTitle = styled(Typography)({
  fontSize: FONT_SIZE.SECTION_TITLE,
  fontWeight: FONT_WEIGHT.BOLD,
  margin: "1rem 0 1rem",
  textAlign: "center",
})

const SectionSubtitle = styled(Typography)({
  textAlign: "center",
  color: TEXT_COLOR.SECONDARY,
  fontSize: FONT_SIZE.SECTION_SUBTITLE,
  margin: 0,
  maxWidth: 600,
  marginLeft: "auto",
  marginRight: "auto",
})

const AudienceGrid = styled(Box)({
  display: "grid",
  gridTemplateColumns: "1fr",
  gap: "2rem",
  "@media (min-width: 768px)": {
    gridTemplateColumns: "repeat(3, 1fr)",
  },
})

const AudienceCardWrapper = styled(Box)({
  display: "flex",
  flexDirection: "column",
  gap: "1.5rem",
  backgroundColor: BG_COLOR.WHITE,
  padding: "2.5rem",
  borderRadius: 16,
  border: `1px solid ${BORDER_COLOR.DEFAULT}`,
})

const AudienceHeader = styled(Box)({
  display: "flex",
  alignItems: "flex-start",
  gap: "0.75rem",
})

const AudienceIcon = styled(Box)({ ...ICON_BOX })

const AudienceTitle = styled(Typography)({
  fontSize: FONT_SIZE.CARD_TITLE,
  fontWeight: FONT_WEIGHT.BOLD,
  margin: 0,
})

const AudienceDescription = styled(Typography)({
  fontSize: FONT_SIZE.BODY,
  color: TEXT_COLOR.SECONDARY,
  margin: 0,
  lineHeight: LINE_HEIGHT.NORMAL,
})

const AudienceFeatures = styled("ul")({
  listStyle: "none",
  padding: 0,
  margin: "auto 0 0",
  display: "flex",
  flexDirection: "column",
  gap: "0.75rem",
})

const AudienceFeature = styled("li")({
  display: "flex",
  alignItems: "center",
  gap: "0.75rem",
  fontSize: FONT_SIZE.SMALL,
  "& .material-symbols-outlined": {
    fontSize: FONT_SIZE.BODY,
  },
})
