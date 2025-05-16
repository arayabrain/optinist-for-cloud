// utils/docsLink.ts
const algoNameMapping: Record<string, string> = {
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

export const getDocumentationUrl = (algoName: string): string => {
  const formatted = algoName.toLowerCase().replace(/_/g, "-")
  const mappedName = algoNameMapping[formatted] || formatted
  return `https://optinist.readthedocs.io/en/latest/specifications/algorithm_nodes.html#${mappedName}`
}
