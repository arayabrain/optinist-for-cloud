export const EXPERIMENTS_SLICE_NAME = "experiments"

export type Experiments =
  | {
      status: "fulfilled"
      experimentList: ExperimentListType
      loading?: boolean
    }
  | {
      status: "uninitialized"
      loading?: boolean
    }
  | {
      status: "pending"
      loading?: boolean
    }
  | {
      status: "error"
      message?: string
      loading?: boolean
    }

export type ExperimentListType = {
  [uid: string]: ExperimentType
}

export type ExperimentType = {
  uid: string
  functions: {
    [nodeId: string]: ExperimentFunction
  }
  status?: EXPERIMENTS_STATUS
  name: string
  startedAt: string
  finishedAt?: string
  hasNWB: boolean
  frameRate?: number
  data_usage: number
  isRemoteSynced?: boolean
}

export type ExperimentFunction = {
  name: string
  nodeId: string
  status: EXPERIMENTS_STATUS
  hasNWB: boolean
  message?: string
}

export type EXPERIMENTS_STATUS = "success" | "error" | "running"

export interface ExperimentSortKeys {
  uid: string
  name: string
  startedAt: string
  data_usage: string
}
