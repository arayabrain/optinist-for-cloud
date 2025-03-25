import { CSSProperties, FC } from "react"

import { Launch } from "@mui/icons-material"
import { Tooltip } from "@mui/material"

interface NodesLinkButtonProps {
  algoName: string
  placement?: string
  linkStyle?: CSSProperties
  iconStyle?: CSSProperties
}

const NodesLinkButton: FC<NodesLinkButtonProps> = ({
  algoName,
  placement,
  linkStyle,
  iconStyle,
}) => {
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
    <Tooltip title="Check Documentation" placement="top" arrow>
      <a
        href={parameterUrl}
        target="_blank"
        rel="noopener noreferrer"
        style={{
          ...linkStyle,
        }}
      >
        <Launch style={{ ...iconStyle }} />
      </a>
    </Tooltip>
  )
}

export default NodesLinkButton
