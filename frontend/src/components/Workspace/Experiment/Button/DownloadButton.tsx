import { memo, useContext, useState, useRef } from "react"
import { useSelector } from "react-redux"

import { useSnackbar } from "notistack"

import CloudQueueIcon from "@mui/icons-material/CloudQueue"
import SimCardDownloadOutlinedIcon from "@mui/icons-material/SimCardDownloadOutlined"
import { Tooltip } from "@mui/material"
import IconButton from "@mui/material/IconButton"

import {
  downloadExperimentConfigApi,
  downloadExperimentNwbApi,
} from "api/experiments/Experiments"
import { downloadWorkflowConfigApi } from "api/workflow/Workflow"
import { ExperimentUidContext } from "components/Workspace/Experiment/ExperimentTable"
import { selectCurrentWorkspaceId } from "store/slice/Workspace/WorkspaceSelector"

interface NWBDownloadButtonProps {
  name: string
  nodeId?: string
  hasNWB: boolean
  isRemoteSynced: boolean
}

export const NWBDownloadButton = memo(function NWBDownloadButton({
  name,
  nodeId,
  hasNWB,
  isRemoteSynced,
}: NWBDownloadButtonProps) {
  const workspaceId = useSelector(selectCurrentWorkspaceId)
  const uid = useContext(ExperimentUidContext)
  const ref = useRef<HTMLAnchorElement | null>(null)
  const [url, setFileUrl] = useState<string>()
  const { enqueueSnackbar } = useSnackbar()

  const onClick = async () => {
    if (!workspaceId) return
    try {
      const responseData = await downloadExperimentNwbApi(
        workspaceId!,
        uid,
        nodeId,
      )
      const url = URL.createObjectURL(new Blob([responseData]))
      setFileUrl(url)
      setTimeout(() => {
        ref.current?.click()
        URL.revokeObjectURL(url)
      }, 100)
    } catch (error) {
      enqueueSnackbar("File not found", { variant: "error" })
    }
  }

  return (
    <>
      {isRemoteSynced ? (
        <>
          <IconButton onClick={onClick} color="primary" disabled={!hasNWB}>
            <SimCardDownloadOutlinedIcon />
          </IconButton>
          <a
            href={url}
            download={`nwb_${name}.nwb`}
            className="hidden"
            ref={ref}
            data-testid="nwb-download-link"
          >
            {/* 警告が出るので空文字を入れておく */}{" "}
          </a>
        </>
      ) : (
        <Tooltip title="Data is unsynchronized">
          <CloudQueueIcon color="disabled" style={{ padding: "8px" }} />
        </Tooltip>
      )}
    </>
  )
})

export const SnakemakeDownloadButton = memo(function SnakemakeDownloadButton() {
  const workspaceId = useSelector(selectCurrentWorkspaceId)
  const uid = useContext(ExperimentUidContext)
  const ref = useRef<HTMLAnchorElement | null>(null)
  const [url, setFileUrl] = useState<string>()
  const { enqueueSnackbar } = useSnackbar()

  const onClick = async () => {
    try {
      const responseData = await downloadExperimentConfigApi(workspaceId!, uid)
      const url = URL.createObjectURL(new Blob([responseData]))
      setFileUrl(url)
      setTimeout(() => {
        ref.current?.click()
        URL.revokeObjectURL(url)
      }, 100)
    } catch (error) {
      enqueueSnackbar("File not found", { variant: "error" })
    }
  }

  return (
    <>
      <IconButton onClick={onClick}>
        <SimCardDownloadOutlinedIcon color="primary" />
      </IconButton>
      <a
        href={url}
        download={`snakemake_${uid}.yaml`}
        className="hidden"
        ref={ref}
        data-testid="snakemake-download-link"
      >
        {/* 警告が出るので空文字を入れておく */}{" "}
      </a>
    </>
  )
})

export const WorkflowDownloadButton = memo(function WorkflowDownloadButton() {
  const workspaceId = useSelector(selectCurrentWorkspaceId)
  const uid = useContext(ExperimentUidContext)
  const ref = useRef<HTMLAnchorElement | null>(null)
  const [url, setFileUrl] = useState<string>()
  const { enqueueSnackbar } = useSnackbar()

  const onClick = async () => {
    try {
      const responseData = await downloadWorkflowConfigApi(workspaceId!, uid)
      const url = URL.createObjectURL(new Blob([responseData]))
      setFileUrl(url)
      setTimeout(() => {
        ref.current?.click()
        URL.revokeObjectURL(url)
      }, 100)
    } catch (error) {
      enqueueSnackbar("File not found", { variant: "error" })
    }
  }

  return (
    <>
      <IconButton onClick={onClick} data-testid="workflow-download-button">
        <SimCardDownloadOutlinedIcon color="primary" />
      </IconButton>
      <a
        href={url}
        download={`workflow_${uid}.yaml`}
        className="hidden"
        ref={ref}
        data-testid="workflow-download-link"
      >
        {/* 警告が出るので空文字を入れておく */}{" "}
      </a>
    </>
  )
})
