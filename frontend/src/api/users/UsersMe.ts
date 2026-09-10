import {
  UserDTO,
  UpdateUserDTO,
  UpdateUserPasswordDTO,
} from "api/users/UsersApiDTO"
import { API_TIMEOUT } from "const/API"
import axios from "utils/axios"

export const getMeApi = async (): Promise<UserDTO> => {
  const response = await axios.get("/users/me")
  return response.data
}

export const updateMeApi = async (data: UpdateUserDTO): Promise<UserDTO> => {
  const response = await axios.put("/users/me", data)
  return response.data
}

export const updateMePasswordApi = async (
  data: UpdateUserPasswordDTO,
): Promise<UserDTO> => {
  const response = await axios.put("/users/me/password", data)
  return response.data
}

export const deleteMeApi = async (): Promise<string> => {
  const response = await axios.delete("/users/me")
  return response.data
}

export const logoutFreeUserApi = async (): Promise<{
  message: string
  logged_out: boolean
  cleanup_after_minutes?: number
}> => {
  const response = await axios.post("/users/me/free/logout", undefined, {
    timeout: API_TIMEOUT.LOGOUT,
  })
  return response.data
}
