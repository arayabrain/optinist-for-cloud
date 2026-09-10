import {
  ChangeEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react"
import { useDispatch, useSelector } from "react-redux"
import { useSearchParams } from "react-router-dom"

import moment from "moment"
import { enqueueSnackbar, VariantType } from "notistack"

import ImageIcon from "@mui/icons-material/Image"
import InsightsIcon from "@mui/icons-material/Insights"
import PublicIcon from "@mui/icons-material/Public"
import PublicOffIcon from "@mui/icons-material/PublicOff"
import {
  Alert,
  Box,
  Checkbox,
  Chip,
  IconButton,
  Input,
  styled,
  Tooltip,
} from "@mui/material"
import Button from "@mui/material/Button"
import Dialog from "@mui/material/Dialog"
import DialogActions from "@mui/material/DialogActions"
import DialogContent from "@mui/material/DialogContent"
import DialogContentText from "@mui/material/DialogContentText"
import {
  DataGrid,
  GridColDef,
  GridEventListener,
  GridFilterInputValueProps,
  GridFilterItem,
  GridFilterModel,
  GridSortDirection,
  GridSortItem,
  GridSortModel,
  getGridSingleSelectOperators,
} from "@mui/x-data-grid"

import { UserDTO } from "api/users/UsersApiDTO"
import { ConfirmDialog } from "components/common/ConfirmDialog"
import Loading from "components/common/Loading"
import PaginationCustom from "components/common/PaginationCustom"
import SwitchCustom from "components/common/SwitchCustom"
import InputsView from "components/Dataview/InputsView"
import OutputsView from "components/Dataview/OutputsView"
import { ThumbnailImage } from "components/Dataview/ThumbnailImage"
import { WorkflowDetailsView } from "components/Dataview/WorkflowDetailsView"
import { ImagePlotSimpleWithLoading } from "components/Workspace/Visualize/Plot/ImagePlotSimple"
import { RoiPlotSimpleWithLoading } from "components/Workspace/Visualize/Plot/RoiPlotSimple"
import { DELAY_TIME_INPUT_CONFIRMED } from "const/Form"
import {
  getDataviewRecords,
  getPublicDataviewRecords,
  postPublish,
  postPublishAll,
  putAttributes,
} from "store/slice/Dataview/DataviewActions"
import {
  selectDataviewPrivateData,
  selectDataviewPublicData,
  selectDataviewLoading,
  selectDataviewPrivateError,
  selectDataviewPublicError,
} from "store/slice/Dataview/DataviewSelectors"
import { DataviewType } from "store/slice/Dataview/DataviewType"
import { AppDispatch } from "store/store"

type PopupAttributesProps = {
  data?: string | string[]
  open: boolean
  handleClose: () => void
  role?: boolean
  handleChangeAttributes: (e: ChangeEvent<HTMLTextAreaElement>) => void
  uid?: string
  onSubmit: () => void
  readonly?: boolean
}

type DataviewProps = {
  user?: UserDTO
  handleRowClick?: GridEventListener<"rowClick">
  readonly?: boolean
  metadataEditable?: boolean
  workspaceId?: string
}

const useDebounce = () => {
  const timeoutRef = useRef<NodeJS.Timeout | undefined>()

  return useCallback((callback: () => void, delay: number) => {
    if (timeoutRef.current) clearTimeout(timeoutRef.current)
    timeoutRef.current = setTimeout(callback, delay)
  }, [])
}

const getPublishStatusValue = (publishStatus?: string) => {
  if (!publishStatus) return undefined
  // Handle both numeric strings and "Published"/"No_Published" strings
  if (publishStatus === "1" || publishStatus === "Published") return 1
  if (publishStatus === "0" || publishStatus === "No_Published") return 0
  return Number(publishStatus) // Fallback to numeric conversion
}

const buildFilterParams = (
  dataParamsFilter: Record<string, string | string[] | undefined>,
  excludeWorkspaceId: boolean = false,
) => {
  return Object.keys(dataParamsFilter)
    .filter((key) => {
      if (excludeWorkspaceId && key === "workspace_id") return false
      const value = dataParamsFilter[key]
      return Array.isArray(value) ? value.length > 0 : Boolean(value)
    })
    .map((key) => {
      const value = dataParamsFilter[key]
      if (Array.isArray(value)) {
        return value.map((item) => `${key}=${item}`).join("&")
      }
      return `${key}=${value}`
    })
    .join("&")
}

const FilterInput = ({
  applyValue,
  item,
  loading,
}: GridFilterInputValueProps & { loading: boolean }) => {
  const debounce = useDebounce()

  return (
    <Input
      autoFocus={!loading}
      sx={{ paddingTop: "16px" }}
      defaultValue={item.value || ""}
      onChange={(e) => {
        debounce(() => {
          applyValue({ ...item, value: e.target.value })
        }, DELAY_TIME_INPUT_CONFIRMED)
      }}
    />
  )
}

const defineColumns = (
  listIdData: number[],
  setListCheck: (value: number[]) => void,
  listCheck: number[],
  dataviewRecords: DataviewType[],
  checkBoxAll: boolean,
  setCheckBoxAll: (value: boolean) => void,
  handleOpenAttributes: (value: string, id: number) => void,
  handleOpenInputsView: (workspaceId: number, uid: string) => void,
  handleOpenOutputsView: (workspaceId: number, uid: string) => void,
  handleOpenDetailsView: (dataviewRecord: DataviewType) => void,
  is_public: boolean,
  readonly?: boolean,
  loading: boolean = false,
  workspaceId?: string,
) => [
  !is_public &&
    !readonly && {
      field: "checkbox",
      renderHeader: () => (
        <Checkbox
          checked={checkBoxAll}
          onChange={(e: ChangeEvent) => {
            const target = e.target as HTMLInputElement
            setCheckBoxAll(target.checked)
            if (!target.checked) {
              const newListId: number[] = listCheck.filter(
                (item) => !listIdData.includes(item),
              )
              setListCheck([...newListId])
            } else {
              const newList = dataviewRecords.map((item) => item.id)
              setListCheck([
                ...listCheck,
                ...newList.filter((item) => !listCheck.includes(item)),
              ])
            }
          }}
        />
      ),
      sortable: false,
      filterable: false,
      width: 70,
      type: "string",
      renderCell: (params: { row: DataviewType }) => (
        <Checkbox
          checked={listCheck.includes(params.row.id)}
          onChange={(e: ChangeEvent) => {
            const newData = listCheck.filter((id) => id !== params.row.id)
            const target = e.target as HTMLInputElement
            if (!target.checked) {
              setCheckBoxAll(false)
              setListCheck(newData)
            } else setListCheck([...listCheck, params.row.id])
          }}
        />
      ),
    },
  {
    field: "uid",
    headerName: "ID",
    width: 100,
    filterOperators: [
      {
        label: "Contains",
        value: "contains",
        InputComponent: (props: GridFilterInputValueProps) => (
          <FilterInput {...props} loading={loading} />
        ),
      },
    ],
    type: "string",
    renderCell: (params: { row: DataviewType }) => (
      <Tooltip title={params.row?.uid}>
        <SpanCustom>{params.row?.uid}</SpanCustom>
      </Tooltip>
    ),
  },
  {
    field: "name",
    headerName: "Name",
    width: 200,
    filterOperators: [
      {
        label: "Contains",
        value: "contains",
        InputComponent: (props: GridFilterInputValueProps) => (
          <FilterInput {...props} loading={loading} />
        ),
      },
    ],
    type: "string",
    renderCell: (params: { row: DataviewType }) => (
      <Tooltip title={params.row?.name}>
        <SpanCustom>{params.row?.name}</SpanCustom>
      </Tooltip>
    ),
  },
  is_public && {
    field: "user_name",
    headerName: "Owner",
    width: 160,
    filterOperators: [
      {
        label: "Contains",
        value: "contains",
        InputComponent: (props: GridFilterInputValueProps) => (
          <FilterInput {...props} loading={loading} />
        ),
      },
    ],
    type: "string",
    renderCell: (params: { row: DataviewType }) => (
      <Tooltip title={params.row?.owner?.name}>
        <SpanCustom>{params.row?.owner?.name}</SpanCustom>
      </Tooltip>
    ),
  },
  {
    field: "workspace_id",
    headerName: "Ws ID",
    width: 110,
    sortable: !workspaceId,
    filterable: !workspaceId,
    filterOperators: [
      {
        label: "Equals",
        value: "equals",
        InputComponent: (props: GridFilterInputValueProps) => (
          <FilterInput {...props} loading={loading} />
        ),
      },
    ],
    type: "string",
    renderCell: (params: { row: DataviewType }) => (
      <Tooltip title={params.row?.workspace?.id}>
        <SpanCustom>{params.row?.workspace?.id}</SpanCustom>
      </Tooltip>
    ),
  },
  {
    field: "workspace_name",
    headerName: "Workspace",
    width: 160,
    sortable: !workspaceId,
    filterable: !workspaceId,
    filterOperators: [
      {
        label: "Contains",
        value: "contains",
        InputComponent: (props: GridFilterInputValueProps) => (
          <FilterInput {...props} loading={loading} />
        ),
      },
    ],
    type: "string",
    renderCell: (params: { row: DataviewType }) => (
      <Tooltip title={params.row?.workspace?.name}>
        <SpanCustom>{params.row?.workspace?.name}</SpanCustom>
      </Tooltip>
    ),
  },
  /** Currently, attribute is hidden
  {
    field: "attributes",
    headerName: "Attributes",
    width: 120,
    filterable: false,
    sortable: false,
    renderCell: (params: { row: DataviewType }) => {
      const inputValue = JSON.stringify(params?.row?.attributes).trim()
      const parsedJSON = JSON.parse(inputValue)
      const formattedJSON = JSON.stringify(parsedJSON, null, 2)
      const value = formattedJSON
      return (
        <Box
          sx={{ cursor: "pointer" }}
          onClick={() => handleOpenAttributes(value, params?.row?.id)}
        >
          <AssignmentOutlinedIcon color={"primary"} />
        </Box>
      )
    },
  },
  */
  {
    field: "input_data",
    headerName: "Inputs",
    width: 160,
    filterable: false,
    sortable: false,
    renderCell: (params: { row: DataviewType }) => {
      const workspaceId = params?.row?.workspace.id
      const uniqueId = params?.row?.uid
      const thumbnailPath = params?.row?.thumbnails?.image_url
      // Check if it's a PNG thumbnail (new format) or TIFF (legacy)
      const isPngThumb = thumbnailPath && thumbnailPath.includes("_thumb.png")
      const filePath = thumbnailPath ? thumbnailPath.replace(/^\//, "") : null

      return (
        <Box
          sx={{
            width: "100%",
            height: "100%",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            overflow: "hidden",
            gap: 1,
          }}
        >
          <Box sx={{ width: 100, height: 80 }}>
            {filePath ? (
              isPngThumb ? (
                // PNG thumbnail - uses authenticated API to fetch
                <ThumbnailImage
                  workspaceId={workspaceId}
                  uniqueId={uniqueId}
                  thumbType="input"
                  onClick={() => handleOpenInputsView(workspaceId, uniqueId)}
                  alt="Input thumbnail"
                />
              ) : (
                // Legacy TIFF - may need on-demand sync, show with loading indicator
                <ImagePlotSimpleWithLoading
                  filePath={filePath}
                  workspaceId={workspaceId}
                  uniqueId={uniqueId}
                  onClick={() => handleOpenInputsView(workspaceId, uniqueId)}
                />
              )
            ) : (
              <Box
                sx={{
                  width: "100%",
                  height: "100%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  cursor: "pointer",
                }}
                onClick={() => handleOpenInputsView(workspaceId, uniqueId)}
              >
                <ImageIcon color={"primary"} fontSize="large" />
              </Box>
            )}
          </Box>
        </Box>
      )
    },
  },
  {
    field: "output_data",
    headerName: "Outputs",
    width: 160,
    filterable: false,
    sortable: false,
    renderCell: (params: { row: DataviewType }) => {
      const workspaceId = params?.row?.workspace.id
      const uniqueId = params?.row?.uid
      const thumbnailPath = params?.row?.thumbnails?.roi_url
      // Check if it's a PNG thumbnail (new format) or JSON (legacy)
      const isPngThumb = thumbnailPath && thumbnailPath.includes("_thumb.png")
      const filePath = thumbnailPath ? thumbnailPath.replace(/^\//, "") : null

      return (
        <Box
          sx={{
            width: "100%",
            height: "100%",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            overflow: "hidden",
            gap: 1,
          }}
        >
          <Box sx={{ width: 100, height: 80 }}>
            {filePath ? (
              isPngThumb ? (
                // PNG thumbnail - uses authenticated API to fetch
                <ThumbnailImage
                  workspaceId={workspaceId}
                  uniqueId={uniqueId}
                  thumbType="roi"
                  onClick={() => handleOpenOutputsView(workspaceId, uniqueId)}
                  alt="ROI thumbnail"
                />
              ) : (
                // Legacy JSON - may need on-demand sync, show with loading indicator
                <RoiPlotSimpleWithLoading
                  filePath={filePath}
                  workspaceId={workspaceId}
                  uniqueId={uniqueId}
                  onClick={() => handleOpenOutputsView(workspaceId, uniqueId)}
                />
              )
            ) : (
              <Box
                sx={{
                  width: "100%",
                  height: "100%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  cursor: "pointer",
                }}
                onClick={() => handleOpenOutputsView(workspaceId, uniqueId)}
              >
                <ImageIcon color={"primary"} fontSize="large" />
              </Box>
            )}
          </Box>
        </Box>
      )
    },
  },
  {
    field: "details",
    headerName: "Details",
    width: 160,
    filterable: false,
    sortable: false,
    renderCell: (params: { row: DataviewType }) => (
      <Box
        sx={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          cursor: "pointer",
        }}
        onClick={() => handleOpenDetailsView(params?.row)}
      >
        <InsightsIcon color="primary" fontSize="large" />
      </Box>
    ),
  },
  {
    field: "timestamp",
    headerName: "Timestamp",
    width: 160,
    type: "string",
    filterable: false,
    sortable: true,
    renderCell: (params: { row: DataviewType }) => (
      <Tooltip title={params.row?.analyzed_at}>
        <SpanCustom>
          {moment(params.row?.analyzed_at).format("YYYY/MM/DD HH:mm")}
        </SpanCustom>
      </Tooltip>
    ),
  },
]

const PopupAttributes = ({
  data,
  open,
  handleClose,
  role = false,
  handleChangeAttributes,
  onSubmit,
  readonly,
}: PopupAttributesProps) => {
  const [error, setError] = useState("")
  const isValidJSON = (str: string) => {
    try {
      JSON.parse(str)
      setError("")
    } catch {
      setError("format JSON invalid")
    }
  }

  const handleChange = (e: ChangeEvent<HTMLTextAreaElement>) => {
    isValidJSON(e.target.value)
    handleChangeAttributes(e)
  }

  useEffect(() => {
    const handleClosePopup = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        handleClose()
        return
      }
    }

    document.addEventListener("keydown", handleClosePopup)
    return () => {
      document.removeEventListener("keydown", handleClosePopup)
    }
    //eslint-disable-next-line
  }, [])

  return (
    <Box>
      <Dialog
        open={open}
        onClose={handleClose}
        aria-labelledby="draggable-dialog-title"
      >
        <DialogContent sx={{ minWidth: 400 }}>
          <DialogContentText>
            <Content
              readOnly={!role || readonly}
              value={data}
              onChange={handleChange}
            />
            <span style={{ color: "red", display: "block" }}>{error}</span>
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button
            variant={"outlined"}
            autoFocus
            onClick={() => {
              handleClose()
              setError("")
            }}
          >
            Close
          </Button>
          {role && !readonly && (
            <Button variant={"contained"} disabled={!!error} onClick={onSubmit}>
              Save
            </Button>
          )}
        </DialogActions>
      </Dialog>
    </Box>
  )
}
const DataviewRecords = ({
  user,
  handleRowClick,
  readonly,
  metadataEditable,
  workspaceId,
}: DataviewProps) => {
  const is_public: boolean = user == null
  const dataviewRecords = useSelector(
    is_public ? selectDataviewPublicData : selectDataviewPrivateData,
  )
  const loading = useSelector(selectDataviewLoading)
  const error = useSelector(
    is_public ? selectDataviewPublicError : selectDataviewPrivateError,
  )

  const [openPublishAll, setOpenPublishAll] = useState<{
    title: string
    open: boolean
    type: "on" | "off"
    content: string
  }>({
    content: "",
    title: "",
    open: false,
    type: "on",
  })
  const [newParams, setNewParams] = useState(
    window.location.search.replace("?", ""),
  )

  const [listCheck, setListCheck] = useState<number[]>([])
  const [checkBoxAll, setCheckBoxAll] = useState(false)
  const [dataDialog, setDataDialog] = useState<{
    id?: number
    workspaceId?: number
    uid?: string
    type?: string
    data?: string | string[]
    dataviewRecord?: DataviewType
  }>({
    id: undefined,
    uid: undefined,
    type: "",
    data: undefined,
    dataviewRecord: undefined,
  })
  const [fieldFilter, setFieldFilter] = useState<{
    id?: number | string
    field?: string
    operator?: string
  }>({})
  const [valueFilter, setValueFilter] = useState<string | string[]>("")

  const [searchParams, setParams] = useSearchParams()
  const dispatch = useDispatch<AppDispatch>()

  const offset = searchParams.get("offset") || 0
  const limit = searchParams.get("limit") || 50
  const sort = searchParams.getAll("sort")

  const handleClickVariant = (variant: VariantType, mess: string) => {
    enqueueSnackbar(mess, { variant })
  }

  const pagiFilter = useCallback(
    (page?: number) => {
      return `limit=${limit}&offset=${
        page ? Number(limit) * (page - 1) : offset || dataviewRecords.offset
      }`
    },
    //eslint-disable-next-line
    [limit, offset, JSON.stringify(dataviewRecords), dataviewRecords.offset],
  )

  const dataParams = useMemo(() => {
    return {
      offset: Number(offset) || 0,
      limit: Number(limit) || 50,
      sort: sort.length > 0 ? [sort[0], sort[1]] : [],
    }
    //eslint-disable-next-line
  }, [offset, limit, JSON.stringify(sort)])

  const dataParamsFilter = useMemo(
    () => ({
      uid: searchParams.get("uid") || undefined,
      name: searchParams.get("name") || undefined,
      publish_status: searchParams.get("publish_status") || undefined,
      user_name: searchParams.get("user_name") || undefined,
      workspace_id:
        searchParams.get("workspace_id") || workspaceId || undefined,
      workspace_name: searchParams.get("workspace_name") || undefined,
    }),
    [searchParams, workspaceId],
  )

  const [model, setModel] = useState<{
    filter: GridFilterModel
    sort: GridSortModel
  }>({
    filter: {
      items: [],
    },
    sort: [],
  })

  const fetchApi = () => {
    const api = is_public ? getPublicDataviewRecords : getDataviewRecords
    const newPublish = getPublishStatusValue(dataParamsFilter.publish_status)
    dispatch(
      api({ ...dataParamsFilter, publish_status: newPublish, ...dataParams }),
    )
  }

  useEffect(() => {
    const key = Object.keys(dataParamsFilter).find((key) => {
      // Skip workspace_id if it's coming from the URL path
      if (
        key === "workspace_id" &&
        workspaceId &&
        !searchParams.get("workspace_id")
      ) {
        return false
      }
      const value = dataParamsFilter[key as keyof typeof dataParamsFilter]
      return (
        (!Array.isArray(value) && value) ||
        (Array.isArray(value) && value.length)
      )
    }) as keyof typeof dataParamsFilter

    if (key) {
      // Find for the initially filtered column
      const filteredColumn = columnsInstance.find(
        (col) => typeof col === "object" && col.field === key,
      )
      const filteredOperators = (filteredColumn &&
        filteredColumn.filterOperators?.[0]) || { value: "" }

      setFieldFilter({ field: key, operator: filteredOperators?.value })
      setValueFilter(dataParamsFilter[key] as string)
    }
    //eslint-disable-next-line
  }, [])

  // This effect should only set initial model based on URL params, not react to changes
  useEffect(() => {
    if (
      Object.keys(dataParamsFilter).every(
        (key) => !dataParamsFilter[key as keyof typeof dataParamsFilter],
      )
    ) {
      return
    }

    // Only set initial model if there's a filter in URL but model is empty
    if (model.filter.items.length === 0 && fieldFilter?.field) {
      setModel({
        filter: {
          items: [
            {
              field: fieldFilter.field || "",
              operator: fieldFilter.operator || "",
              value: valueFilter || null,
            },
          ],
        },
        sort: [
          {
            field: dataParams.sort[0] || "",
            sort: dataParams.sort[1] as GridSortDirection,
          },
        ],
      })
    }
    //eslint-disable-next-line
  }, [])

  useEffect(() => {
    if (dataviewRecords.items.length === 0) {
      setCheckBoxAll(false)
      return
    }
    const newListId = dataviewRecords.items.map((item) => item.id)
    const isCheck = newListId.every((id) => listCheck.includes(id))
    setCheckBoxAll(isCheck)
  }, [dataviewRecords, listCheck])

  useEffect(() => {
    if (newParams && newParams !== window.location.search.replace("?", "")) {
      setNewParams(window.location.search.replace("?", ""))
    }
    //eslint-disable-next-line
  }, [searchParams])

  useEffect(() => {
    let param = newParams
    if (newParams[0] === "&") param = newParams.slice(1, param.length)
    if (param === window.location.search.replace("?", "")) return
    setParams(param.replaceAll("+", "%2B"))
    //eslint-disable-next-line
  }, [newParams])

  useEffect(() => {
    fetchApi()
    //eslint-disable-next-line
  }, [dataParams, is_public, dataParamsFilter])

  useEffect(() => {
    setCheckBoxAll(false)
  }, [offset, limit, dataParamsFilter])

  const handleOpenInputsView = (
    workspaceId: number | undefined,
    uid: string | undefined,
  ) => {
    setDataDialog({
      workspaceId: workspaceId,
      uid: uid,
      type: "inputs_view",
    })
  }

  const handleOpenOutputsView = (
    workspaceId: number | undefined,
    uid: string | undefined,
  ) => {
    setDataDialog({
      workspaceId: workspaceId,
      uid: uid,
      type: "outputs_view",
    })
  }

  const handleOpenDetailsView = (dataviewRecord: DataviewType) => {
    setDataDialog({
      type: "details_view",
      dataviewRecord: dataviewRecord,
    })
  }

  const handleCloseDialog = () => {
    setDataDialog({})
  }

  const handleOpenAttributes = (data: string, id: number) => {
    setDataDialog({ id: id, type: "attribute", data })
  }

  const handleChangeAttributes = (event: ChangeEvent<HTMLTextAreaElement>) => {
    setDataDialog((pre) => ({ ...pre, data: event.target.value }))
  }

  const onSubmitAttributes = async () => {
    const { id, data } = dataDialog
    if (!id || !data) return
    const res = await dispatch(
      putAttributes({
        id: id,
        attributes: data as string,
        params: { ...dataParamsFilter, ...dataParams },
      }),
    )
    if ((res as { payload: boolean }).payload === true) {
      handleClickVariant("success", "Successfully updated attributes!")
      setDataDialog({ ...dataDialog, id: undefined, type: "" })
      return
    }
    handleClickVariant("error", "Update attributes failed!")
  }

  const getParamsData = () => buildFilterParams(dataParamsFilter, !!workspaceId)

  const handlePage = (_e: ChangeEvent<unknown>, page: number) => {
    const filter = getParamsData()
    const param = `${filter}${
      dataParams.sort[0]
        ? `${filter ? "&" : ""}sort=${dataParams.sort[0]}&sort=${
            dataParams.sort[1]
          }`
        : ""
    }&${pagiFilter(page)}`
    setNewParams(param)
  }

  const handlePublish = async (id: number, status: "on" | "off") => {
    const newPublish = getPublishStatusValue(dataParamsFilter.publish_status)
    try {
      await dispatch(
        postPublish({
          id,
          status,
          params: {
            ...dataParamsFilter,
            publish_status: newPublish,
            ...dataParams,
          },
        }),
      ).unwrap()
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail
      const action = status === "on" ? "publish" : "unpublish"
      enqueueSnackbar(detail ?? `Failed to ${action} experiment`, {
        variant: "error",
        style: { whiteSpace: "pre-line" },
      })
    }
  }

  const handleSort = useCallback(
    (rowSelectionModel: GridSortModel) => {
      setModel({
        ...model,
        sort: rowSelectionModel,
      })
      let param
      const filter = getParamsData()
      if (!rowSelectionModel[0]) {
        param =
          filter || dataParams.sort[0] || offset
            ? `${filter ? `${filter}&` : ""}${pagiFilter()}`
            : ""
      } else {
        param = `${filter}${
          rowSelectionModel[0]
            ? `${filter ? "&" : ""}sort=${rowSelectionModel[0].field}&sort=${rowSelectionModel[0].sort}`
            : ""
        }&${pagiFilter()}`
      }
      setNewParams(param)
      setCheckBoxAll(false)
    },
    //eslint-disable-next-line
    [pagiFilter, model],
  )

  const handleFilter = (modelFilter: GridFilterModel) => {
    if (modelFilter.items.length === 0) {
      const data = Object.keys(dataParamsFilter).filter((key) => {
        const value = dataParamsFilter[key as keyof typeof dataParamsFilter]
        if (Array.isArray(value) && value.length === 0) {
          return false
        }
        return !!value
      })
      setModel({
        ...model,
        filter: {
          items: [
            {
              field: data[0],
              operator: "isAnyOf",
              value:
                dataParamsFilter[data[0] as keyof typeof dataParamsFilter] ||
                "",
            },
          ],
        },
      })
      return
    }

    setModel({
      ...model,
      filter: modelFilter,
    })
    setFieldFilter(modelFilter.items[0])
    setValueFilter(modelFilter.items[0]?.value)
    let filter = ""
    // Only check if value exists (including 0)
    if (modelFilter.items.length > 0 && modelFilter.items[0].value != null) {
      filter =
        modelFilter.items
          .filter((item) => item.value != null) // != null checks both null and undefined
          .map((item: GridFilterItem) => {
            if (Array.isArray(item.value)) {
              return item.value
                .map((value) => `${item.field}=${value}`)
                .join("&")
            }
            return `${item.field}=${item.value}`
          })[0] || ""
    }
    const { sort } = dataParams
    // Reset offset to 0 when filter changes
    const param =
      sort[0] || filter
        ? `${filter}${
            sort[0] ? `${filter ? "&" : ""}sort=${sort[0]}&sort=${sort[1]}` : ""
          }&limit=${limit}&offset=0`
        : ""
    setNewParams(param)
    setCheckBoxAll(false)
  }

  const handleLimit = (event: ChangeEvent<HTMLSelectElement>) => {
    const filter = buildFilterParams(dataParamsFilter, !!workspaceId)
    const { sort } = dataParams
    const param = `${filter}${
      sort[0] ? `${filter ? "&" : ""}sort=${sort[0]}&sort=${sort[1]}` : ""
    }&limit=${Number(event.target.value)}&offset=0`
    setNewParams(param)
  }

  const handlePublishCancel = () => {
    setOpenPublishAll({
      ...openPublishAll,
      open: false,
    })
  }

  const handleOpenPublishAll = (
    title: string,
    content: string,
    type: "on" | "off",
  ) => {
    setOpenPublishAll({
      title: title,
      content: content,
      open: true,
      type: type,
    })
  }

  const handlePublishOk = async () => {
    setOpenPublishAll({
      ...openPublishAll,
      open: false,
    })
    try {
      await dispatch(
        postPublishAll({
          status: openPublishAll.type,
          params: {
            ...dataParamsFilter,
            ...dataParams,
          },
          listCheck,
        }),
      ).unwrap()
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })
        ?.response?.data?.detail
      const action = openPublishAll.type === "on" ? "publish" : "unpublish"
      enqueueSnackbar(detail ?? `Failed to ${action} experiments`, {
        variant: "error",
        style: { whiteSpace: "pre-line" },
      })
    }
  }

  const ColumnPrivate = () => {
    return [
      {
        field: "publish_status",
        headerName: "Publish",
        width: 120,
        sortable: false,
        filterable: true,
        type: "singleSelect",
        valueOptions: [
          { value: 1, label: "Published" },
          { value: 0, label: "No_Published" },
        ],
        filterOperators: getGridSingleSelectOperators().filter(
          (operator) => operator.value === "is",
        ),
        renderCell: (params: { row: DataviewType }) => (
          <Box
            sx={{ cursor: "pointer" }}
            onClick={() =>
              handlePublish(
                params.row.id,
                params.row.publish_status ? "off" : "on",
              )
            }
          >
            <SwitchCustom value={!!params.row.publish_status} />
          </Box>
        ),
      },
    ]
  }

  const columnsInstance = defineColumns(
    dataviewRecords.items.map((item) => item.id),
    setListCheck,
    listCheck,
    dataviewRecords?.items,
    checkBoxAll,
    setCheckBoxAll,
    handleOpenAttributes,
    handleOpenInputsView,
    handleOpenOutputsView,
    handleOpenDetailsView,
    is_public,
    readonly,
    loading,
    workspaceId,
  )

  const columnsTable = [...columnsInstance].filter(Boolean) as GridColDef[]

  const workspaceName = useMemo(() => {
    if (!workspaceId) return null
    return dataviewRecords.header?.workspace_name || null
  }, [workspaceId, dataviewRecords.header])

  return (
    <DataviewRecordsWrapper>
      {workspaceId && workspaceName && (
        <Box sx={{ mb: 1 }}>
          <Chip label={`${workspaceName}`} color="primary" variant="outlined" />
        </Box>
      )}
      {!is_public ? (
        <Box sx={{ height: 40, margin: "0 0 0.5rem 0" }}>
          {!readonly ? (
            <WrapperIcons check={!!(listCheck.length > 0)}>
              <Tooltip title={"bulk publish"} placement={"top"}>
                <span>
                  <IconButton
                    onClick={() =>
                      listCheck.length !== 0 &&
                      handleOpenPublishAll(
                        "Bulk Publish",
                        `Confirm publishling "${listCheck.length} records" at once.`,
                        "on",
                      )
                    }
                    sx={{
                      cursor: listCheck.length > 0 ? "pointer" : "default",
                      color: (theme) =>
                        listCheck.length > 0
                          ? theme.palette.primary.main
                          : "#d0d0d0",
                    }}
                    disabled={!!(listCheck.length === 0)}
                  >
                    <PublicIcon />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title={"bulk unpublish"} placement={"top"}>
                <span>
                  <IconButton
                    onClick={() =>
                      listCheck.length !== 0 &&
                      handleOpenPublishAll(
                        "Bulk UnPublish",
                        `Confirm unpublishing "${listCheck.length} records" at once.`,
                        "off",
                      )
                    }
                    sx={{
                      cursor: listCheck.length > 0 ? "pointer" : "default",
                      color: (theme) =>
                        listCheck.length > 0
                          ? theme.palette.primary.main
                          : "#d0d0d0",
                    }}
                    disabled={!!(listCheck.length === 0)}
                  >
                    <PublicOffIcon />
                  </IconButton>
                </span>
              </Tooltip>
            </WrapperIcons>
          ) : null}
        </Box>
      ) : null}
      {error && !loading && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}
      <DataGrid
        columns={
          !is_public && !readonly
            ? ([...columnsTable, ...ColumnPrivate()] as GridColDef[])
            : (columnsTable as GridColDef[])
        }
        sortModel={model.sort as GridSortItem[]}
        rows={dataviewRecords?.items || []}
        rowHeight={128}
        hideFooter={true}
        filterMode={"server"}
        sortingMode={"server"}
        onSortModelChange={handleSort}
        filterModel={model.filter}
        onFilterModelChange={handleFilter}
        onRowClick={handleRowClick}
        initialState={{
          columns: {
            columnVisibilityModel: {
              workspace_id: false, //Ws ID column is hidden by default
            },
          },
        }}
        sx={{ flex: 1, minHeight: 0 }}
      />
      {dataviewRecords?.items.length > 0 ? (
        <Box sx={{ mt: 2 }}>
          <PaginationCustom
            data={dataviewRecords}
            handlePage={handlePage}
            handleLimit={handleLimit}
            limit={Number(limit)}
          />
        </Box>
      ) : null}

      <InputsView
        open={dataDialog.type === "inputs_view"}
        is_public={is_public}
        workspaceId={dataDialog.workspaceId}
        uid={dataDialog.uid}
        handleClose={handleCloseDialog}
      />

      <OutputsView
        open={dataDialog.type === "outputs_view"}
        is_public={is_public}
        workspaceId={dataDialog.workspaceId}
        uid={dataDialog.uid}
        handleClose={handleCloseDialog}
      />

      <WorkflowDetailsView
        dataviewRecord={dataDialog.dataviewRecord || null}
        open={dataDialog.type === "details_view"}
        onClose={handleCloseDialog}
        is_public={is_public}
      />

      <PopupAttributes
        handleChangeAttributes={handleChangeAttributes}
        data={dataDialog.data}
        open={dataDialog.type === "attribute"}
        handleClose={handleCloseDialog}
        onSubmit={onSubmitAttributes}
        role={!is_public}
        readonly={!metadataEditable}
      />
      <Loading loading={loading} />

      <ConfirmDialog
        open={openPublishAll.open}
        title={openPublishAll.title}
        content={openPublishAll.content}
        onCancel={handlePublishCancel}
        onConfirm={handlePublishOk}
      />
    </DataviewRecordsWrapper>
  )
}

// Vertical space reserved for surrounding chrome; the grid fills the rest of
// the viewport and scrolls internally.
export const DATAVIEW_GRID_RESERVED_HEIGHT = 280

const DataviewRecordsWrapper = styled(Box)(() => ({
  width: "100%",
  height: `calc(100vh - ${DATAVIEW_GRID_RESERVED_HEIGHT}px)`,
  display: "flex",
  flexDirection: "column",
}))

const Content = styled("textarea")(() => ({
  width: 400,
  height: 300,
  whiteSpace: "pre-wrap",
}))

const WrapperIcons = styled(Box, {
  shouldForwardProp: (props) => props !== "check",
})<{ check: boolean }>(() => ({
  display: "flex",
  justifyContent: "end",
  gap: 0,
  height: 50,
  svg: {
    width: 30,
    height: 30,
  },
  button: {
    height: 50,
    width: 50,
  },
  "button: hover": {
    backgroundColor: "#1976d257",
  },
}))

export const SpanCustom = styled("span")(() => ({
  display: "inline-block",
  textOverflow: "ellipsis",
  overflow: "hidden",
}))

export default DataviewRecords
