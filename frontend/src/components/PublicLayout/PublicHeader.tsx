import { FC } from "react"
import { useSelector } from "react-redux"
import { Link, useLocation } from "react-router-dom"

import { Box, styled, Typography } from "@mui/material"

import { Z_INDEX } from "const/Layout"
import {
  BG_COLOR,
  BORDER_COLOR,
  FONT_SIZE,
  FONT_WEIGHT,
  NAV_LINK,
  TEXT_COLOR,
} from "const/Style"
import { selectCurrentUser } from "store/slice/User/UserSelector"

const PublicHeader: FC = () => {
  const user = useSelector(selectCurrentUser)
  const location = useLocation()
  const isLoginPage = location.pathname === "/login"
  const isRegisterPage = location.pathname === "/register"
  const isPublicPage = location.pathname === "/public"

  return (
    <HeaderContainer>
      {isPublicPage ? (
        <HeaderContent>
          <HeaderLogo src="/static/optinist_logo.png" alt="ARAYA OptiNiSt" />
          <HeaderTitle>ARAYA OptiNiSt</HeaderTitle>
        </HeaderContent>
      ) : (
        <HeaderLogoLink to="/public">
          <HeaderContent>
            <HeaderLogo src="/static/optinist_logo.png" alt="ARAYA OptiNiSt" />
            <HeaderTitle>ARAYA OptiNiSt</HeaderTitle>
          </HeaderContent>
        </HeaderLogoLink>
      )}
      <NavSection>
        {user ? (
          <NavButton to="/dashboard">Dashboard</NavButton>
        ) : (
          !isLoginPage &&
          !isRegisterPage && <NavButton to="/login">Login</NavButton>
        )}
      </NavSection>
    </HeaderContainer>
  )
}

const HeaderContainer = styled(Box)({
  width: "98%",
  height: 64,
  backgroundColor: BG_COLOR.HEADER,
  borderBottom: `1px solid ${BORDER_COLOR.DEFAULT}`,
  boxShadow:
    "0px 2px 4px -1px rgba(0,0,0,0.2), 0px 4px 5px 0px rgba(0,0,0,0.14), 0px 1px 10px 0px rgba(0,0,0,0.12)",
  display: "flex",
  alignItems: "center",
  padding: "0 24px",
  position: "fixed",
  top: 0,
  left: 0,
  zIndex: Z_INDEX.HEADER,
})

const HeaderLogoLink = styled(Link)({
  textDecoration: "none",
  display: "flex",
  alignItems: "center",
  transition: "opacity 0.2s",
  ":hover": {
    opacity: 0.8,
  },
})

const HeaderContent = styled(Box)({
  display: "flex",
  alignItems: "center",
  gap: 12,
})

const HeaderLogo = styled("img")({
  height: 40,
  width: "auto",
})

const HeaderTitle = styled(Typography)({
  fontSize: FONT_SIZE.CARD_TITLE,
  fontWeight: FONT_WEIGHT.SEMIBOLD,
  color: TEXT_COLOR.BLACK,
})

const NavSection = styled(Box)({
  marginLeft: "auto",
  display: "flex",
  alignItems: "center",
  gap: 16,
})

const NavLink = styled(Link)(NAV_LINK)

const NavButton = styled(Link)({
  display: "inline-block",
  padding: "8px 16px",
  fontSize: FONT_SIZE.SMALL,
  fontWeight: FONT_WEIGHT.MEDIUM,
  color: BG_COLOR.WHITE,
  backgroundColor: BG_COLOR.BUTTON_DARK,
  borderRadius: 6,
  textDecoration: "none",
  transition: "background-color 0.2s",
  ":hover": {
    backgroundColor: BG_COLOR.BUTTON_DARK_HOVER,
  },
})

export default PublicHeader
