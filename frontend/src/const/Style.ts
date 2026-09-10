// Typography scale
export const FONT_SIZE = {
  HERO_TITLE: "3rem",
  HERO_TITLE_MD: "3.75rem",
  DISPLAY_SM: "1.5rem",
  HERO_SUBTITLE_MD: "2rem",
  SECTION_TITLE: "2rem",
  SECTION_SUBTITLE: "1.125rem",
  CARD_TITLE: "1.25rem",
  BODY: "1rem",
  SMALL: "0.875rem",
  ICON_LG: "1.875rem",
} as const

// Font weights
export const FONT_WEIGHT = {
  REGULAR: 400,
  MEDIUM: 500,
  SEMIBOLD: 600,
  BOLD: 700,
  BLACK: 900,
} as const

// Line heights
export const LINE_HEIGHT = {
  TIGHT: 1.1,
  NORMAL: 1.6,
} as const

// Letter spacing
export const LETTER_SPACING = {
  TIGHT: "-0.025em",
  WIDE: "0.1em",
} as const

// Text colors
export const TEXT_COLOR = {
  BLACK: "#000000",
  PRIMARY: "#111827",
  SECONDARY: "#6b7280",
  MUTED: "#4b5563",
  ACCENT: "#2563eb",
  MARQUEE_LABEL: "#9ca3af",
  WHITE: "white",
  WHITE_SOFT: "rgba(255, 255, 255, 0.85)",
  WHITE_MUTED: "rgba(255, 255, 255, 0.6)",
} as const

// Accent colors (for icons and highlights)
export const ACCENT_COLOR = {
  PRIMARY: { bg: "rgba(37, 99, 235, 0.1)", color: "#2563eb" },
  MAGENTA: { bg: "rgba(225, 29, 72, 0.1)", color: "#e11d48" },
  CYAN: { bg: "rgba(13, 148, 136, 0.1)", color: "#0d9488" },
  GREEN: { bg: "rgba(5, 150, 105, 0.1)", color: "#059669" },
  YELLOW: { bg: "rgba(217, 119, 6, 0.1)", color: "#d97706" },
} as const

// Background colors
export const BG_COLOR = {
  PAGE: "#f9fafb",
  CARD: "#f9fafb",
  WHITE: "white",
  HEADER: "#E1DEDB",
  DARK: "#1f2937",
  BUTTON_PRIMARY: "#2563eb",
  BUTTON_PRIMARY_HOVER: "#1d4ed8",
  BUTTON_DARK: "#000000c4",
  BUTTON_DARK_HOVER: "#00000090",
  BENEFIT_ICON: "rgba(255, 255, 255, 0.15)",
} as const

// Borders
export const BORDER_COLOR = {
  DEFAULT: "#e5e7eb",
} as const

// Shared header nav link style
export const NAV_LINK = {
  fontSize: FONT_SIZE.SMALL,
  color: TEXT_COLOR.BLACK,
  textDecoration: "none",
  whiteSpace: "nowrap",
  ":hover": {
    textDecoration: "underline",
  },
} as const

// Shared icon container style (48x48 rounded square)
export const ICON_BOX = {
  width: 48,
  height: 48,
  borderRadius: 8,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  flexShrink: 0,
  "& .material-symbols-outlined": {
    fontSize: FONT_SIZE.ICON_LG,
  },
} as const

// Shared button base style
export const BUTTON_BASE = {
  fontFamily: "inherit",
  fontSize: FONT_SIZE.BODY,
  fontWeight: FONT_WEIGHT.REGULAR,
  borderRadius: 8,
  border: "none",
  cursor: "pointer",
  transition: "background-color 0.2s",
  backgroundColor: BG_COLOR.BUTTON_PRIMARY,
  color: TEXT_COLOR.WHITE,
  "&:hover": {
    backgroundColor: BG_COLOR.BUTTON_PRIMARY_HOVER,
  },
} as const
