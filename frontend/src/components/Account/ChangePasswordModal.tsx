import { ChangeEvent, FC, useEffect, useState } from "react"

import {
  Box,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  styled,
  Tooltip,
  Typography,
} from "@mui/material"

import InputPassword from "components/Account/InputPassword"
import {
  ALLOWED_SPECIAL_CHARACTERS,
  regexIgnoreS,
  regexPassword,
} from "const/Auth"

type ChangePasswordModalProps = {
  onClose: () => void
  open: boolean
  onSubmit: (oldPass: string, newPass: string) => void
}

const ChangePasswordModal: FC<ChangePasswordModalProps> = ({
  onClose,
  open,
  onSubmit,
}) => {
  const [errors, setErrors] = useState<{ [key: string]: string }>({})
  const [values, setValues] = useState<{ [key: string]: string }>({})

  // Clear on every close, not just the Close button: the page also closes this
  // modal itself once a submit settles. The inputs are uncontrolled and the
  // Dialog unmounts them, so state left behind here outlives what the user can
  // see - the next attempt would submit the previous old password from a field
  // that looks empty.
  useEffect(() => {
    if (!open) {
      setValues({})
      setErrors({})
    }
  }, [open])
  const onChangeValue = (
    event: ChangeEvent<HTMLInputElement>,
    validate?: (value: string) => string,
  ) => {
    const { name, value } = event.target
    setValues({ ...values, [name]: value })
    if (name === "new_password" && values.confirm_password) {
      const newErrors: { [key: string]: string } = {}
      const errorNewPass = validate?.(value) || ""
      const errorConfirmPass = validateReEnter(values.confirm_password)
      if (!errorNewPass) {
        newErrors[name] = errorNewPass
        newErrors["confirm_password"] =
          value !== values.confirm_password ? "Passwords do not match" : ""
      } else {
        newErrors[name] = errorNewPass
        newErrors["confirm_password"] = errorConfirmPass
      }
      setErrors({ ...errors, ...newErrors })
      return
    }
    setErrors({ ...errors, [name]: validate?.(value) || "" })
  }

  const validatePassword = (value: string): string => {
    if (!value) return "This field is required"
    if (value.length > 255)
      return "The text may not be longer than 255 characters"
    if (!regexPassword.test(value)) {
      return "Your password must be at least 6 characters long and must contain at least one letter, number, and special character"
    }
    if (regexIgnoreS.test(value)) {
      return "Allowed special characters (!#$%&()*+,-./@_|)"
    }
    return ""
  }

  const validateReEnter = (value: string): string => {
    if (!value) return "This field is required"
    if (value !== values.new_password) {
      return "Passwords do not match"
    }
    return ""
  }

  // A mismatch is already flagged by onChangeValue as the new password is typed,
  // so blurring only has to report the field being left empty.
  const validateNewPasswordOnBlur = () => {
    if (!values.new_password)
      setErrors((pre) => ({ ...pre, new_password: "This field is required" }))
  }

  // The mismatch check belongs here and not only in onChangeValue: this is the
  // whole of submit-time validity, so submission can be gated on this alone.
  // Consulting the live `errors` state instead would skip the fields it has no
  // entry for, and report only some of the reasons the form was refused.
  const validateForm = () => {
    const errorPassword = !values.password ? "This field is required" : ""
    const errorNewPass = validatePassword(values.new_password)
    const errorConfirmPass =
      validatePassword(values.confirm_password) ||
      validateReEnter(values.confirm_password)
    return {
      password: errorPassword,
      new_password: errorNewPass,
      confirm_password: errorConfirmPass,
    }
  }

  const onChangePass = async () => {
    const newErrors: { [key: string]: string } = validateForm()
    if (Object.values(newErrors).some(Boolean)) {
      setErrors(newErrors)
      return
    }
    await onSubmit(values.password, values.new_password)
  }

  return (
    <Dialog open={open} onClose={onClose}>
      <DialogTitle>
        <BoxTitle>
          <Typography sx={{ fontWeight: 600, fontSize: 18 }}>
            Change Password
          </Typography>
          <Typography style={{ fontSize: 13 }}>
            <span style={{ color: "red" }}>*</span> is required
          </Typography>
        </BoxTitle>
      </DialogTitle>
      <DialogContent>
        <BoxConfirm>
          <FormInline>
            <Label>
              Old Password <span style={{ color: "red" }}>*</span>
            </Label>
            <InputPassword
              onChange={(e) => onChangeValue(e, validatePassword)}
              name="password"
              error={errors.password}
              onBlur={(e) => onChangeValue(e, validatePassword)}
              placeholder="Old Password"
            />
          </FormInline>
          <FormInline>
            <Label>
              New Password <span style={{ color: "red" }}>*</span>
            </Label>
            <InputPassword
              onChange={(e) => onChangeValue(e, validatePassword)}
              name="new_password"
              error={errors.new_password}
              placeholder="New Password"
              onBlur={validateNewPasswordOnBlur}
            />
          </FormInline>
          <FormInline>
            <Label>
              Confirm Password <span style={{ color: "red" }}>*</span>
            </Label>
            <InputPassword
              onChange={(e) => onChangeValue(e, validateReEnter)}
              name="confirm_password"
              error={errors.confirm_password}
              placeholder="Confirm Password"
              onBlur={(e) => onChangeValue(e, validateReEnter)}
            />
          </FormInline>

          <HelperText>
            At least 6 characters including letters, numbers, and{" "}
            <Tooltip title={ALLOWED_SPECIAL_CHARACTERS} arrow>
              <SpecialCharsLink>special characters</SpecialCharsLink>
            </Tooltip>
          </HelperText>
        </BoxConfirm>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} variant={"outlined"}>
          Close
        </Button>
        <Button onClick={() => onChangePass()} variant={"contained"}>
          UPDATE
        </Button>
      </DialogActions>
    </Dialog>
  )
}

const BoxTitle = styled(Box)({
  display: "flex",
  justifyContent: "space-between",
})

const BoxConfirm = styled(Box)({ margin: "20px 0" })

const FormInline = styled(Box)({
  display: "flex",
  justifyContent: "space-between",
  marginBottom: 10,
  gap: 30,
})

const Label = styled(Typography)({ fontSize: 14, marginTop: 7, width: "100%" })

const HelperText = styled(Typography)({
  fontSize: 12,
  color: "#6b7280",
  marginTop: 4,
})

const SpecialCharsLink = styled("span")({
  color: "#1e2125",
  textDecoration: "underline solid",
  fontWeight: 500,
  cursor: "help",
})

export default ChangePasswordModal
