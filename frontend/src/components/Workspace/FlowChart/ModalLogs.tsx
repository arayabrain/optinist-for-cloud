import { ChangeEvent, useCallback, useEffect, useRef, useState } from "react"

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

type Props = {
  isOpen?: boolean
  onClose?: () => void
}

const ModalLogs = ({ isOpen = false, onClose }: Props) => {
  const [keyword, setKeywork] = useState("")
  const [logs, setLogs] = useState<string[]>([])
  const [levels, setLevels] = useState<TLevelsLog>()
  const [openSearch, setOpenSearch] = useState(true)
  const [indexSearch, setIndexSearch] = useState(-1)

  const nextOffset = useRef(-1)
  const preOffset = useRef(-1)
  const allowEndScroll = useRef(true)
  const timeoutApi = useRef<NodeJS.Timeout>()
  const scrollRef = useRef<ScrollInverted | null>(null)
  const keywordRef = useRef(keyword)
  const levelsRef = useRef(levels)
  const timeoutKeyword = useRef<NodeJS.Timeout>()

  useEffect(() => {
    keywordRef.current = keyword
  }, [keyword])

  useEffect(() => {
    levelsRef.current = levels
  }, [levels])

  const serviceLogs = useCallback(
    async (offset: number, reverse: boolean = false) => {
      const res = await axios.get("/logs", {
        params: {
          offset,
          reverse,
          levels: levelsRef.current,
          limit: 200,
          search: keywordRef.current,
        },
      })
      if (res.config.params?.levels !== levelsRef.current) throw ""
      if (res.config.params?.search !== keywordRef.current) throw ""
      return res
    },
    [],
  )

  const getPreviousData = useCallback(async () => {
    const { data } = await serviceLogs(preOffset.current, true)
    if (data.prev_offset < preOffset.current) {
      preOffset.current = data.prev_offset
      setLogs((pre) => [...data.data, ...pre])
    }
  }, [serviceLogs])

  const getNextData = useCallback(async () => {
    const { data } = await serviceLogs(preOffset.current, true)
    if (data.prev_offset < preOffset.current) {
      preOffset.current = data.prev_offset
      setLogs((pre) => [...data.data, ...pre])
    }
  }, [serviceLogs])

  const getRealtimeData = useCallback(async () => {
    console.log("asdas")
    clearTimeout(timeoutApi.current)
    try {
      const { data } = await serviceLogs(nextOffset.current)
      if (!allowEndScroll.current) return
      if (data.next_offset > nextOffset.current) {
        nextOffset.current = data.next_offset
        setLogs((pre) => [...pre, ...data.data])
      }
      if (preOffset.current === -1) {
        preOffset.current = data.prev_offset
        getPreviousData()
      }
    } catch {
      /* empty */
    }
    timeoutApi.current = setTimeout(getRealtimeData, 2000)
  }, [getPreviousData, serviceLogs])

  useEffect(() => {
    getRealtimeData()
    return () => {
      clearTimeout(timeoutApi.current)
    }
  }, [getRealtimeData])

  useEffect(() => {
    nextOffset.current = -1
    preOffset.current = -1
    setLogs([])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [levels])

  useEffect(() => {
    if (keyword.length) {
      setKeywork("")
      allowEndScroll.current = true
      getRealtimeData()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openSearch])

  useEffect(() => {
    if (keywordRef.current.length) {
      const _indexSearch = logs.findIndex((el) =>
        el.toLowerCase().includes(keywordRef.current.toLowerCase()),
      )
      setIndexSearch(_indexSearch)
      if (_indexSearch > -1) scrollRef.current?.scrollIntoView(_indexSearch)
    }
  }, [logs])

  const onScroll = useCallback((top: number, isUserScrolling: boolean) => {
    if (top > 3 && isUserScrolling) allowEndScroll.current = false
  }, [])

  const onChangeKeyword = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      allowEndScroll.current = !e.target.value
      setKeywork(e.target.value)
      const _indexSearch = logs.findIndex((el) =>
        el.toLowerCase().includes(e.target.value.toLowerCase()),
      )
      setIndexSearch(_indexSearch)
      if (_indexSearch > -1) scrollRef.current?.scrollIntoView(_indexSearch)
      clearTimeout(timeoutKeyword.current)
      timeoutKeyword.current = setTimeout(() => {
        if (allowEndScroll.current) getRealtimeData()
      }, 300)
    },
    [getRealtimeData, logs],
  )

  const onNextSearch = useCallback(() => {
    const _indexSearch = logs.findIndex(
      (el, i) =>
        el.toLowerCase().includes(keyword.toLowerCase()) && i > indexSearch,
    )
    if (_indexSearch < 0) {
      getNextData()
      return
    }
    setIndexSearch(_indexSearch)
    if (_indexSearch > -1) scrollRef.current?.scrollIntoView(_indexSearch)
  }, [getNextData, indexSearch, keyword, logs])

  const onPrevSearch = useCallback(() => {
    const _indexSearch = logs.findIndex(
      (el, i) =>
        el.toLowerCase().includes(keyword.toLowerCase()) && i < indexSearch,
    )
    if (_indexSearch < 0) {
      getPreviousData()
      return
    }
    setIndexSearch(_indexSearch)
    if (_indexSearch > -1) scrollRef.current?.scrollIntoView(_indexSearch)
  }, [getPreviousData, indexSearch, keyword, logs])

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

  const onChangeTypeFilter = useCallback(
    (t: TLevelsLog) => {
      if (t === levels) setLevels(undefined)
      else setLevels(t)
    },
    [levels],
  )

  const onStartReached = useCallback(() => {
    if (keyword.length) return
    getPreviousData()
  }, [getPreviousData, keyword.length])

  const onEndReached = useCallback(() => {
    if (keyword.length) return
    if (!allowEndScroll.current) getRealtimeData()
    allowEndScroll.current = true
  }, [getRealtimeData, keyword.length])

  const onLayout = useCallback(() => {
    if (allowEndScroll.current) scrollRef.current?.scrollToEnd()
  }, [])

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
              active={levels === TLevelsLog.INFO}
              onClick={() => onChangeTypeFilter(TLevelsLog.INFO)}
            >
              <InfoIcon />
              <span>{TLevelsLog.INFO}</span>
            </MenuFilter>
            <MenuFilter
              active={levels === TLevelsLog.WARNING}
              onClick={() => onChangeTypeFilter(TLevelsLog.WARNING)}
            >
              <WarningIcon />
              <span>{TLevelsLog.WARNING}</span>
            </MenuFilter>
            <MenuFilter
              active={levels === TLevelsLog.DEBUG}
              onClick={() => onChangeTypeFilter(TLevelsLog.DEBUG)}
            >
              <AdbIcon />
              <span>{TLevelsLog.DEBUG}</span>
            </MenuFilter>
            <MenuFilter
              active={levels === TLevelsLog.ERROR}
              onClick={() => onChangeTypeFilter(TLevelsLog.ERROR)}
            >
              <ErrorIcon />
              <span>{TLevelsLog.ERROR}</span>
            </MenuFilter>
            <MenuFilter
              active={levels === TLevelsLog.CRITICAL}
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
})<{ active: boolean }>`
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
