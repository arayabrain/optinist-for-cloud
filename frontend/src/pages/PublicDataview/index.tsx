import { useEffect } from "react"

import { Box, styled } from "@mui/material"

import DataviewRecords from "components/Dataview/DataviewRecords"
import PublicDataviewWrapper from "components/Dataview/PublicDataviewWrapper"
import { trackEvent } from "utils/analytics"

const PublicDataview = () => {
  useEffect(() => {
    trackEvent("view_public_data")
  }, [])

  return (
    <>
      <PageWrapper>
        <Title>OptiNiSt Public Repository</Title>
        <PublicDataviewWrapper>
          <DataviewRecords />
        </PublicDataviewWrapper>
      </PageWrapper>
    </>
  )
}

const PageWrapper = styled(Box)({
  paddingTop: 32,
})

const Title = styled("h1")(() => ({
  fontSize: 24,
  fontWeight: 600,
  color: "#000000",
  marginBottom: 24,
  marginTop: 0,
}))

export default PublicDataview
