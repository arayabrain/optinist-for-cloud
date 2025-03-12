import {
  KeyboardEvent as KeyboardEventReact,
  MouseEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react"

import qs from "qs"
import { v4 as uuidv4 } from "uuid"

import { keyframes } from "@emotion/react"
import styled from "@emotion/styled"
import AdbIcon from "@mui/icons-material/Adb"
import ArrowBackIosIcon from "@mui/icons-material/ArrowBackIos"
import ArrowForwardIosIcon from "@mui/icons-material/ArrowForwardIos"
import CloseIcon from "@mui/icons-material/Close"
import ErrorIcon from "@mui/icons-material/Error"
import GradeIcon from "@mui/icons-material/Grade"
import InfoIcon from "@mui/icons-material/Info"
import KeyboardDoubleArrowDownIcon from "@mui/icons-material/KeyboardDoubleArrowDown"
import MenuIcon from "@mui/icons-material/Menu"
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
  limit?: number
  search?: string
}

type TParamQueryLogs = TParams & { reverse?: boolean; offset: number }

type Props = {
  isOpen?: boolean
  onClose?: () => void
}

const SPACE_CHECK_SCROLL = 50

const ModalLogs = ({ isOpen = false, onClose }: Props) => {
  const [keyword, setKeywork] = useState("")
  const [logs, setLogs] = useState<{ text: string; id: string }[]>([])
  const [levels, setLevels] = useState<TLevelsLog[]>()
  const [openSearch, setOpenSearch] = useState(false)
  const [openLevels, setOpenLevels] = useState(false)
  const [visibleScrollEnd, setVisibleScrollEnd] = useState(false)
  const [searchId, setSearchId] = useState("")

  const params = useRef<TParams>({ limit: 50 })
  const timeoutApi = useRef<NodeJS.Timeout>()
  const offset = useRef({ next: -1, pre: -1 })
  const pending = useRef({ next: false, pre: false, reached: false })
  const refLogs = useRef(logs)
  const refSearchId = useRef(searchId)
  const isSearchKeyWhenPre = useRef(false)

  const [isError, setIsError] = useState(false)
  const isUnmount = useRef(false)

  const refScroll = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    return () => {
      isUnmount.current = true
    }
  }, [])

  useEffect(() => {
    refSearchId.current = searchId
  }, [searchId])

  useEffect(() => {
    if (!openLevels && levels?.length) setLevels([])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openLevels])

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
    const _params = { ...params.current, offset: pre, reverse: true }
    const { data, offset: _offset } = await serviceLogs(_params)
    if (_offset.pre < pre) {
      setLogs((pre) => {
        refLogs.current = [...data, ...pre]
        return refLogs.current
      })
    }
    if (isSearchKeyWhenPre.current) {
      const item = getItemSearchPre(params.current.search)
      if (item) setSearchId(item.id)
      isSearchKeyWhenPre.current = false
    }
    offset.current.pre = _offset.pre
  }, [getItemSearchPre, serviceLogs])

  const getInitOffset = useCallback(async () => {
    const { offset: _offset, data } = await serviceLogs({
      offset: -1,
      levels: params.current.levels,
    })
    setLogs((pre) => [...pre, ...data])
    offset.current = _offset
    const key = params.current.search
    params.current.search = ""
    await getPreviousData()
    params.current.search = key
  }, [getPreviousData, serviceLogs])

  const getData = useCallback(async () => {
    const { next, pre } = offset.current
    const { search } = params.current
    if (
      pre === -1 ||
      (search && next === -1) ||
      (pre > 0 && !refLogs.current.length)
    ) {
      await getInitOffset()
    }
    const { data, offset: _offset } = await serviceLogs({
      ...params.current,
      offset: offset.current.next,
      reverse: false,
    })
    if (_offset.next > offset.current.next) setLogs((pre) => [...pre, ...data])
    offset.current.next = _offset.next
  }, [getInitOffset, serviceLogs])

  const getNextData = useCallback(
    async (isForce?: boolean) => {
      clearTimeout(timeoutApi.current)
      try {
        const {
          clientHeight = 0,
          scrollHeight = 0,
          scrollTop = 0,
        } = refScroll.current || {}
        if (
          !isForce &&
          (scrollHeight - scrollTop > clientHeight + SPACE_CHECK_SCROLL ||
            params.current.search)
        )
          return
        await getData()
        setIsError(false)
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
      } catch {
        setIsError(true)
      } finally {
        if (!isUnmount.current) {
          timeoutApi.current = setTimeout(getNextData, TIME_INTERVAL_API)
        }
      }
    },
    [getData],
  )

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
    else getNextData(true)
  }, [getNextData, searchId])

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
    (event: KeyboardEventReact<HTMLInputElement>) => {
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
    getNextData()
    return () => {
      clearTimeout(timeoutApi.current)
    }
  }, [getNextData])

  useEffect(() => {
    setLogs([])
    refLogs.current = []
    setSearchId("")
    refSearchId.current = ""
    offset.current = { next: -1, pre: -1 }
    params.current.levels = levels
  }, [levels])

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

  const onWindowKeydown = useCallback((event: KeyboardEvent) => {
    if (event.code === "Escape") {
      event.preventDefault()
      event.stopPropagation()
      setOpenSearch(false)
      setOpenLevels(false)
    }
  }, [])

  useEffect(() => {
    if (!openSearch && !openLevels) return
    document.addEventListener("keydown", onWindowKeydown)
    return () => {
      document.removeEventListener("keydown", onWindowKeydown)
    }
  }, [openLevels, openSearch, onWindowKeydown])

  const onScroll = useCallback((event: MouseEvent<HTMLDivElement>) => {
    const {
      clientHeight = 0,
      scrollHeight = 0,
      scrollTop = 0,
    } = event.target as HTMLDivElement
    setVisibleScrollEnd(
      scrollHeight - scrollTop > clientHeight + SPACE_CHECK_SCROLL,
    )
  }, [])

  const onScrollEnd = useCallback(() => {
    if (!refScroll.current) return
    const { clientHeight = 0, scrollHeight = 0 } = refScroll.current
    refScroll.current.scrollTo({
      top: scrollHeight - clientHeight,
      behavior: "smooth",
    })
  }, [])

  return (
    <Modal
      disableEscapeKeyDown={openSearch || openLevels}
      style={{ display: "flex" }}
      open={isOpen}
      onClose={onClose}
    >
      <Content>
        <ScrollLogs
          ref={refScroll}
          searchId={searchId}
          logs={logs}
          keyword={keyword}
          onStartReached={onStartReached}
          isError={isError}
          onScroll={onScroll}
        />
        <ButtonCloseModal onClick={onClose}>
          <CloseIcon />
        </ButtonCloseModal>
        {openSearch ? (
          <BoxSearch isOpen>
            <Box display="flex">
              <BoxIconSearch marginLeft="8px" onClick={onPrevSearch}>
                <ArrowBackIosIcon />
              </BoxIconSearch>
              <BoxIconSearch onClick={onNextSearch}>
                <ArrowForwardIosIcon />
              </BoxIconSearch>
            </Box>
            <InputSearch
              autoFocus
              value={keyword}
              placeholder="Search logs"
              onChange={(e) => setKeywork(e.target.value)}
              onKeyDown={onKeyDown}
            />
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

        {openLevels ? (
          <BoxFilter>
            <Box position="relative">
              <MenuFilter
                active={!levels?.length}
                onClick={() => setLevels(undefined)}
              >
                <InfoIcon />
                <span>{TLevelsLog.ALL}</span>
              </MenuFilter>
              <BoxCloseLevel onClick={() => setOpenLevels(false)}>
                <CloseIcon />
              </BoxCloseLevel>
            </Box>
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
        ) : (
          <BoxMenu onClick={() => setOpenLevels(true)}>
            <MenuIcon />
          </BoxMenu>
        )}
        {visibleScrollEnd ? (
          <BoxScrollDown onClick={onScrollEnd}>
            <KeyboardDoubleArrowDownIcon />
          </BoxScrollDown>
        ) : null}
      </Content>
    </Modal>
  )
}

const BoxCloseLevel = styled(Box)`
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  right: 8px;
  cursor: pointer;

  svg {
    font-size: 20px;
  }
`

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
  color: ${({ active }) => (active ? "rgb(0, 0, 0)" : "rgba(0, 0, 0, 0.3)")};
  svg {
    width: 30px;
  }
  border-bottom: 2px solid;
  border-color: ${({ active }) => (active ? "rgb(0, 0, 0)" : "transparent")};
`

const BoxFilter = styled(Box)`
  position: absolute;
  right: 5px;
  top: 85px;
  background: white;
  min-width: 150px;
`

const BoxSearch = styled(Box, {
  shouldForwardProp: (props) => props !== "isOpen",
})<{ isOpen?: boolean }>`
  position: absolute;
  right: 5px;
  top: 39px;
  height: 37px;
  width: 320px;
  display: flex;
  align-items: center;
  border: ${({ isOpen }) =>
    !isOpen ? "none" : "1px solid rgba(255, 255, 255, 0.5)"};
  border-radius: ${({ isOpen }) => (isOpen ? "4px" : "40px")};
  background-color: ${({ isOpen }) => (isOpen ? "black" : "transparent")};
  svg {
    font-size: ${({ isOpen }) => (isOpen ? "14px" : "18px")};
  }
`

const InputSearch = styled("input")`
  height: 23px;
  background-color: transparent;
  outline: none;
  border: none;
  color: white;
  padding: 8px 10px;
  flex: 1;
`

const BoxIconSearch = styled(Box)`
  color: white;
  height: 40px;
  width: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  svg {
    font-size: 14px;
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
  outline: none;
`

const ButtonClose = styled(Box)`
  width: 37px;
  height: 39px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  color: white;
  svg {
    font-size: 16px;
  }
`

const ButtonCloseModal = styled(ButtonClose)`
  position: absolute;
  top: 0;
  right: 5px;
  height: 39px;
  svg {
    font-size: 20px;
  }
`

const BoxMenu = styled(ButtonClose)`
  position: absolute;
  top: 85px;
  right: 5px;
  height: 39px;
  svg {
    font-size: 20px;
  }
`

const rotate = keyframes`
  0% {
    transform: translateY(-5px);
  }
  50% {
    transform: translateY(0);
  }
  100% {
    transform: translateY(-5px);
  }
`

const BoxScrollDown = styled(Box)`
  position: absolute;
  bottom: 10px;
  right: 5px;
  color: white;
  width: 30px;
  height: 30px;
  animation: ${rotate} 2s linear infinite;
  cursor: pointer;
`

export default ModalLogs
