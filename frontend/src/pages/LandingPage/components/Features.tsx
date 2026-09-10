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

interface Feature {
  icon: string
  title: string
  description: string
  image: string
  imageAlt: string
  iconColor: "PRIMARY" | "MAGENTA" | "GREEN" | "YELLOW"
}

const features: Feature[] = [
  {
    icon: "account_tree",
    title: "Visual Workflow Builder",
    description:
      "Drag algorithm nodes onto a blank workflow, connect them " +
      "visually, and configure parameters via a sidebar panel " +
      "with text fields, dropdowns, and toggles. Run pipelines " +
      "with one click.",
    image: "/images/landing-page/visualize_workflow_builder.png",
    imageAlt: "Visual Workflow Builder",
    iconColor: "PRIMARY",
  },
  {
    icon: "analytics",
    title: "Rich Visualization",
    description:
      "Heatmaps, time series, scatter plots, bar charts, " +
      "histograms, and more. Customize colors, export " +
      "publication-ready figures.",
    image: "/images/landing-page/rich_visualization.png",
    imageAlt: "Rich Visualization",
    iconColor: "MAGENTA",
  },
  {
    icon: "frame_inspect",
    title: "Cell ROI Analysis Tools",
    description:
      "View, select, and analyze cell ROI overlaid on images. " +
      "Perfect for calcium imaging and spatial analysis " +
      "workflows.",
    image: "/images/landing-page/roi_analysis_tools.png",
    imageAlt: "Cell ROI Analysis Tools",
    iconColor: "GREEN",
  },
  {
    icon: "science",
    title: "Experiment Management",
    description:
      "Track all analysis workflows and results. Compare " +
      "approaches. Re-run past analyses instantly. Export data " +
      "to NWB (Neurodata Without Borders) or download " +
      "workflow configs.",
    image: "/images/landing-page/experiment_management.png",
    imageAlt: "Experiment Management",
    iconColor: "YELLOW",
  },
]

export const Features = () => {
  return (
    <FeaturesSection id="features">
      <Container>
        <FeaturesHeader>
          <SectionLabel>
            <span
              className="material-symbols-outlined"
              style={{ fontSize: FONT_SIZE.CARD_TITLE }}
            >
              widgets
            </span>
            Features
          </SectionLabel>
          <SectionTitleCenter>
            Everything You Need to Analyze
          </SectionTitleCenter>
          <SectionDescription>
            Powerful tools that make complex data analysis accessible to
            everyone.
          </SectionDescription>
        </FeaturesHeader>
        <FeaturesGrid>
          {features.map((feature, index) => (
            <FeatureCard key={index}>
              <FeatureContent>
                <FeatureHeader>
                  <FeatureIcon
                    style={{
                      backgroundColor: ACCENT_COLOR[feature.iconColor].bg,
                      color: ACCENT_COLOR[feature.iconColor].color,
                    }}
                  >
                    <span className="material-symbols-outlined">
                      {feature.icon}
                    </span>
                  </FeatureIcon>
                  <FeatureTitle>{feature.title}</FeatureTitle>
                </FeatureHeader>
                <FeatureDescription>{feature.description}</FeatureDescription>
              </FeatureContent>
              <FeatureVisual>
                <FeatureImage src={feature.image} alt={feature.imageAlt} />
              </FeatureVisual>
            </FeatureCard>
          ))}
        </FeaturesGrid>
      </Container>
    </FeaturesSection>
  )
}

const FeaturesSection = styled("section")({
  padding: "5rem 0",
})

const Container = styled(Box)({
  maxWidth: 1200,
  margin: "0 auto",
  padding: "0 1.5rem",
})

const FeaturesHeader = styled(Box)({
  display: "flex",
  flexDirection: "column",
  gap: "1rem",
  marginBottom: "4rem",
  textAlign: "center",
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

const SectionTitleCenter = styled(Typography)({
  fontSize: FONT_SIZE.SECTION_TITLE,
  fontWeight: FONT_WEIGHT.BOLD,
  margin: 0,
  textAlign: "center",
})

const SectionDescription = styled(Typography)({
  color: TEXT_COLOR.SECONDARY,
  margin: 0,
  fontSize: FONT_SIZE.SECTION_SUBTITLE,
  textAlign: "center",
  maxWidth: 600,
  marginLeft: "auto",
  marginRight: "auto",
})

const FeaturesGrid = styled(Box)({
  display: "grid",
  gridTemplateColumns: "1fr",
  gap: "2rem",
  "@media (min-width: 768px)": {
    gridTemplateColumns: "repeat(2, 1fr)",
  },
})

const FeatureCard = styled(Box)({
  backgroundColor: BG_COLOR.WHITE,
  border: `1px solid ${BORDER_COLOR.DEFAULT}`,
  borderRadius: 12,
  overflow: "hidden",
  display: "flex",
  flexDirection: "column",
})

const FeatureContent = styled(Box)({
  padding: "1.5rem",
  display: "flex",
  flexDirection: "column",
  gap: "0.5rem",
})

const FeatureIcon = styled(Box)({ ...ICON_BOX })

const FeatureHeader = styled(Box)({
  display: "flex",
  alignItems: "flex-start",
  gap: "0.75rem",
  marginBottom: "0.5rem",
})

const FeatureTitle = styled(Typography)({
  fontSize: FONT_SIZE.CARD_TITLE,
  fontWeight: FONT_WEIGHT.BOLD,
  margin: 0,
})

const FeatureDescription = styled(Typography)({
  fontSize: FONT_SIZE.BODY,
  color: TEXT_COLOR.SECONDARY,
  lineHeight: LINE_HEIGHT.NORMAL,
  margin: 0,
})

const FeatureVisual = styled(Box)({
  backgroundColor: BG_COLOR.CARD,
  height: 256,
  margin: "0 1.5rem 1.5rem",
  borderRadius: 8,
  border: `1px solid ${BORDER_COLOR.DEFAULT}`,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  overflow: "hidden",
})

const FeatureImage = styled("img")({
  width: "100%",
  height: "100%",
  objectFit: "cover",
  borderRadius: 8,
})
