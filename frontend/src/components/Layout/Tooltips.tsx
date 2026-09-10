import { FC, MouseEvent, useState } from "react"
import { useDispatch, useSelector } from "react-redux"
import { Link } from "react-router-dom"

import { useSnackbar } from "notistack"

import {
  Addchart,
  Description,
  GitHub,
  MenuBook,
  OpenInNew,
  PrivacyTip,
} from "@mui/icons-material"
import {
  Divider,
  ListItemIcon,
  ListItemText,
  Menu,
  MenuItem,
  Tooltip,
} from "@mui/material"
import IconButton from "@mui/material/IconButton"

import { ConfirmDialog } from "components/common/ConfirmDialog"
import { getExperiments } from "store/slice/Experiments/ExperimentsActions"
import { reset } from "store/slice/VisualizeItem/VisualizeItemSlice"
import { importSampleData } from "store/slice/Workflow/WorkflowActions"
import {
  selectActiveTab,
  selectCurrentWorkspaceId,
} from "store/slice/Workspace/WorkspaceSelector"
import { WORKSPACE_TABS } from "store/slice/Workspace/WorkspaceType"
import { AppDispatch } from "store/store"

const Tooltips: FC = () => {
  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null)
  const menuId = "documentation-menu"
  const open = Boolean(anchorEl)
  const handleClickMenuIcon = (event: MouseEvent<HTMLElement>) => {
    setAnchorEl(event.currentTarget)
  }
  const handleClose = () => {
    setAnchorEl(null)
  }
  const handleGoToDocClick = () => {
    window.open("https://optinist.readthedocs.io/en/latest/", "_blank")
  }

  const [dialogOpen, setDialogOpen] = useState(false)

  const dispatch: AppDispatch = useDispatch()
  const { enqueueSnackbar } = useSnackbar()
  const workspaceId = useSelector(selectCurrentWorkspaceId)
  const activeTab = useSelector(selectActiveTab)
  const isRecordTab = activeTab === WORKSPACE_TABS.RECORD
  const category = "tutorial"
  const workspaceReady = typeof workspaceId === "number"

  const handleImportSampleDataClick = () => {
    if (workspaceReady) {
      dispatch(importSampleData({ workspaceId, category }))
        .unwrap()
        .then(() => {
          enqueueSnackbar("Sample data import success", { variant: "success" })
          dispatch(reset())
          dispatch(getExperiments())
        })
        .catch(() => {
          enqueueSnackbar("Sample data import error", { variant: "error" })
        })
    }
  }

  return (
    <>
      <Tooltip title="GitHub repository">
        <IconButton
          href="https://github.com/arayabrain/araya-optinist"
          target="_blank"
        >
          <GitHub />
        </IconButton>
      </Tooltip>
      <Tooltip title="Documentation">
        <IconButton onClick={handleClickMenuIcon}>
          <MenuBook />
        </IconButton>
      </Tooltip>
      <Menu
        id={menuId}
        anchorEl={anchorEl}
        open={open}
        onClose={handleClose}
        MenuListProps={{
          "aria-labelledby": menuId,
          role: "listbox",
        }}
      >
        <MenuItem onClick={handleGoToDocClick}>
          <ListItemIcon>
            <OpenInNew />
          </ListItemIcon>
          <ListItemText>User Guide</ListItemText>
        </MenuItem>
        <MenuItem
          disabled={!isRecordTab}
          onClick={() => {
            handleClose()
            setDialogOpen(true)
          }}
        >
          <ListItemIcon>
            <Addchart />
          </ListItemIcon>
          <ListItemText>Import Sample Data</ListItemText>
        </MenuItem>
        <Divider />
        <MenuItem component={Link} to="/privacy" onClick={handleClose}>
          <ListItemIcon>
            <PrivacyTip />
          </ListItemIcon>
          <ListItemText>Privacy Policy</ListItemText>
        </MenuItem>
        <MenuItem component={Link} to="/terms" onClick={handleClose}>
          <ListItemIcon>
            <Description />
          </ListItemIcon>
          <ListItemText>Terms of Service</ListItemText>
        </MenuItem>
      </Menu>
      <ConfirmDialog
        open={dialogOpen}
        setOpen={setDialogOpen}
        onConfirm={handleImportSampleDataClick}
        title="Import sample data?"
        content={"sample data files and tutorial records will be imported."}
        iconType="info"
      />
    </>
  )
}

export default Tooltips
