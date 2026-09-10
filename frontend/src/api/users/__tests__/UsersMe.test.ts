import { describe, it, expect, beforeEach, jest } from "@jest/globals"

/**
 * Tests for logoutFreeUserApi.
 *
 * The logout call must carry its own short timeout: the UI Sign Out path
 * awaits this call before clearing tokens, and the axios default timeout of
 * 10 minutes would strand the session behind a hung backend.
 */

const mockPost = jest.fn() as unknown as jest.Mock<
  Promise<{ data: { message: string; logged_out: boolean } }>
>

jest.mock("utils/axios", () => ({
  __esModule: true,
  default: { post: mockPost },
}))

describe("logoutFreeUserApi", () => {
  beforeEach(() => {
    jest.resetModules()
    jest.clearAllMocks()
  })

  it("posts the logout with the dedicated short timeout and returns the response data", async () => {
    mockPost.mockResolvedValue({
      data: { message: "ok", logged_out: true },
    })

    const { logoutFreeUserApi } = await import("api/users/UsersMe")
    const { API_TIMEOUT } = await import("const/API")

    const result = await logoutFreeUserApi()

    expect(mockPost).toHaveBeenCalledWith("/users/me/free/logout", undefined, {
      timeout: API_TIMEOUT.LOGOUT,
    })
    expect(API_TIMEOUT.LOGOUT).toBeLessThan(API_TIMEOUT.DEFAULT)
    expect(result).toEqual({ message: "ok", logged_out: true })
  })
})
