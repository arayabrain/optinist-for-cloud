import { FC } from "react"
import { Link } from "react-router-dom"

import { Launch } from "@mui/icons-material"
import { Tooltip } from "@mui/material"

interface NodesLinkButtonProps {
  algoName: string
}

const NodesLinkButton: FC<NodesLinkButtonProps> = ({ algoName }) => {
  const algoNameMapping: { [key: string]: string } = {
    eta: "eta-event-triggered-average",
    cca: "cca-canonical-correlation-analysis",
    dpca: "dpca-demixed-principal-component-analysis",
    dca: "dca-dynamical-component-analysis",
    tsne: "tsne-t-distributed-stochastic-neighbor-embedding",
    glm: "glm-generalized-linear-model",
    lda: "lda-linear-discriminant-analysis",
    svm: "svm-support-vector-machine",
    granger: "granger-granger-causality-test",
    "lccd-cell-detection": "lccd-detect",
    "microscope-to-img": "microscope-to-image",
    "cnmf-multisession": "caiman-cnmf-multisession",
  }

  let formattedAlgoName = algoName.toLowerCase().replace(/_/g, "-")

  // Check if the formatted name exists in the mapping, otherwise keep it as is
  formattedAlgoName =
    algoNameMapping[formattedAlgoName as keyof typeof algoNameMapping] ||
    formattedAlgoName

  const parameterUrl = `https://optinist.readthedocs.io/en/latest/specifications/algorithm_nodes.html#${formattedAlgoName}`

  return (
    <Tooltip title="Check Documentation" placement="right" arrow>
      <a
        href={parameterUrl}
        target="_blank"
        rel="noopener noreferrer"
        style={{
          textDecoration: "underline",
          color: "inherit",
          cursor: "pointer",
          marginLeft: "5px",
          display: "inline-flex",
          alignItems: "center",
        }}
      >
        <Launch style={{ fontSize: "12px", color: "#808080" }} />
      </a>
    </Tooltip>
  )
}

export default NodesLinkButton
