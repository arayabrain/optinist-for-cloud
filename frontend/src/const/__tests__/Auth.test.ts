/**
 * The password charset rule the register form, the change-password modal and
 * the admin user editor all validate against.
 *
 * The expected set is written out literally rather than read back from
 * `ALLOWED_SPECIAL_CHARACTERS`: a test that derives its inputs from the
 * constant moves with it, and could no longer tell a widened rule from the
 * intended one. AUTH-15 covers the same rule end to end on the register form.
 */

import { describe, it, expect } from "@jest/globals"

import {
  ALLOWED_SPECIAL_CHARACTERS,
  regexIgnoreS,
  regexPassword,
} from "const/Auth"

const ALLOWED_SPECIALS = "!#$%&()*+,-./@_|"
const PRINTABLE_ASCII = Array.from({ length: 95 }, (_, i) =>
  String.fromCharCode(32 + i),
)
const isAllowed = (char: string) =>
  /[a-zA-Z0-9]/.test(char) || ALLOWED_SPECIALS.includes(char)
const forbidden = PRINTABLE_ASCII.filter((char) => !isAllowed(char))

describe("password charset rule", () => {
  it("flags every printable character outside the allowed set", () => {
    expect(PRINTABLE_ASCII.filter((char) => regexIgnoreS.test(char))).toEqual(
      forbidden,
    )
  })

  it("shows the user exactly the specials it accepts", () => {
    // The constant is the message text, so a widened rule with a stale message
    // would leave users guessing which characters are actually allowed
    expect(ALLOWED_SPECIAL_CHARACTERS.replace(/ /g, "")).toBe(ALLOWED_SPECIALS)
  })

  it("requires one of those same specials, so the two rules cannot drift", () => {
    for (const char of ALLOWED_SPECIALS) {
      expect(regexPassword.test(`Testa1${char}`)).toBe(true)
    }
    for (const char of forbidden) {
      expect(regexPassword.test(`Testa1${char}`)).toBe(false)
    }
    // AUTH-15's inputs satisfy the complexity rule, so the charset rule is the
    // branch that refuses them
    expect(regexPassword.test("Test@12<")).toBe(true)
    expect(regexIgnoreS.test("Test@12<")).toBe(true)
  })
})
