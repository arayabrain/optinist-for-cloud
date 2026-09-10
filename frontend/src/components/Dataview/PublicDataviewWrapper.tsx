import { FC, ReactNode } from "react"

import { Box, styled } from "@mui/material"

import { DATAVIEW_GRID_RESERVED_HEIGHT } from "components/Dataview/DataviewRecords"
import PublicLayout from "components/PublicLayout"
import { PUBLIC_FOOTER_HEIGHT } from "components/PublicLayout/PublicFooter"

const PublicDataviewWrapper: FC<{ children: ReactNode }> = ({ children }) => {
  return (
    <PublicLayout>
      <Box
        sx={{
          paddingTop: 2,
        }}
      >
        <DataviewContent>{children}</DataviewContent>
      </Box>
    </PublicLayout>
  )
}

// Part of the base grid reserve reclaimed for the grid on the public page,
// leaving the footer a small bottom margin within the viewport.
const FOOTER_BOTTOM_SLACK = 50

const DataviewContent = styled(Box)(() => ({
  width: "94vw",
  margin: "auto",
  marginTop: 15,
  // The grid sizes itself to the viewport; on the public page it is followed by
  // PublicFooter, so shrink it to keep the grid and footer within the viewport.
  "& > div": {
    height: `calc(100vh - ${
      DATAVIEW_GRID_RESERVED_HEIGHT + PUBLIC_FOOTER_HEIGHT - FOOTER_BOTTOM_SLACK
    }px)`,
  },
}))

export default PublicDataviewWrapper
