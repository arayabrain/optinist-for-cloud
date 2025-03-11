import { KeyboardEvent, useCallback, useEffect, useRef, useState } from "react"

import qs from "qs"
import { v4 as uuidv4 } from "uuid"

import styled from "@emotion/styled"
import AdbIcon from "@mui/icons-material/Adb"
import ArrowBackIosIcon from "@mui/icons-material/ArrowBackIos"
import ArrowForwardIosIcon from "@mui/icons-material/ArrowForwardIos"
import CloseIcon from "@mui/icons-material/Close"
import ErrorIcon from "@mui/icons-material/Error"
import GradeIcon from "@mui/icons-material/Grade"
import InfoIcon from "@mui/icons-material/Info"
import SearchIcon from "@mui/icons-material/Search"
import WarningIcon from "@mui/icons-material/Warning"
import { Box, Modal } from "@mui/material"

import ScrollLogs from "components/Workspace/FlowChart/ScrollLogs"
import axios from "utils/axios"

const TIME_INTERVAL_API = 2000

enum TLevelsLog {
  ALL = "ALL",
  INFO = "INFO",
  ERROR = "ERROR",
  DEBUG = "DEBUG",
  WARNING = "WARNING",
  CRITICAL = "CRITICAL",
}

type TParams = {
  levels?: TLevelsLog[]
  limit: number
  search?: string
}

type TParamQueryLogs = TParams & { reverse?: boolean; offset: number }

type Props = {
  isOpen?: boolean
  onClose?: () => void
}

const ModalLogs = ({ isOpen = false, onClose }: Props) => {
  const [keyword, setKeywork] = useState("")
  const [logs, setLogs] = useState<{ text: string; id: string }[]>([])
  const [levels, setLevels] = useState<TLevelsLog[]>()
  const [openSearch, setOpenSearch] = useState(true)
  const [searchId, setSearchId] = useState("")

  const params = useRef<TParams>({ limit: 50 })
  const timeoutApi = useRef<NodeJS.Timeout>()
  const offset = useRef({ next: -1, pre: -1 })
  const pending = useRef({ next: false, pre: false, reached: false })
  const refLogs = useRef(logs)
  const refSearchId = useRef(searchId)
  const isSearchKeyWhenPre = useRef(false)

  const [isError, setIsError] = useState(false)

  const refScroll = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    refSearchId.current = searchId
  }, [searchId])

  const getItemSearchPre = useCallback((search?: string) => {
    if (!search) return
    const index = refLogs.current
      .toReversed()
      .findIndex((e) => e.id === refSearchId.current)
    return refLogs.current.toReversed().find((e, i) => {
      return e.text.toLowerCase().includes(search.toLowerCase()) && i > index
    })
  }, [])

  const serviceLogs = useCallback(async (params: TParamQueryLogs) => {
    const { data, config } = await axios.get("/logs", {
      params,
      paramsSerializer: (params) => {
        // eslint-disable-next-line import/no-named-as-default-member
        return qs.stringify(params, { arrayFormat: "repeat" })
      },
    })
    return {
      data: data.data.map((e: string) => ({ text: e, id: uuidv4() })),
      params: config.params,
      offset: { next: data.next_offset, pre: data.prev_offset },
    }
  }, [])

  const getPreviousData = useCallback(async () => {
    const { pre } = offset.current
    if (pre <= 0) return
    const _params = { ...params.current, offset: pre, reverse: true }
    const { data, offset: _offset } = await serviceLogs(_params)
    if (_offset.pre < pre) {
      setLogs((pre) => {
        refLogs.current = [...data, ...pre]
        if (isSearchKeyWhenPre.current) {
          const item = getItemSearchPre(params.current.search)
          if (item) setSearchId(item.id)
          isSearchKeyWhenPre.current = false
        }
        return refLogs.current
      })
    }
    offset.current.pre = _offset.pre
  }, [getItemSearchPre, serviceLogs])

  const getNextData = useCallback(async () => {
    clearTimeout(timeoutApi.current)
    try {
      const { next } = offset.current
      const _params = { ...params.current, offset: next, reverse: false }
      const { data, offset: _offset } = await serviceLogs(_params)
      if (_offset.next > next) setLogs((pre) => [...pre, ...data])
      offset.current.next = _offset.next
      if (_offset.pre !== -1 && offset.current.pre === -1) {
        offset.current.pre = _offset.pre
        await getPreviousData()
      }
      setIsError(false)
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    } catch {
      setIsError(true)
    } finally {
      timeoutApi.current = setTimeout(getNextData, TIME_INTERVAL_API)
    }
  }, [getPreviousData, serviceLogs])

  useEffect(() => {
    getNextData()
    return () => {
      clearTimeout(timeoutApi.current)
    }
  }, [getNextData])

  useEffect(() => {
    setKeywork("")
    params.current.search = undefined
  }, [openSearch])

  const onChangeTypeFilter = useCallback(
    (t: TLevelsLog) => {
      if (levels?.includes(t)) {
        params.current.levels = levels.filter((e) => e !== t)
      } else params.current.levels = [...(levels || []), t]
      setLevels(params.current.levels)
    },
    [levels],
  )

  const onPrevSearch = useCallback(() => {
    const item = getItemSearchPre(params.current.search)
    if (item) setSearchId(item.id)
    else {
      isSearchKeyWhenPre.current = true
      getPreviousData()
    }
  }, [getItemSearchPre, getPreviousData])

  const onNextSearch = useCallback(() => {
    const { search } = params.current
    if (!search) return
    const index = refLogs.current.findIndex((e) => e.id === searchId)
    const item = refLogs.current.find((e, i) => {
      return e.text.toLowerCase().includes(search.toLowerCase()) && i > index
    })
    if (item) setSearchId(item.id)
  }, [searchId])

  const onStartReached = useCallback(async () => {
    if (pending.current.reached) return
    pending.current.reached = true
    try {
      await getPreviousData()
    } finally {
      pending.current.reached = false
    }
  }, [getPreviousData])

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLInputElement>) => {
      if (event.code === "Escape") {
        event.preventDefault()
        event.stopPropagation()
        setOpenSearch(false)
      } else if (event.code === "Enter") {
        onNextSearch()
      }
    },
    [onNextSearch],
  )

  const getTop = useCallback((elementId: string) => {
    return (
      refScroll.current
        ?.querySelector(`#scroll_item_${elementId}`)
        ?.getBoundingClientRect()?.top || -1
    )
  }, [])

  const getSearchId = useCallback(() => {
    const { search } = params.current
    if (!search) return
    const list = refLogs.current
      .filter((e) => e.text.toLowerCase().includes(search.toLowerCase()))
      .map((e) => ({ ...e, top: getTop(e.id) }))
    let item = list.filter((e) => e.top > 0)[0]
    if (!item) item = list.toSorted((a, b) => (a.top > b.top ? -1 : 1))[0]
    if (item) {
      setSearchId(item?.id ?? "")
    } else {
      isSearchKeyWhenPre.current = true
      getPreviousData()
    }
  }, [getPreviousData, getTop])

  useEffect(() => {
    params.current.search = keyword.trim()
    if (keyword) getSearchId()
    else setSearchId("")
  }, [getSearchId, keyword])

  useEffect(() => {
    setLogs([])
    refLogs.current = []
    refSearchId.current = ""
    setSearchId("")
    offset.current = { next: -1, pre: -1 }
    const search = params.current.search
    clearTimeout(timeoutApi.current)
    params.current.search = ""
    getNextData().then(() => {
      params.current.search = search
    })
    return () => {
      clearTimeout(timeoutApi.current)
    }
  }, [getNextData, levels])

  useEffect(() => {
    refLogs.current = logs
    const { search } = params.current
    if (!refSearchId.current && search) {
      const item = refLogs.current.find((e) =>
        e.text.toLowerCase().includes(search.toLowerCase()),
      )
      if (item) setSearchId(item.id)
      else onPrevSearch()
    }
  }, [logs, onPrevSearch])

  return (
    <Modal style={{ display: "flex" }} open={isOpen} onClose={onClose}>
      <Content>
        <ScrollLogs
          ref={refScroll}
          searchId={searchId}
          logs={logs}
          keyword={keyword}
          onStartReached={onStartReached}
          isError={isError}
        />
        {openSearch ? (
          <BoxSearch>
            <InputSearch
              autoFocus
              value={keyword}
              placeholder="Search logs"
              onChange={(e) => setKeywork(e.target.value)}
              onKeyDown={onKeyDown}
            />
            <Box display="flex">
              <BoxIconSearch onClick={onPrevSearch}>
                <ArrowBackIosIcon />
              </BoxIconSearch>
              <BoxIconSearch onClick={onNextSearch}>
                <ArrowForwardIosIcon />
              </BoxIconSearch>
            </Box>
            <ButtonClose onClick={() => setOpenSearch(false)}>
              <CloseIcon />
            </ButtonClose>
          </BoxSearch>
        ) : (
          <BoxSearch width={"auto !important"}>
            <ButtonClose onClick={() => setOpenSearch(true)}>
              <SearchIcon />
            </ButtonClose>
          </BoxSearch>
        )}
        <BoxFilter>
          <MenuFilter
            active={!levels?.length}
            onClick={() => setLevels(undefined)}
          >
            <InfoIcon />
            <span>{TLevelsLog.ALL}</span>
          </MenuFilter>
          <MenuFilter
            active={levels?.includes(TLevelsLog.INFO)}
            onClick={() => onChangeTypeFilter(TLevelsLog.INFO)}
          >
            <InfoIcon />
            <span>{TLevelsLog.INFO}</span>
          </MenuFilter>
          <MenuFilter
            active={levels?.includes(TLevelsLog.WARNING)}
            onClick={() => onChangeTypeFilter(TLevelsLog.WARNING)}
          >
            <WarningIcon />
            <span>{TLevelsLog.WARNING}</span>
          </MenuFilter>
          <MenuFilter
            active={levels?.includes(TLevelsLog.DEBUG)}
            onClick={() => onChangeTypeFilter(TLevelsLog.DEBUG)}
          >
            <AdbIcon />
            <span>{TLevelsLog.DEBUG}</span>
          </MenuFilter>
          <MenuFilter
            active={levels?.includes(TLevelsLog.ERROR)}
            onClick={() => onChangeTypeFilter(TLevelsLog.ERROR)}
          >
            <ErrorIcon />
            <span>{TLevelsLog.ERROR}</span>
          </MenuFilter>
          <MenuFilter
            active={levels?.includes(TLevelsLog.CRITICAL)}
            onClick={() => onChangeTypeFilter(TLevelsLog.CRITICAL)}
          >
            <GradeIcon />
            <span>{TLevelsLog.CRITICAL}</span>
          </MenuFilter>
        </BoxFilter>
        <ButtonCloseModal onClick={onClose}>
          <CloseIcon />
        </ButtonCloseModal>
      </Content>
    </Modal>
  )
}

const MenuFilter = styled(Box, {
  shouldForwardProp: (props) => props !== "active",
})<{ active?: boolean }>`
  display: flex;
  padding: 5px 16px;
  align-items: center;
  color: white;
  cursor: pointer;
  gap: 4px;
  padding-left: 5px;
  background-color: ${({ active }) => (active ? "blue" : "")};
  svg {
    width: 30px;
  }
`

const BoxFilter = styled(Box)`
  position: absolute;
  background-color: #2d404e;
  right: 0;
  bottom: 0;
`

const BoxSearch = styled(Box)`
  position: absolute;
  right: 0;
  top: 0;
  background-color: #2d404e;
  height: 43px;
  width: 320px;
  display: flex;
  align-items: center;
  gap: 4px;
`

const InputSearch = styled("input")`
  width: 200px;
  height: 23;
  background-color: transparent;
  outline: none;
  border: none;
  color: white;
  padding: 16px 10px;
`

const BoxIconSearch = styled(Box)`
  color: #7fa2bc;
  height: 43px;
  width: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  svg {
    font-size: 18px;
  }
`

const Content = styled(Box)`
  width: 1600px;
  max-width: 85%;
  height: 700px;
  max-height: 80%;
  background-color: black;
  position: relative;
  white-space: pre-wrap;
  margin: auto;
`

const ButtonClose = styled(Box)`
  width: 40px;
  height: 43px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: #7fa2bc;
`

const ButtonCloseModal = styled(ButtonClose)`
  background-color: white;
  position: absolute;
  top: 0;
  height: 40px;
`

export default ModalLogs
