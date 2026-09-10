import { Box, styled, Typography, keyframes } from "@mui/material"

import { FONT_SIZE, FONT_WEIGHT, TEXT_COLOR, BG_COLOR } from "const/Style"

const formats = [
  "HDF5",
  "MATLAB",
  "NWB",
  "CSV",
  "TIFF",
  "Fluorescence",
  "Behavioral",
]

export const FormatMarquee = () => {
  return (
    <MarqueeSection id="formats">
      <Container>
        <MarqueeLabel>Works With Your Data</MarqueeLabel>
      </Container>
      <Marquee>
        <MarqueeContent>
          {formats.map((format, index) => (
            <MarqueeItem key={index}>{format}</MarqueeItem>
          ))}
        </MarqueeContent>
        <MarqueeContent aria-hidden="true">
          {formats.map((format, index) => (
            <MarqueeItem key={`dup-${index}`}>{format}</MarqueeItem>
          ))}
        </MarqueeContent>
      </Marquee>
    </MarqueeSection>
  )
}

const marqueeScroll = keyframes`
  from {
    transform: translateX(0);
  }
  to {
    transform: translateX(-100%);
  }
`

const MarqueeSection = styled("section")({
  backgroundColor: BG_COLOR.DARK,
  padding: "3rem 0",
  overflow: "hidden",
})

const Container = styled(Box)({
  maxWidth: 1200,
  margin: "0 auto",
  padding: "0 1.5rem",
})

const MarqueeLabel = styled(Typography)({
  color: TEXT_COLOR.MARQUEE_LABEL,
  fontSize: FONT_SIZE.SECTION_TITLE,
  fontWeight: FONT_WEIGHT.BOLD,
  textAlign: "center",
  margin: "0 0 2rem",
})

const Marquee = styled(Box)({
  display: "flex",
  overflow: "hidden",
  userSelect: "none",
  gap: "2rem",
})

const MarqueeContent = styled(Box)({
  flexShrink: 0,
  display: "flex",
  justifyContent: "space-around",
  minWidth: "100%",
  gap: "2rem",
  animation: `${marqueeScroll} 20s linear infinite`,
})

const MarqueeItem = styled("span")({
  color: TEXT_COLOR.WHITE_MUTED,
  fontWeight: FONT_WEIGHT.BLACK,
  fontSize: FONT_SIZE.DISPLAY_SM,
  padding: "0 2.5rem",
})
