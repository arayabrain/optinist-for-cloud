import { useCallback, useEffect, useRef, useState } from "react"

import styled from "@emotion/styled"
import CloseIcon from "@mui/icons-material/Close"
import { Box, Modal } from "@mui/material"
import { ScrollInverted } from "@react-scroll-inverted/react-scroll"

import axios from "utils/axios"

type Props = {
  isOpen?: boolean
  onClose?: () => void
}

const ModalLogs = ({ isOpen = false, onClose }: Props) => {
  const [logs, setLogs] = useState<string[]>([])

  const nextOffset = useRef(-1)
  const isPending = useRef(false)
  const isPendingPre = useRef(false)

  const timeout = useRef<NodeJS.Timeout>()

  const allowEndScroll = useRef(true)
  const scrollRef = useRef<ScrollInverted | null>(null)

  const preOffset = useRef(-1)

  const serviceLogs = useCallback((offset: number, reverse?: boolean) => {
    return axios.get("/logs", { params: { offset, reverse } })
  }, [])

  const getPrevious = useCallback(async () => {
    if (isPendingPre.current) return
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
  }, [serviceLogs])

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
    getData()
    return () => {
      clearTimeout(timeout.current)
    }
  }, [getData])

  useEffect(() => {
    if (!isOpen) clearTimeout(timeout.current)
  }, [isOpen])

  const renderItem = useCallback(({ item }: { item: string }) => {
    return <BoxItem>{item}</BoxItem>
  }, [])

  return (
    <Modal open={isOpen} onClose={onClose}>
      <Body>
        <Content>
          <ButtonClose onClick={onClose}>
            <CloseIcon />
          </ButtonClose>
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
        </Content>
      </Body>
    </Modal>
  )
}

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
`

const ButtonClose = styled(Box)`
  position: absolute;
  width: 40px;
  height: 40px;
  background-color: white;
  display: flex;
  align-items: center;
  justify-content: center;
  right: 0;
  cursor: pointer;
  z-index: 1;
`

const BoxItem = styled("div")`
  color: white;
  font-size: 16px;
  padding: 3px 16px;
`

export default ModalLogs
