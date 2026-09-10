import { useNavigate } from "react-router-dom"

import { Box, styled, Typography } from "@mui/material"

import {
  FONT_SIZE,
  FONT_WEIGHT,
  LINE_HEIGHT,
  TEXT_COLOR,
  BG_COLOR,
  BUTTON_BASE,
  ICON_BOX,
} from "const/Style"

interface Benefit {
  icon: string
  title: string
  description: string
}

const benefits: Benefit[] = [
  {
    icon: "science",
    title: "Standardized Analysis",
    description:
      "Share workflows alongside your results so others can " +
      "replicate your exact methods, eliminating methodological " +
      "variability between studies.",
  },
  {
    icon: "compare",
    title: "Objective Comparisons",
    description:
      "Compare results across labs with confidence\u2014same " +
      "methods, same parameters, truly comparable outcomes.",
  },
  {
    icon: "group_work",
    title: "Invite Collaborators",
    description:
      "Invite other labs to analyze their data using your exact " +
      "workflow and display results side-by-side.",
  },
]

export const PublicRepository = () => {
  const navigate = useNavigate()

  return (
    <PublicRepoSection id="public-repository">
      <Container>
        <SectionHeader>
          <SectionLabel>
            <span
              className="material-symbols-outlined"
              style={{ fontSize: FONT_SIZE.CARD_TITLE }}
            >
              public
            </span>
            Public Repository
          </SectionLabel>
          <SectionTitle>Araya OptiNiSt Public Repository</SectionTitle>
          <SectionSubtitle>
            Public data repositories exist, but they rarely ensure consistent
            analysis methods across datasets. Araya OptiNiSt changes that.
          </SectionSubtitle>
        </SectionHeader>
        <PublicRepoGrid>
          <PublicRepoContent>
            <PublicRepoBody>
              Labs can publish their analysis results alongside the exact
              workflows used—allowing other researchers to apply identical
              methods to their own data and contribute to a growing collection
              of directly comparable results.
            </PublicRepoBody>
            <Attribution>
              Built on{" "}
              <a
                href="https://github.com/oist/optinist"
                target="_blank"
                rel="noopener noreferrer"
              >
                OptiNiSt
              </a>{" "}
              by ARAYA and OIST (Okinawa Institute of Science and Technology)
            </Attribution>
            <PublicRepoBenefits>
              {benefits.map((benefit, index) => (
                <PublicRepoBenefit key={index}>
                  <BenefitIcon>
                    <span className="material-symbols-outlined">
                      {benefit.icon}
                    </span>
                  </BenefitIcon>
                  <div>
                    <BenefitTitle>{benefit.title}</BenefitTitle>
                    <BenefitDescription>
                      {benefit.description}
                    </BenefitDescription>
                  </div>
                </PublicRepoBenefit>
              ))}
            </PublicRepoBenefits>
          </PublicRepoContent>
          <PublicRepoVisual>
            <PublicRepoGlow />
            <PublicRepoImage
              src="/images/landing-page/optinist_public_repository.png"
              alt="OptiNiSt Public Repository"
            />
            <RepoButton onClick={() => navigate("/public")}>
              Explore the Public Repository
            </RepoButton>
          </PublicRepoVisual>
        </PublicRepoGrid>
      </Container>
    </PublicRepoSection>
  )
}

const PublicRepoSection = styled("section")({
  background: "linear-gradient(135deg, #1e3a5f 0%, #2d4a6f 50%, #1a3352 100%)",
  padding: "5rem 0",
  position: "relative",
  overflow: "hidden",
  "&::before": {
    content: "\"\"",
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    backgroundImage:
      "url(\"data:image/svg+xml,%3Csvg width='60' height='60' " +
      "viewBox='0 0 60 60' xmlns='http://www.w3.org/2000/svg'" +
      "%3E%3Cg fill='none' fill-rule='evenodd'%3E%3Cg " +
      "fill='%23ffffff' fill-opacity='0.03'%3E%3Cpath " +
      "d='M36 34v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4zm0-30V0h-2v4" +
      "h-4v2h4v4h2V6h4V4h-4zM6 34v-4H4v4H0v2h4v4h2v-4h4v-2H" +
      "6zM6 4V0H4v4H0v2h4v4h2V6h4V4H6z'/%3E%3C/g%3E%3C/g" +
      "%3E%3C/svg%3E\")",
    opacity: 0.5,
  },
})

const Container = styled(Box)({
  maxWidth: 1200,
  margin: "0 auto",
  padding: "0 1.5rem",
  position: "relative",
  zIndex: 1,
})

const SectionHeader = styled(Box)({
  display: "flex",
  flexDirection: "column",
  textAlign: "center",
  marginBottom: "3rem",
})

const SectionLabel = styled(Box)({
  display: "inline-flex",
  alignSelf: "flex-start",
  alignItems: "center",
  gap: "0.5rem",
  backgroundColor: BG_COLOR.BENEFIT_ICON,
  color: TEXT_COLOR.WHITE,
  fontWeight: FONT_WEIGHT.BOLD,
  fontSize: FONT_SIZE.SMALL,
  padding: "0.5rem 1rem",
  borderRadius: 9999,
  width: "fit-content",
})

const SectionTitle = styled(Typography)({
  fontSize: FONT_SIZE.SECTION_TITLE,
  fontWeight: FONT_WEIGHT.BOLD,
  margin: "0 0 1rem",
  color: TEXT_COLOR.WHITE,
})

const SectionSubtitle = styled(Typography)({
  color: TEXT_COLOR.WHITE_SOFT,
  margin: 0,
  lineHeight: LINE_HEIGHT.NORMAL,
  fontSize: FONT_SIZE.SECTION_SUBTITLE,
  maxWidth: 600,
  marginLeft: "auto",
  marginRight: "auto",
})

const PublicRepoGrid = styled(Box)({
  display: "grid",
  gridTemplateColumns: "1fr",
  gap: "3rem",
  alignItems: "center",
  "@media (min-width: 1024px)": {
    gridTemplateColumns: "1fr 1fr",
  },
})

const PublicRepoContent = styled(Box)({
  display: "flex",
  flexDirection: "column",
  gap: "1rem",
})

const PublicRepoBody = styled(Typography)({
  color: TEXT_COLOR.WHITE_SOFT,
  margin: 0,
  lineHeight: LINE_HEIGHT.NORMAL,
  fontSize: FONT_SIZE.BODY,
})

const Attribution = styled(Typography)({
  fontSize: FONT_SIZE.SMALL,
  color: TEXT_COLOR.WHITE_MUTED,
  fontStyle: "italic",
  "& a": {
    color: TEXT_COLOR.WHITE_SOFT,
    textDecoration: "underline",
    "&:hover": {
      color: TEXT_COLOR.WHITE,
    },
  },
})

const PublicRepoBenefits = styled(Box)({
  display: "flex",
  flexDirection: "column",
  gap: "1.25rem",
  marginTop: "1rem",
})

const PublicRepoBenefit = styled(Box)({
  display: "flex",
  alignItems: "flex-start",
  gap: "1rem",
})

const BenefitIcon = styled(Box)({
  ...ICON_BOX,
  width: 44,
  height: 44,
  borderRadius: 10,
  backgroundColor: BG_COLOR.BENEFIT_ICON,
  color: TEXT_COLOR.WHITE,
})

const BenefitTitle = styled(Typography)({
  fontWeight: FONT_WEIGHT.BOLD,
  margin: "0 0 0.25rem",
  color: TEXT_COLOR.WHITE,
})

const BenefitDescription = styled(Typography)({
  fontSize: FONT_SIZE.BODY,
  color: TEXT_COLOR.WHITE_SOFT,
  margin: 0,
  lineHeight: LINE_HEIGHT.NORMAL,
})

const PublicRepoVisual = styled(Box)({
  position: "relative",
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
})

const PublicRepoGlow = styled(Box)({
  position: "absolute",
  inset: "-1rem",
  background: "rgba(255, 255, 255, 0.1)",
  filter: "blur(40px)",
  borderRadius: 20,
})

const PublicRepoImage = styled("img")({
  position: "relative",
  width: "100%",
  height: "auto",
  borderRadius: 12,
  boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.25)",
})

const RepoButton = styled("button")({
  ...BUTTON_BASE,
  position: "relative",
  height: 48,
  padding: "0 2rem",
  backgroundColor: BG_COLOR.WHITE,
  color: TEXT_COLOR.ACCENT,
  marginTop: "1.5rem",
  "&:hover": {
    backgroundColor: "#f3f4f6",
  },
})
