import { memo, useContext } from "react"
import { useSelector } from "react-redux"

import Box from "@mui/material/Box"
import Collapse from "@mui/material/Collapse"
import Table from "@mui/material/Table"
import TableBody from "@mui/material/TableBody"
import TableCell from "@mui/material/TableCell"
import TableHead from "@mui/material/TableHead"
import TableRow from "@mui/material/TableRow"
import Typography from "@mui/material/Typography"

import { NWBDownloadButton } from "components/Workspace/Experiment/Button/DownloadButton"
import { ExperimentStatusIcon } from "components/Workspace/Experiment/ExperimentStatusIcon"
import { ExperimentUidContext } from "components/Workspace/Experiment/ExperimentTable"
import {
  selectExperimentFunctionHasNWB,
  selectExperimentFunctionMessage,
  selectExperimentFunctionName,
  selectExperimentFunctionNodeIdList,
  selectExperimentFunctionStatus,
  selectExperimentIsRemoteSynced,
} from "store/slice/Experiments/ExperimentsSelectors"
import { arrayEqualityFn } from "utils/EqualityUtils"

interface CollapsibleTableProps {
  open: boolean
}

export const CollapsibleTable = memo(function CollapsibleTable({
  open,
}: CollapsibleTableProps) {
  return (
    <TableRow>
      <TableCell sx={{ paddingBottom: 0, paddingTop: 0 }} colSpan={12}>
        <Collapse in={open} timeout="auto" unmountOnExit>
          <Box margin={1}>
            <Typography variant="h6" gutterBottom component="div">
              Details
            </Typography>
            <Table size="small" aria-label="purchases">
              <Head />
              <Body />
            </Table>
          </Box>
        </Collapse>
      </TableCell>
    </TableRow>
  )
})

const Head = memo(function Head() {
  return (
    <TableHead>
      <TableRow>
        <TableCell>Function</TableCell>
        <TableCell>nodeID</TableCell>
        <TableCell>Success</TableCell>
        <TableCell>NWB</TableCell>
      </TableRow>
    </TableHead>
  )
})

const Body = memo(function Body() {
  const uid = useContext(ExperimentUidContext)
  const nodeIdList = useSelector(
    selectExperimentFunctionNodeIdList(uid),
    arrayEqualityFn,
  )
  return (
    <TableBody>
      {nodeIdList.map((nodeId) => (
        <TableRowOfFunction key={nodeId} nodeId={nodeId} />
      ))}
    </TableBody>
  )
})

interface TableRowOfFunctionProps {
  nodeId: string
}

const TableRowOfFunction = memo(function TableRowOfFunction({
  nodeId,
}: TableRowOfFunctionProps) {
  const uid = useContext(ExperimentUidContext)
  const name = useSelector(selectExperimentFunctionName(uid, nodeId))
  const status = useSelector(selectExperimentFunctionStatus(uid, nodeId))
  const hasNWB = useSelector(selectExperimentFunctionHasNWB(uid, nodeId))
  const message = useSelector(selectExperimentFunctionMessage(uid, nodeId))
  const isRemoteSynced = useSelector(selectExperimentIsRemoteSynced(uid))

  return (
    <TableRow key={nodeId}>
      <TableCell component="th" scope="row">
        {name}
      </TableCell>
      <TableCell>{nodeId}</TableCell>
      <TableCell>
        <ExperimentStatusIcon status={status} message={message} />
      </TableCell>
      <TableCell>
        <NWBDownloadButton
          name={name}
          nodeId={nodeId}
          hasNWB={hasNWB}
          isRemoteSynced={isRemoteSynced}
        />
      </TableCell>
    </TableRow>
  )
})
