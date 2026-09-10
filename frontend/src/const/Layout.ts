export const APP_BAR_HEIGHT = 60
export const CONTENT_HEIGHT = `calc(100vh - 48px - ${APP_BAR_HEIGHT}px)` // 48px: spacing(3)
export const DRAWER_WIDTH = 300
export const MAX_DRAWER_WIDTH = 350
export const RIGHT_DRAWER_WIDTH = 320

/**
 * Centralized z-index values for consistent layering.
 * Based on Material UI's z-index scale:
 * - Drawer: 1200
 * - Modal: 1300
 * - Snackbar: 1400
 * - Tooltip: 1500
 */
export const Z_INDEX = {
  /** Elements within cards/panels (relative z-index) */
  CARD_ELEMENT: 1,
  /** Workspace toolbar, fixed elements */
  TOOLBAR: 4,
  /** File select dialog elements */
  DIALOG_ELEMENT: 10,
  /** Popover, popup share dialogs */
  POPUP: 100,
  /** Image plot overlays */
  PLOT_OVERLAY: 999,
  /** Header, floating buttons */
  HEADER: 1000,
  /** Cookie consent notice - below modals and snackbars so both stay clickable */
  CONSENT_BANNER: 1200,
  /** Loading overlay - high but below MUI modals */
  LOADING_OVERLAY: 1250,
} as const
