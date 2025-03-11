import {
  ForwardedRef,
  forwardRef,
  MouseEvent,
  useCallback,
  useEffect,
  useRef,
} from "react"

import styled from "@emotion/styled"
import { Box } from "@mui/material"

type Props = {
  logs: { text: string; id: string }[]
  keyword: string
  onLayout?: (layout: { height: number; scrollHeight: number }) => void
  onStartReached?: () => void
  searchId: string
}

const ScrollLogs = forwardRef(function ScrollLogsRef(
  { logs, keyword, onLayout, onStartReached, searchId }: Props,
  ref: ForwardedRef<HTMLDivElement | null>,
) {
  const scrollRef = useRef<HTMLDivElement>()
  const refHeight = useRef(-1)
  const refScrollHeight = useRef(-1)
  const isUserScroll = useRef(true)

  const getLayout = useCallback(() => {
    if (!scrollRef.current) return -1
    const { clientHeight, scrollHeight, scrollTop } = scrollRef.current
    if (
      refHeight.current !== clientHeight ||
      refScrollHeight.current !== scrollHeight
    ) {
      onLayout?.({ height: clientHeight, scrollHeight })
      const change = scrollHeight - refScrollHeight.current
      isUserScroll.current = false
      if (!keyword.length) {
        if (scrollTop <= 3) {
          scrollRef.current.scrollTo({ top: change })
        } else if (
          refScrollHeight.current - refHeight.current <=
          scrollTop + 3
        ) {
          scrollRef.current.scrollTo({ top: scrollHeight - clientHeight + 50 })
        }
      }
      refHeight.current = clientHeight
      refScrollHeight.current = scrollHeight
      setTimeout(() => {
        isUserScroll.current = true
      }, 10)
    }
    return window.requestAnimationFrame(getLayout)
  }, [keyword.length, onLayout])

  useEffect(() => {
    const refFrame = getLayout()
    return () => {
      window.cancelAnimationFrame(refFrame)
    }
  }, [getLayout])

  useEffect(() => {
    if (!scrollRef.current) return
    const element = scrollRef.current.querySelector(`#scroll_item_${searchId}`)
    if (!element) return
    const { height } = scrollRef.current.getBoundingClientRect()
    const { top } = element.getBoundingClientRect()
    if (top < 50 || top > height + 40) element.scrollIntoView()
  }, [searchId])

  const onScroll = useCallback(
    (event: MouseEvent<HTMLDivElement>) => {
      if (!isUserScroll.current) return
      const top = (event.target as HTMLDivElement).scrollTop
      if (top <= 0) onStartReached?.()
    },
    [onStartReached],
  )

  const renderItem = useCallback(
    ({ item }: { item: { text: string; id: string } }) => {
      if (!keyword.trim()) return <BoxItem>{item.text}</BoxItem>
      const indexx = item.text
        .toLowerCase()
        .indexOf(keyword.trim().toLowerCase())
      const keys = [
        item.text.substring(0, indexx),
        `<span style="background: ${searchId === item.id ? "#ff9632" : "#ffff00"}; color: #555f64">${item.text.substring(indexx, indexx + keyword.trim().length)}</span>`,
        item.text.substring(indexx + keyword.trim().length, item.text.length),
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
      return <BoxItem>{item.text}</BoxItem>
    },
    [searchId, keyword],
  )

  return (
    <Box height="100%" ref={ref}>
      <BoxScroll ref={scrollRef} onScroll={onScroll}>
        {logs.map((e) => (
          <div id={`scroll_item_${e.id}`} key={`${e}_${e.id}`}>
            {renderItem({ item: e })}
          </div>
        ))}
      </BoxScroll>
    </Box>
  )
})

const BoxScroll = styled(Box)`
  height: 100%;
  overflow: auto;
  font-family: Monospaced, sans-serif;
`

const BoxItem = styled("div")`
  color: white;
  font-size: 16px;
  padding: 3px 16px;
`

export default ScrollLogs
