import { memo, useContext, useState } from "react"
import { useSelector, useDispatch } from "react-redux"

import { useSnackbar } from "notistack"

import CloudDownloadIcon from "@mui/icons-material/CloudDownloadOutlined"
import DoneIcon from "@mui/icons-material/Done"
import IconButton from "@mui/material/IconButton"

import { ConfirmDialog } from "components/common/ConfirmDialog"
import { ExperimentUidContext } from "components/Workspace/Experiment/ExperimentTable"
import { syncRemoteStorageExperiment } from "store/slice/Experiments/ExperimentsActions"
import {
  selectExperimentName,
  selectExperimentIsRemoteSynced,
} from "store/slice/Experiments/ExperimentsSelectors"
import {
  selectPipelineLatestUid,
  selectPipelineIsStartedSuccess,
} from "store/slice/Pipeline/PipelineSelectors"
import { AppDispatch, RootState } from "store/store"

export const CloudSyncButton = memo(function CloudSyncButton() {
  const dispatch = useDispatch<AppDispatch>()
  const uid = useContext(ExperimentUidContext)
  const isRunning = useSelector((state: RootState) => {
    const currentUid = selectPipelineLatestUid(state)
    const isPending = selectPipelineIsStartedSuccess(state)
    return uid === currentUid && isPending
  })
  const name = useSelector(selectExperimentName(uid))
  const isRemoteSynced = useSelector(selectExperimentIsRemoteSynced(uid))
  const [open, setOpen] = useState(false)
  const { enqueueSnackbar } = useSnackbar()

  const openDialog = () => {
    setOpen(true)
  }
  const handleSyncRemote = () => {
    dispatch(syncRemoteStorageExperiment(uid))
      .unwrap()
      .then(() => {
        enqueueSnackbar("Successfully synchronize", { variant: "success" })
      })
      .catch(() => {
        enqueueSnackbar("Failed to synchronize", { variant: "error" })
      })
  }

  return (
    <>
      {isRemoteSynced ? (
        <DoneIcon color="success" />
      ) : (
        <>
          <IconButton
            onClick={openDialog}
            disabled={isRunning}
            color="primary"
            style={{ padding: 0 }}
          >
            <CloudDownloadIcon />
          </IconButton>
          <ConfirmDialog
            open={open}
            setOpen={setOpen}
            onConfirm={handleSyncRemote}
            title="Sync remote storage record?"
            content={`${name} (${uid})`}
            confirmLabel="OK"
            iconType="info"
          />
        </>
      )}
    </>
  )
})
