import { ChangeEvent, useCallback, useEffect, useRef, useState } from "react"

import qs from "qs"

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
import { ScrollInverted } from "@react-scroll-inverted/react-scroll"

import axios from "utils/axios"

enum TLevelsLog {
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

type Props = {
  isOpen?: boolean
  onClose?: () => void
}

const ModalLogs = ({ isOpen = false, onClose }: Props) => {
  const [keyword, setKeywork] = useState("")
  const [logs, setLogs] = useState<string[]>([])
  const [levels, setLevels] = useState<TLevelsLog[]>()
  const [openSearch, setOpenSearch] = useState(true)
  const [indexSearch, setIndexSearch] = useState(-1)

  const scrollRef = useRef<ScrollInverted | null>(null)
  const params = useRef<TParams>({ limit: 200 })
  const timeoutApi = useRef<NodeJS.Timeout>()
  const offset = useRef({ next: -1, pre: -1 })
  const allowScrollToEnd = useRef(true)
  const pending = useRef({
    realtime: false,
    pre: false,
    next: false,
    startReached: false,
    endReached: false,
  })

  const getIndexKeyword = useCallback((key: string, _logs: string[]) => {
    if (!key) return
    const _indexSearch = _logs.findIndex((el) =>
      el.toLowerCase().includes(key.toLowerCase()),
    )
    setIndexSearch(_indexSearch)
    if (_indexSearch > -1) scrollRef.current?.scrollToIndex(_indexSearch)
  }, [])

  const serviceLogs = useCallback(async (offset: number, reverse: boolean) => {
    const res = await axios.get("/logs", {
      params: { ...params.current, offset, reverse },
      paramsSerializer: (params) => {
        // eslint-disable-next-line import/no-named-as-default-member
        return qs.stringify(params, { arrayFormat: "repeat" })
      },
    })
    return res
  }, [])

  const getPreviousData = useCallback(async () => {
    if (pending.current.pre) return
    pending.current.pre = true
    try {
      const { data } = await serviceLogs(offset.current.pre, true)
      if (data.prev_offset < offset.current.pre) {
        offset.current.pre = data.prev_offset
        setLogs((pre) => [...data.data, ...pre])
      }
    } finally {
      pending.current.pre = false
    }
  }, [serviceLogs])

  const getNextFindData = useCallback(async () => {
    if (pending.current.next) return
    pending.current.next = true
    try {
      const { data } = await serviceLogs(offset.current.next, false)
      if (data.next_offset > offset.current.next) {
        offset.current.next = data.next_offset
        setLogs((pre) => [...pre, ...data.data])
      }
    } finally {
      pending.current.next = false
    }
  }, [serviceLogs])

  const getRealtimeData = useCallback(async () => {
    if (pending.current.realtime) return
    pending.current.realtime = true
    clearTimeout(timeoutApi.current)
    try {
      const { data } = await serviceLogs(offset.current.next, false)
      if (data.next_offset > offset.current.next) {
        offset.current.next = data.next_offset
        setLogs((pre) => [...pre, ...data.data])
      }
      if (offset.current.pre === -1 && data.prev_offset !== -1) {
        offset.current.pre = data.prev_offset
        await getPreviousData()
      }
    } finally {
      pending.current.realtime = false
    }
    if (!params.current.search) {
      timeoutApi.current = setTimeout(getRealtimeData, 2000)
    }
  }, [getPreviousData, serviceLogs])

  useEffect(() => {
    getRealtimeData()
    return () => {
      clearTimeout(timeoutApi.current)
    }
  }, [getRealtimeData])

  useEffect(() => {
    setKeywork("")
    params.current.search = undefined
  }, [openSearch])

  useEffect(() => {
    params.current.levels = levels
    offset.current = { next: -1, pre: -1 }
    setLogs([])
    getRealtimeData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [levels])

  const onChangeTypeFilter = useCallback(
    (t: TLevelsLog) => {
      if (levels?.includes(t)) setLevels(levels.filter((e) => e !== t))
      else setLevels((pre) => [...(pre || []), t])
    },
    [levels],
  )

  const onPrevSearch = useCallback(() => {
    const _indexSearch = logs.findIndex(
      (el, index) =>
        el.toLowerCase().includes(keyword.toLowerCase()) && index < indexSearch,
    )
    if (_indexSearch < 0) getPreviousData()
    else {
      setIndexSearch(_indexSearch)
      scrollRef.current?.scrollIntoView(_indexSearch)
    }
  }, [getPreviousData, indexSearch, keyword, logs])

  const onNextSearch = useCallback(() => {
    const _indexSearch = logs.findIndex(
      (el, index) =>
        el.toLowerCase().includes(keyword.toLowerCase()) && index > indexSearch,
    )
    if (_indexSearch < 0) getNextFindData()
    else {
      setIndexSearch(_indexSearch)
      scrollRef.current?.scrollIntoView(_indexSearch)
    }
  }, [getNextFindData, indexSearch, keyword, logs])

  const onChangeKeyword = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      const _keyword = e.target.value
      allowScrollToEnd.current = !!_keyword
      setKeywork(_keyword)
      params.current.search = _keyword
      if (_keyword) clearTimeout(timeoutApi.current)
      getIndexKeyword(_keyword, logs)
    },
    [getIndexKeyword, logs],
  )

  const onScroll = useCallback((top: number, isUserScrolling: boolean) => {
    if (top > 3 && isUserScrolling) {
      allowScrollToEnd.current = false
      clearTimeout(timeoutApi.current)
    }
  }, [])

  const onStartReached = useCallback(async () => {
    if (pending.current.startReached || keyword.length) return
    pending.current.startReached = true
    try {
      await getPreviousData()
    } finally {
      pending.current.startReached = false
    }
  }, [getPreviousData, keyword.length])

  const onEndReached = useCallback(async () => {
    if (pending.current.endReached || keyword.length) return
    pending.current.endReached = true
    try {
      if (!allowScrollToEnd.current) {
        allowScrollToEnd.current = true
        await getRealtimeData()
      }
    } finally {
      pending.current.endReached = false
    }
  }, [getRealtimeData, keyword.length])

  const onLayout = useCallback(() => {
    if (allowScrollToEnd.current) scrollRef.current?.scrollToEnd()
    else getIndexKeyword(keyword, logs)
  }, [getIndexKeyword, keyword, logs])

  const renderItem = useCallback(
    ({ item, index }: { item: string; index: number }) => {
      if (!keyword) return <BoxItem>{item}</BoxItem>
      const indexx = item.toLowerCase().indexOf(keyword.toLowerCase())
      const keys = [
        item.substring(0, indexx),
        `<span style="background: ${logs.length - 1 - indexSearch === index ? "#ff9632" : "#ffff00"}; color: #555f64">${item.substring(indexx, indexx + keyword.length)}</span>`,
        item.substring(indexx + keyword.length, item.length),
      ]
      if (indexx > -1) {
        return (
          <BoxItem
            dangerouslySetInnerHTML={{
              __html: keys.join(""),
            }}
          />
        )
      }
      return <BoxItem>{item}</BoxItem>
    },
    [indexSearch, keyword, logs.length],
  )

  return (
    <Modal open={isOpen} onClose={onClose}>
      <Body>
        <Content>
          <ScrollInverted
            onScroll={onScroll}
            ref={scrollRef}
            data={logs}
            renderItem={renderItem}
            onLayout={onLayout}
            onStartReached={onStartReached}
            onEndReached={onEndReached}
          />
          {openSearch ? (
            <BoxSearch>
              <InputSearch
                autoFocus
                value={keyword}
                placeholder="Search logs"
                onChange={onChangeKeyword}
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
      </Body>
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

const Body = styled(Box)`
  display: flex;
  height: 100dvh;
  align-items: center;
  justify-content: center;
`

const Content = styled(Box)`
  width: 1600px;
  max-width: 85%;
  height: 700px;
  max-height: 80%;
  background-color: black;
  position: relative;
  white-space: pre-wrap;
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

const BoxItem = styled("div")`
  color: white;
  font-size: 16px;
  padding: 3px 16px;
`

export default ModalLogs
