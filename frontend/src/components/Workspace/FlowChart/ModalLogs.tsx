import { ChangeEvent, useCallback, useEffect, useRef, useState } from "react"

import styled from "@emotion/styled"
import AdbIcon from "@mui/icons-material/Adb"
import ArrowBackIosIcon from "@mui/icons-material/ArrowBackIos"
import ArrowForwardIosIcon from "@mui/icons-material/ArrowForwardIos"
import CloseIcon from "@mui/icons-material/Close"
import ErrorIcon from "@mui/icons-material/Error"
import InfoIcon from "@mui/icons-material/Info"
import WarningIcon from "@mui/icons-material/Warning"
import { Box, Modal } from "@mui/material"
import { ScrollInverted } from "@react-scroll-inverted/react-scroll"

import axios from "utils/axios"

type Props = {
  isOpen?: boolean
  onClose?: () => void
}

const ModalLogs = ({ isOpen = false, onClose }: Props) => {
  const [logs, setLogs] = useState<string[]>([])

  const [keyword, setKeywork] = useState("")

  const nextOffset = useRef(-1)
  const isPending = useRef(false)
  const isPendingPre = useRef(false)

  const timeout = useRef<NodeJS.Timeout>()

  const allowEndScroll = useRef(true)
  const scrollRef = useRef<ScrollInverted | null>(null)
  const preOffset = useRef(-1)
  const indexSearch = useRef(-1)

  const serviceLogs = useCallback((offset: number, reverse?: boolean) => {
    return axios.get("/logs", { params: { offset, reverse } })
  }, [])

  const getPrevious = useCallback(
    async (allow?: boolean) => {
      if (isPendingPre.current && !allow) return
      isPendingPre.current = true
      try {
        const { data } = await serviceLogs(preOffset.current)
        if (data.prev_offset < preOffset.current) {
          preOffset.current = data.prev_offset
          setLogs((pre) => [...data.data, ...pre])
        }
      } finally {
        isPendingPre.current = false
      }
    },
    [serviceLogs],
  )

  const getData = useCallback(async () => {
    if (isPending.current) return
    isPending.current = true
    try {
      const { data } = await serviceLogs(nextOffset.current, false)
      if (!allowEndScroll.current) return
      if (preOffset.current === -1) preOffset.current = data.prev_offset
      if (data.next_offset > nextOffset.current) {
        nextOffset.current = data.next_offset
        setLogs((pre) => [...pre, ...data.data])
      }
    } finally {
      isPending.current = false
      timeout.current = setTimeout(() => {
        if (allowEndScroll.current) getData()
      }, 2000)
    }
  }, [serviceLogs])

  const onScroll = useCallback((top: number, isUserScrolling: boolean) => {
    if (top > 3 && isUserScrolling) allowEndScroll.current = false
  }, [])

  useEffect(() => {
    getPrevious(true)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    getData()
    return () => {
      clearTimeout(timeout.current)
    }
  }, [getData])

  useEffect(() => {
    if (!isOpen) clearTimeout(timeout.current)
  }, [isOpen])

  const onChangeKeyword = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => {
      allowEndScroll.current = !e.target.value
      setKeywork(e.target.value)
      indexSearch.current = logs.findIndex((el) => el.includes(e.target.value))
      scrollRef.current?.scrollToIndex(indexSearch.current)
    },
    [logs],
  )

  const onNextSearch = useCallback(() => {
    if (!keyword.trim().length) return
    let inNext = logs.findIndex(
      (e, i) => e.includes(keyword) && i > indexSearch.current,
    )
    if (inNext < 0) inNext = logs.findIndex((e) => e.includes(keyword))
    indexSearch.current = inNext
    scrollRef.current?.scrollToIndex(indexSearch.current)
  }, [keyword, logs])

  const onPreSearch = useCallback(() => {
    if (!keyword.trim().length) return
    let inNext = logs.findIndex(
      (e, i) => e.includes(keyword) && i < indexSearch.current,
    )
    if (inNext < 0) {
      inNext = [...logs].reverse().findIndex((e) => e.includes(keyword))
    }
    indexSearch.current = inNext
    scrollRef.current?.scrollToIndex(indexSearch.current)
  }, [keyword, logs])

  const renderItem = useCallback(
    ({ item }: { item: string }) => {
      if (!keyword) return <BoxItem>{item}</BoxItem>
      const keys = item.split(keyword)
      if (keys.length > 1) {
        return (
          <BoxItem
            dangerouslySetInnerHTML={{
              __html: keys.join(
                `<span style="background: #c1ddff; color: #555f64">${keyword}</span>`,
              ),
            }}
          />
        )
      }
      return <BoxItem>{item}</BoxItem>
    },
    [keyword],
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
            onLayout={() => {
              if (allowEndScroll.current) {
                scrollRef.current?.scrollToEnd()
              }
            }}
            onStartReached={getPrevious}
            onEndReached={() => {
              if (!allowEndScroll.current) getData()
              allowEndScroll.current = true
            }}
          />
          <BoxSearch>
            <InputSearch placeholder="Search logs" onChange={onChangeKeyword} />
            <Box display="flex">
              <BoxIconSearch onClick={onPreSearch}>
                <ArrowBackIosIcon />
              </BoxIconSearch>
              <BoxIconSearch onClick={onNextSearch}>
                <ArrowForwardIosIcon />
              </BoxIconSearch>
            </Box>
            <ButtonClose onClick={onClose}>
              <CloseIcon />
            </ButtonClose>
          </BoxSearch>
          <BoxFilter>
            <MenuFilter>
              <InfoIcon /> INFO
            </MenuFilter>
            <MenuFilter>
              <AdbIcon /> DEBUG
            </MenuFilter>
            <MenuFilter>
              <WarningIcon /> WARNING
            </MenuFilter>
            <MenuFilter>
              <ErrorIcon /> ERROR
            </MenuFilter>
          </BoxFilter>
        </Content>
      </Body>
    </Modal>
  )
}

const MenuFilter = styled(Box)`
  display: flex;
  padding: 5px 16px;
  align-items: center;
  color: white;
  cursor: pointer;
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

const BoxItem = styled("div")`
  color: white;
  font-size: 16px;
  padding: 3px 16px;
`

export default ModalLogs
