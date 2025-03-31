// components/ExternalLinkButton.tsx
import { FC, CSSProperties } from "react"

import { Launch } from "@mui/icons-material"
import { Tooltip } from "@mui/material"

interface ExternalLinkButtonProps {
  url: string
  tooltip?: string
  linkStyle?: CSSProperties
  iconStyle?: CSSProperties
}

const ExternalLinkButton: FC<ExternalLinkButtonProps> = ({
  url,
  tooltip = "Check Documentation",
  linkStyle = {},
  iconStyle = {},
}) => (
  <Tooltip title={tooltip} placement="top" arrow>
    <a href={url} target="_blank" rel="noopener noreferrer" style={linkStyle}>
      <Launch style={iconStyle} />
    </a>
  </Tooltip>
)

export default ExternalLinkButton
