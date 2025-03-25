import { PropsWithChildren, useRef, useState } from "react"

import { Box, Divider } from "@mui/material"

import { DRAWER_WIDTH, MAX_DRAWER_WIDTH } from "const/Layout"

export const ResizableSidebar = ({ children }: PropsWithChildren) => {
  const [width, setWidth] = useState(240)
  const isDragging = useRef(false)

  const handleMouseDown = () => {
    isDragging.current = true
    document.addEventListener("mousemove", handleMouseMove)
    document.addEventListener("mouseup", handleMouseUp)
  }

  const handleMouseMove = (e: MouseEvent) => {
    if (!isDragging.current) return
    setWidth(Math.max(180, e.clientX)) // min width = 180px
  }

  const handleMouseUp = () => {
    isDragging.current = false
    document.removeEventListener("mousemove", handleMouseMove)
    document.removeEventListener("mouseup", handleMouseUp)
  }

  return (
    <Box display="flex" height="100%">
      <Box
        width={width}
        minWidth={DRAWER_WIDTH}
        maxWidth={MAX_DRAWER_WIDTH}
        sx={{
          overflow: "auto",
          borderRight: "1px solid #ccc",
        }}
      >
        {children}
      </Box>
      <Divider
        onMouseDown={handleMouseDown}
        sx={{
          width: "2px",
          cursor: "col-resize",
          backgroundColor: "#ddd",
          "&:hover": { backgroundColor: "#bbb" },
        }}
      />
    </Box>
  )
}
