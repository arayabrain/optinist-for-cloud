import { FC, ReactNode } from "react"

import { Box } from "@mui/material"

import PublicFooter from "components/PublicLayout/PublicFooter"
import PublicHeader from "components/PublicLayout/PublicHeader"

const PublicLayout: FC<{ children: ReactNode }> = ({ children }) => {
  return (
    <Box sx={{ height: "100%", width: "100%" }}>
      <PublicHeader />
      <Box>{children}</Box>
      <PublicFooter />
    </Box>
  )
}

export default PublicLayout
