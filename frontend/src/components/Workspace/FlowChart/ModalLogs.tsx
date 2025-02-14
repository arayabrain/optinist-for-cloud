import { useCallback, useEffect, useRef, useState, WheelEvent } from "react"

import styled from "@emotion/styled"
import CloseIcon from "@mui/icons-material/Close"
import { Box, Modal } from "@mui/material"

import axios from "utils/axios"

type Props = {
  isOpen?: boolean
  onClose?: () => void
}

const ModalLogs = ({ isOpen = false, onClose }: Props) => {
  const [logs, setLogs] = useState<string[]>([])

  const next_offset = useRef(-1)
  const isPending = useRef(false)

  const allowEndScroll = useRef(true)

  const scrollRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (!scrollRef.current || !allowEndScroll.current) return
    const { scrollHeight, clientHeight } = scrollRef.current
    setTimeout(() => {
      scrollRef.current?.scrollTo({ top: scrollHeight - clientHeight })
    }, 0)
  }, [logs?.length])

  const getData = useCallback(() => {
    if (isPending.current) return
    isPending.current = true
    axios
      .get("/logs", {
        params: {
          offset: next_offset.current,
          reverse: false,
          line_offset: 500,
        },
      })
      .then(({ data }) => {
        isPending.current = false
        if (data.next_offset > next_offset.current) {
          next_offset.current = data.next_offset
          setLogs((pre) => [...pre, ...data.data])
        }
      })
  }, [])

  useEffect(() => {
    getData()
    const interval = setInterval(() => {
      getData()
    }, 2000)
    return () => {
      clearInterval(interval)
    }
  }, [getData])

  const onScroll = useCallback((event: WheelEvent<HTMLDivElement>) => {
    const { scrollHeight, clientHeight, scrollTop } =
      event.target as HTMLDivElement
    allowEndScroll.current = scrollTop + 55 >= scrollHeight - clientHeight
  }, [])

  return (
    <Modal open={isOpen} onClose={onClose}>
      <Body>
        <Content>
          <ButtonClose onClick={onClose}>
            <CloseIcon />
          </ButtonClose>
          <Box
            ref={scrollRef}
            overflow="auto"
            height="100%"
            onScroll={onScroll}
          >
            {logs.map((log, index) => (
              <BoxItem key={`${index}_${log}`}>{log}</BoxItem>
            ))}
            <Box height="50px" />
          </Box>
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
