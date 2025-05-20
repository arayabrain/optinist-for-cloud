import {
  ChangeEvent,
  createContext,
  useContext,
  FC,
  Fragment,
  memo,
  useEffect,
  useState,
  FocusEvent,
  MouseEvent,
} from "react"
import { useSelector, useDispatch } from "react-redux"

import { useSnackbar } from "notistack"

import ContentCopyIcon from "@mui/icons-material/ContentCopy"
import DeleteIcon from "@mui/icons-material/Delete"
import KeyboardArrowDownIcon from "@mui/icons-material/KeyboardArrowDown"
import KeyboardArrowUpIcon from "@mui/icons-material/KeyboardArrowUp"
import ReplayIcon from "@mui/icons-material/Replay"
import { Tooltip } from "@mui/material"
import Alert from "@mui/material/Alert"
import AlertTitle from "@mui/material/AlertTitle"
import Box from "@mui/material/Box"
import Button from "@mui/material/Button"
import Checkbox from "@mui/material/Checkbox"
import IconButton from "@mui/material/IconButton"
import Paper from "@mui/material/Paper"
import { styled } from "@mui/material/styles"
import Table from "@mui/material/Table"
import TableBody from "@mui/material/TableBody"
import TableCell, { tableCellClasses } from "@mui/material/TableCell"
import TableContainer from "@mui/material/TableContainer"
import TableHead from "@mui/material/TableHead"
import TablePagination from "@mui/material/TablePagination"
import TableRow from "@mui/material/TableRow"
import TableSortLabel from "@mui/material/TableSortLabel"
import Typography from "@mui/material/Typography"

import { renameExperimentApi } from "api/experiments/Experiments"
import { ConfirmDialog } from "components/common/ConfirmDialog"
import { useLocalStorage } from "components/utils/LocalStorageUtil"
import { DeleteButton } from "components/Workspace/Experiment/Button/DeleteButton"
import {
  NWBDownloadButton,
  SnakemakeDownloadButton,
  WorkflowDownloadButton,
} from "components/Workspace/Experiment/Button/DownloadButton"
import { RemoteSyncButton } from "components/Workspace/Experiment/Button/RemoteSyncButton"
import { ReproduceButton } from "components/Workspace/Experiment/Button/ReproduceButton"
import { CollapsibleTable } from "components/Workspace/Experiment/CollapsibleTable"
import { ExperimentStatusIcon } from "components/Workspace/Experiment/ExperimentStatusIcon"
import {
  copyExperimentByList,
  deleteExperimentByList,
  getExperiments,
} from "store/slice/Experiments/ExperimentsActions"
import {
  selectExperimentsStatusIsUninitialized,
  selectExperimentsStatusIsFulfilled,
  selectExperimentStartedAt,
  selectExperimentFinishedAt,
  selectExperimentName,
  selectExperimentStatus,
  selectExperimentsStatusIsError,
  selectExperimentsErrorMessage,
  selectExperimentList,
  selectExperimentHasNWB,
  selectExperimentDataUsage,
  selectExperimentIsRemoteSynced,
} from "store/slice/Experiments/ExperimentsSelectors"
import { ExperimentSortKeys } from "store/slice/Experiments/ExperimentsType"
import {
  selectPipelineIsStartedSuccess,
  selectPipelineLatestUid,
} from "store/slice/Pipeline/PipelineSelectors"
import { clearCurrentPipeline } from "store/slice/Pipeline/PipelineSlice"
import {
  selectCurrentWorkspaceId,
  selectIsWorkspaceOwner,
} from "store/slice/Workspace/WorkspaceSelector"
import { AppDispatch, RootState } from "store/store"
import { convertBytes } from "utils"

export const ExperimentUidContext = createContext<string>("")

export const ExperimentTable: FC = () => {
  const isUninitialized = useSelector(selectExperimentsStatusIsUninitialized)
  const isFulfilled = useSelector(selectExperimentsStatusIsFulfilled)
  const isError = useSelector(selectExperimentsStatusIsError)
  const dispatch = useDispatch<AppDispatch>()
  useEffect(() => {
    if (!isUninitialized) return
    const timeout = setTimeout(() => dispatch(getExperiments()), 1000)
    return () => {
      clearTimeout(timeout)
    }
  }, [dispatch, isUninitialized])

  if (isFulfilled) {
    return <TableImple />
  } else if (isError) {
    return <ExperimentsErrorView />
  } else {
    return null
  }
}

const ExperimentsErrorView: FC = () => {
  const message = useSelector(selectExperimentsErrorMessage)
  return (
    <Alert severity="error">
      <AlertTitle>failed to get experiments...</AlertTitle>
      {message}
    </Alert>
  )
}

const LOCAL_STORAGE_KEY_PER_PAGE = "optinist_experiment_table_per_page"

const TableImple = memo(function TableImple() {
  const isOwner = useSelector(selectIsWorkspaceOwner)
  const currentPipelineUid = useSelector(selectPipelineLatestUid)
  const experimentList = useSelector(selectExperimentList)
  const experimentListValues = Object.values(experimentList)
  const experimentListKeys = Object.keys(experimentList)
  const dispatch = useDispatch<AppDispatch>()
  const [checkedList, setCheckedList] = useState<string[]>([])
  const [open, setOpen] = useState(false)
  const [openCopy, setOpenCopy] = useState(false)
  const isRunning = useSelector((state: RootState) => {
    const currentUid = selectPipelineLatestUid(state)
    const isPending = selectPipelineIsStartedSuccess(state)
    return checkedList.includes(currentUid as string) && isPending
  })
  const { enqueueSnackbar } = useSnackbar()

  const onClickReload = () => {
    dispatch(getExperiments())
  }
  const [order, setOrder] = useState<Order>("desc")
  const [sortTarget, setSortTarget] =
    useState<keyof ExperimentSortKeys>("startedAt")
  const sortHandler = (property: keyof ExperimentSortKeys) => () => {
    const isAsc = sortTarget === property && order === "asc"
    setOrder(isAsc ? "desc" : "asc")
    setSortTarget(property)
  }

  const onClickCopy = () => {
    setOpenCopy(true)
  }

  const onClickOkCopy = () => {
    dispatch(copyExperimentByList(checkedList))
      .unwrap()
      .then(() => {
        dispatch(getExperiments())
        enqueueSnackbar("Record Copied", { variant: "success" })
      })
      .catch(() => {
        enqueueSnackbar("Failed to copy", { variant: "error" })
      })
    setCheckedList([])
    setOpenCopy(false)
  }

  const onCheckBoxClick = (uid: string) => {
    if (checkedList.includes(uid)) {
      setCheckedList(checkedList.filter((v) => v !== uid))
    } else {
      setCheckedList([...checkedList, uid])
    }
  }

  const onChangeAllCheck = (checked: boolean) => {
    if (checked) {
      setCheckedList(experimentListValues.map((experiment) => experiment.uid))
    } else {
      setCheckedList([])
    }
  }

  const recordsIsEmpty = experimentListKeys.length === 0

  const onClickDelete = () => {
    setOpen(true)
  }
  const onClickOk = () => {
    dispatch(deleteExperimentByList(checkedList))
      .unwrap()
      .then(() => {
        // do nothing.
      })
      .catch(() => {
        enqueueSnackbar("Failed to delete", { variant: "error" })
      })

    checkedList.filter((v) => v === currentPipelineUid).length > 0 &&
      dispatch(clearCurrentPipeline())
    setCheckedList([])
    setOpen(false)
  }

  const [page, setPage] = useState(0)

  const handleChangePage = (event: unknown, newPage: number) => {
    setPage(newPage)
  }

  const [rowsPerPage, setRowsPerPage] = useLocalStorage(
    LOCAL_STORAGE_KEY_PER_PAGE,
    10,
    (value) => {
      return isNaN(value) ? 10 : value
    },
  )
  const handleChangeRowsPerPage = (event: ChangeEvent<HTMLInputElement>) => {
    const newValue = parseInt(event.target.value, 10)
    setRowsPerPage(newValue)
    setPage(0)
  }

  // Avoid a layout jump when reaching the last page with empty rows.
  const emptyRows =
    page > 0
      ? Math.max(0, (1 + page) * rowsPerPage - experimentListKeys.length)
      : 0

  return (
    <Box sx={{ display: "flex", flexDirection: "column" }}>
      <Box
        sx={{
          display: "flex",
          justifyContent: "flex-end",
          alignItems: "center",
        }}
      >
        {!recordsIsEmpty && (
          <Typography sx={{ flexGrow: 1, m: 1 }}>
            {checkedList.length} selected
          </Typography>
        )}
        <Button
          sx={{
            margin: (theme) => theme.spacing(0, 1, 1, 1),
          }}
          variant="outlined"
          endIcon={<ContentCopyIcon />}
          onClick={onClickCopy}
          disabled={checkedList.length === 0 || isRunning}
        >
          COPY
        </Button>
        <Button
          sx={{
            margin: (theme) => theme.spacing(0, 1, 1, 0),
          }}
          variant="outlined"
          endIcon={<ReplayIcon />}
          onClick={onClickReload}
        >
          Reload
        </Button>
        {isOwner && (
          <Button
            data-testid="delete-selected-button"
            sx={{
              marginBottom: (theme) => theme.spacing(1),
            }}
            variant="outlined"
            color="error"
            endIcon={<DeleteIcon />}
            onClick={onClickDelete}
            disabled={checkedList.length === 0 || isRunning}
          >
            Delete
          </Button>
        )}
      </Box>
      <ConfirmDialog
        open={open}
        setOpen={setOpen}
        onConfirm={onClickOk}
        title="Delete records?"
        content={
          <>
            {checkedList.map((uid) => {
              const experiment = experimentList[uid]
              return (
                <Typography key={uid}>
                  ・
                  {experiment
                    ? `${experiment.name} (${uid})`
                    : `Unknown (${uid})`}
                </Typography>
              )
            })}
          </>
        }
        iconType="warning"
        confirmLabel="delete"
        confirmButtonColor="error"
      />
      <ConfirmDialog
        open={openCopy}
        setOpen={setOpenCopy}
        onConfirm={onClickOkCopy}
        title="Copy records?"
        content={
          <>
            {checkedList.map((uid) => (
              <Typography key={uid}>
                ・{experimentList[uid].name} ({uid})
              </Typography>
            ))}
          </>
        }
        iconType="warning"
        confirmLabel="copy"
      />
      <Paper
        elevation={0}
        variant="outlined"
        sx={{
          flexGlow: 1,
          height: "100%",
        }}
      >
        <TableContainer component={Paper} elevation={0}>
          <Table aria-label="collapsible table">
            <HeadItem
              order={order}
              sortTarget={sortTarget}
              sortHandler={sortHandler}
              allCheckIndeterminate={
                checkedList.length !== 0 &&
                checkedList.length !== Object.keys(experimentList).length
              }
              allChecked={
                checkedList.length === Object.keys(experimentList).length
              }
              onChangeAllCheck={onChangeAllCheck}
              checkboxVisible={!recordsIsEmpty}
              isOwner={isOwner}
            />
            <TableBody>
              {experimentListValues
                .sort(getComparator(order, sortTarget))
                .slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage)
                .map((expData) => (
                  <ExperimentUidContext.Provider
                    value={expData.uid}
                    key={expData.uid}
                  >
                    <RowItem
                      onCheckBoxClick={onCheckBoxClick}
                      checked={checkedList.includes(expData.uid)}
                      isOwner={isOwner}
                    />
                  </ExperimentUidContext.Provider>
                ))}
              {emptyRows > 0 && (
                <TableRow
                  style={{
                    height: 53 * emptyRows,
                  }}
                >
                  <TableCell colSpan={10} />
                </TableRow>
              )}
              {recordsIsEmpty && (
                <TableRow>
                  <TableCell colSpan={10}>
                    <Typography
                      sx={{
                        color: (theme) => theme.palette.text.secondary,
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        height: "300px",
                        textAlign: "center",
                      }}
                      variant="h6"
                    >
                      No Rows...
                    </Typography>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
        <TablePagination
          rowsPerPageOptions={[5, 10, 25]}
          component="div"
          count={experimentListKeys.length}
          rowsPerPage={rowsPerPage}
          page={page}
          onPageChange={handleChangePage}
          onRowsPerPageChange={handleChangeRowsPerPage}
        />
      </Paper>
    </Box>
  )
})

interface HeadItemProps {
  order: Order
  sortTarget: keyof ExperimentSortKeys
  sortHandler: (property: keyof ExperimentSortKeys) => () => void
  allChecked: boolean
  onChangeAllCheck: (checked: boolean) => void
  allCheckIndeterminate: boolean
  checkboxVisible: boolean
  isOwner: boolean
}

const HeadItem = memo(function HeadItem({
  order,
  sortTarget,
  sortHandler,
  allChecked,
  onChangeAllCheck,
  allCheckIndeterminate,
  checkboxVisible,
  isOwner,
}: HeadItemProps) {
  return (
    <TableHead>
      <TableRow>
        <TableCell padding="checkbox">
          <Checkbox
            data-testid="select-all-checkbox"
            sx={{ visibility: checkboxVisible ? "visible" : "hidden" }}
            checked={allChecked}
            indeterminate={allCheckIndeterminate}
            onChange={(e) => onChangeAllCheck(e.target.checked)}
          />
        </TableCell>
        <TableCell />
        <TableCell>
          <TableSortLabel
            active={sortTarget === "startedAt"}
            direction={order}
            onClick={sortHandler("startedAt")}
          >
            Timestamp
          </TableSortLabel>
        </TableCell>
        <TableCell>
          <TableSortLabel
            active={sortTarget === "uid"}
            direction={order}
            onClick={sortHandler("uid")}
          >
            ID
          </TableSortLabel>
        </TableCell>
        <TableCell>
          <TableSortLabel
            active={sortTarget === "name"}
            direction={order}
            onClick={sortHandler("name")}
          >
            Name
          </TableSortLabel>
        </TableCell>
        <TableCell>
          <TableSortLabel
            active={sortTarget === "data_usage"}
            direction={order}
            onClick={sortHandler("data_usage")}
          >
            Data size
          </TableSortLabel>
        </TableCell>
        <TableCell>Success</TableCell>
        <TableCell>Reproduce</TableCell>
        <TableCell>Workflow</TableCell>
        <TableCell>Snakemake</TableCell>
        <TableCell>NWB</TableCell>
        <TableCell>Sync</TableCell>
        {isOwner && <TableCell>Delete</TableCell>}
      </TableRow>
    </TableHead>
  )
})

interface RowItemProps {
  onCheckBoxClick: (uid: string) => void
  checked: boolean
  isOwner: boolean
}

const RowItem = memo(function RowItem({
  onCheckBoxClick,
  checked,
  isOwner,
}: RowItemProps) {
  const workspaceId = useSelector(selectCurrentWorkspaceId)
  const uid = useContext(ExperimentUidContext)
  const startedAt = useSelector(selectExperimentStartedAt(uid))
  const finishedAt = useSelector(selectExperimentFinishedAt(uid))
  const dataUsage = useSelector(selectExperimentDataUsage(uid))
  const status = useSelector(selectExperimentStatus(uid))
  const name = useSelector(selectExperimentName(uid))
  const hasNWB = useSelector(selectExperimentHasNWB(uid))
  const [open, setOpen] = useState(false)
  const [isEdit, setEdit] = useState(false)
  const [errorEdit, setErrorEdit] = useState("")
  const [valueEdit, setValueEdit] = useState(name)
  const dispatch = useDispatch<AppDispatch>()
  const { enqueueSnackbar } = useSnackbar()
  const newLocal = useSelector(selectExperimentIsRemoteSynced(uid))
  const isRemoteSynced = newLocal

  const onBlurEdit = (event: FocusEvent) => {
    event.preventDefault()
    if (errorEdit) return
    setTimeout(() => {
      setEdit(false)
      onSaveNewName()
    }, 300)
  }

  const onEdit = (event: MouseEvent) => {
    if (isEdit || errorEdit) return
    event.preventDefault()
    setEdit(true)
  }

  const onChangeName = (event: ChangeEvent<HTMLInputElement>) => {
    let errorEdit = ""
    if (!event.target.value.trim()) {
      errorEdit = "Name is empty"
    }
    setErrorEdit(errorEdit)
    setValueEdit(event.target.value)
  }

  const onSaveNewName = async () => {
    if (valueEdit === name || workspaceId === void 0) return

    try {
      await renameExperimentApi(workspaceId, uid, valueEdit)
    } catch (e) {
      enqueueSnackbar("Failed to rename", { variant: "error" })
    }

    dispatch(getExperiments())
  }

  return (
    <Fragment>
      <TableRow
        sx={{
          "& > *": {
            borderBottom: "unset",
          },
          [`& .${tableCellClasses.root}`]: {
            borderBottomWidth: 0,
          },
        }}
      >
        <TableCell padding="checkbox">
          <Checkbox onChange={() => onCheckBoxClick(uid)} checked={checked} />
        </TableCell>
        <TableCell>
          <IconButton
            aria-label="expand row"
            size="small"
            onClick={() => setOpen((prevOpen) => !prevOpen)}
          >
            {open ? <KeyboardArrowUpIcon /> : <KeyboardArrowDownIcon />}
          </IconButton>
        </TableCell>
        <TableCell
          sx={{ minWidth: 210, width: 210 }}
          component="th"
          scope="row"
        >
          {finishedAt == null ? (
            startedAt
          ) : (
            <>
              {/* date string format is YYYY-MM-DD HH:mm:ss */}
              <Typography variant="body2">{`${startedAt} - ${
                finishedAt.split(" ")[1]
              }`}</Typography>
              <Typography variant="body2">
                (elapsed{" "}
                {(new Date(finishedAt).getTime() -
                  new Date(startedAt).getTime()) /
                  1000}{" "}
                sec)
              </Typography>
            </>
          )}
        </TableCell>
        <TableCell>{uid}</TableCell>
        <TableCell
          sx={{ width: 160, position: "relative" }}
          onClick={isRemoteSynced ? onEdit : undefined}
        >
          {!isEdit ? (
            isRemoteSynced ? (
              valueEdit
            ) : (
              <Tooltip title="Data is unsynchronized">
                <Typography sx={{ color: "gray" }}>{valueEdit}</Typography>
              </Tooltip>
            )
          ) : (
            <>
              <Input
                placeholder="Name"
                error={!!errorEdit}
                onChange={onChangeName}
                autoFocus
                onBlur={onBlurEdit}
                value={valueEdit}
              />
              {errorEdit ? <TextError>{errorEdit}</TextError> : null}
            </>
          )}
        </TableCell>
        <TableCell>{convertBytes(dataUsage)}</TableCell>
        <TableCell>
          <ExperimentStatusIcon status={status} />
        </TableCell>
        <TableCell>
          <ReproduceButton />
        </TableCell>
        <TableCell>
          <WorkflowDownloadButton />
        </TableCell>
        <TableCell>
          <SnakemakeDownloadButton />
        </TableCell>
        <TableCell>
          <NWBDownloadButton
            name={uid}
            hasNWB={hasNWB}
            isRemoteSynced={isRemoteSynced}
          />
        </TableCell>
        <TableCell>
          <RemoteSyncButton />
        </TableCell>
        {isOwner && (
          <TableCell>
            {" "}
            <DeleteButton />
          </TableCell>
        )}
      </TableRow>
      <CollapsibleTable open={open} />
    </Fragment>
  )
})

const Input = styled("input")<{ error: boolean }>(({ error }) => ({
  width: "100%",
  border: "none",
  borderBottom: "1px solid",
  outline: "none",
  color: error ? "#d32f2f" : "",
  borderColor: error ? "#d32f2f" : "",
}))

const TextError = styled(Typography)(() => ({
  color: "#d32f2f",
  fontSize: 12,
  height: 12,
  position: "absolute",
  bottom: 12,
}))

type Order = "asc" | "desc"

function getComparator<Key extends keyof ExperimentSortKeys>(
  order: Order,
  orderBy: Key,
): (
  a: { [key in Key]: number | string },
  b: { [key in Key]: number | string },
) => number {
  return order === "desc"
    ? (a, b) => descendingComparator(a, b, orderBy)
    : (a, b) => -descendingComparator(a, b, orderBy)
}

function descendingComparator<T>(a: T, b: T, orderBy: keyof T) {
  if (b[orderBy] < a[orderBy]) {
    return -1
  }
  if (b[orderBy] > a[orderBy]) {
    return 1
  }
  return 0
}
