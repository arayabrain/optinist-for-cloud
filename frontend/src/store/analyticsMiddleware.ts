import { AnyAction, Middleware } from "@reduxjs/toolkit"

import { run, runByCurrentUid } from "store/slice/Pipeline/PipelineActions"
import { registerUser } from "store/slice/Registration/RegistrationActions"
import { ModeType } from "store/slice/Standalone/StandaloneType"
import { login } from "store/slice/User/UserActions"
import { trackEvent } from "utils/analytics"

const EVENT_BY_ACTION_TYPE = new Map<string, string>([
  [registerUser.fulfilled.type, "sign_up"],
  [login.fulfilled.type, "login"],
  [run.fulfilled.type, "run_pipeline"],
  [runByCurrentUid.fulfilled.type, "run_pipeline"],
])

// proxyLogin shares login's action type; only the real login carries a LoginDTO.
const isUserLogin = (arg: unknown): boolean =>
  typeof arg === "object" && arg !== null && "email" in arg

export const analyticsMiddleware: Middleware = (api) => (next) => (action) => {
  const result = next(action)

  const { type, meta } = action as AnyAction
  const event = EVENT_BY_ACTION_TYPE.get(type)
  if (!event) return result

  const state = api.getState() as { mode: ModeType }
  if (state.mode.mode) return result

  if (event === "login" && !isUserLogin(meta?.arg)) return result

  trackEvent(event)
  return result
}
