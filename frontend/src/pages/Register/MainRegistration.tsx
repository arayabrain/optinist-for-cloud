import {
  ChangeEvent,
  FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react"
import { useDispatch, useSelector } from "react-redux"
import { Link, useNavigate } from "react-router-dom"

import CheckCircleIcon from "@mui/icons-material/CheckCircle"
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined"
import LockOutlinedIcon from "@mui/icons-material/LockOutlined"
import MailOutlineIcon from "@mui/icons-material/MailOutline"
import PersonOutlineIcon from "@mui/icons-material/PersonOutline"
import {
  Alert,
  Box,
  CircularProgress,
  Snackbar,
  styled,
  Tooltip,
  Typography,
} from "@mui/material"

import PublicHeader from "components/PublicLayout/PublicHeader"
import {
  ALLOWED_SPECIAL_CHARACTERS,
  regexIgnoreS,
  regexPassword,
} from "const/Auth"
import { FONT_WEIGHT, TEXT_COLOR } from "const/Style"
import {
  registerUser,
  resendVerificationEmail,
} from "store/slice/Registration/RegistrationActions"
import {
  selectRegistrationError,
  selectRegistrationLoading,
  selectRegistrationSuccess,
  selectRegistrationUser,
  selectResendEmailLoading,
  selectResendEmailSuccess,
} from "store/slice/Registration/RegistrationSelector"
import {
  clearAllRegistrationState,
  clearRegistrationErrors,
  clearResendSuccess,
} from "store/slice/Registration/RegistrationSlice"
import { AppDispatch } from "store/store"

interface FormData {
  email: string
  password: string
  confirmPassword: string
  name: string
}

const RegistrationForm = () => {
  const navigate = useNavigate()
  const dispatch = useDispatch<AppDispatch>()

  // Redux Selectors
  const loading = useSelector(selectRegistrationLoading)
  const success = useSelector(selectRegistrationSuccess)
  const error = useSelector(selectRegistrationError)
  const user = useSelector(selectRegistrationUser)
  const resendingEmail = useSelector(selectResendEmailLoading)
  const resendSuccess = useSelector(selectResendEmailSuccess)

  // Local State
  const [formData, setFormData] = useState<FormData>({
    email: "",
    password: "",
    confirmPassword: "",
    name: "",
  })

  const [validationError, setValidationError] = useState("")
  const [showPassword, setShowPassword] = useState(false)
  const [agreedToTerms, setAgreedToTerms] = useState(false)
  const [showResendSnackbar, setShowResendSnackbar] = useState(false)
  const [fieldErrors, setFieldErrors] = useState<Record<string, boolean>>({})
  const [resendCooldown, setResendCooldown] = useState(false)
  const cooldownTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const startCooldown = useCallback(() => {
    setResendCooldown(true)
    if (cooldownTimerRef.current) {
      clearTimeout(cooldownTimerRef.current)
    }
    cooldownTimerRef.current = setTimeout(() => {
      setResendCooldown(false)
      cooldownTimerRef.current = null
    }, 60000)
  }, [])

  // Cleanup on mount/unmount
  useEffect(() => {
    dispatch(clearRegistrationErrors())

    return () => {
      dispatch(clearAllRegistrationState())
      if (cooldownTimerRef.current) {
        clearTimeout(cooldownTimerRef.current)
      }
    }
  }, [dispatch])

  // Show snackbar when resend is successful
  useEffect(() => {
    if (resendSuccess) {
      setShowResendSnackbar(true)
      dispatch(clearResendSuccess())
    }
  }, [resendSuccess, dispatch])

  // Handle input change
  const handleChange = (e: ChangeEvent<HTMLInputElement>) => {
    const { name, value } = e.target
    setFormData((prev) => ({ ...prev, [name]: value }))

    // Clear validation error
    if (validationError) {
      setValidationError("")
      setFieldErrors({})
    }

    // Clear server error
    if (error) {
      dispatch(clearRegistrationErrors())
    }
  }

  // Validate form
  const validateForm = (): boolean => {
    if (!formData.email || !formData.password || !formData.name) {
      setValidationError("Please fill in all fields")
      setFieldErrors({
        name: !formData.name,
        email: !formData.email,
        password: !formData.password,
        confirmPassword: !formData.confirmPassword,
      })
      return false
    }

    if (formData.name.trim().length < 2) {
      setValidationError("Name must be at least 2 characters")
      setFieldErrors({ name: true })
      return false
    }

    if (formData.password.length > 255) {
      setValidationError("The text may not be longer than 255 characters")
      setFieldErrors({ password: true })
      return false
    }

    if (!regexPassword.test(formData.password)) {
      setValidationError(
        "Your password must be at least 6 characters long and must contain at least one letter, number, and special character",
      )
      setFieldErrors({ password: true })
      return false
    }

    if (regexIgnoreS.test(formData.password)) {
      setValidationError("Allowed special characters (!#$%&()*+,-./@_|)")
      setFieldErrors({ password: true })
      return false
    }

    if (formData.password !== formData.confirmPassword) {
      setValidationError("password is not match")
      setFieldErrors({ confirmPassword: true })
      return false
    }

    if (!agreedToTerms) {
      setValidationError(
        "You must agree to the Terms of Service and Privacy Policy",
      )
      return false
    }

    return true
  }

  // Handle form submission
  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()

    if (!validateForm()) {
      return
    }

    const resultAction = await dispatch(
      registerUser({
        email: formData.email,
        password: formData.password,
        name: formData.name.trim(),
        organization_id: 1,
      }),
    )

    if (registerUser.fulfilled.match(resultAction)) {
      // eslint-disable-next-line no-console
      console.log("Pre-registration successful! Please verify your email.")
      startCooldown()
    } else {
      // eslint-disable-next-line no-console
      console.error("Pre-registration failed:", resultAction.payload)
    }
  }

  // Handle resend email
  const handleResendEmail = async () => {
    startCooldown()
    const resultAction = await dispatch(resendVerificationEmail(formData.email))

    if (resendVerificationEmail.fulfilled.match(resultAction)) {
      // eslint-disable-next-line no-console
      console.log("Email resent successfully")
    } else {
      // eslint-disable-next-line no-console
      console.error("Email resend failed:", resultAction.payload)
    }
  }

  // Success view
  if (success) {
    return (
      <>
        <PublicHeader />
        <PageWrapper>
          <FormCard>
            <SuccessContent>
              <CheckCircleIcon sx={{ fontSize: 80, color: "#10b981", mb: 2 }} />

              <Title>Registration Almost Complete!</Title>

              <Subtitle>
                A verification email has been sent to{" "}
                <strong>{user?.email || formData.email}</strong>
              </Subtitle>

              <Alert severity="info" icon={<InfoOutlinedIcon />} sx={{ mb: 3 }}>
                <Typography variant="body2">
                  To complete your registration, please check your email and
                  click the verification link. You&apos;ll be able to log in
                  once your email is verified.
                </Typography>
              </Alert>

              {resendCooldown && (
                <Alert severity="warning" sx={{ mb: 2, fontSize: 12 }}>
                  Please wait a while before requesting another verification
                  email.
                </Alert>
              )}

              <ResendButton
                onClick={handleResendEmail}
                disabled={resendingEmail || resendCooldown}
              >
                {resendingEmail ? (
                  <>
                    <CircularProgress size={16} sx={{ mr: 1 }} />
                    Sending...
                  </>
                ) : (
                  "Resend Verification Email"
                )}
              </ResendButton>

              <SubmitButton onClick={() => navigate("/login")}>
                Go to Login Page
              </SubmitButton>
            </SuccessContent>
          </FormCard>

          {/* Resend success snackbar */}
          <Snackbar
            open={showResendSnackbar}
            autoHideDuration={6000}
            onClose={() => setShowResendSnackbar(false)}
            anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
          >
            <Alert
              onClose={() => setShowResendSnackbar(false)}
              severity="success"
              sx={{ width: "100%" }}
            >
              Verification email resent successfully
            </Alert>
          </Snackbar>
        </PageWrapper>
      </>
    )
  }

  // Registration form
  return (
    <>
      <PublicHeader />
      <PageWrapper>
        <FormCard>
          <CardHeader>
            <LogoWrapper>
              <Logo src="/static/optinist_logo.png" alt="OptiNiSt Logo" />
            </LogoWrapper>
            <Title>Create your account</Title>
            <Subtitle>Sign up to get started with OptiNiSt</Subtitle>
          </CardHeader>

          <form onSubmit={handleSubmit}>
            {/* Validation or server error */}
            {(validationError || error) && (
              <Alert severity="error" sx={{ mb: 3, fontSize: 12 }}>
                {validationError || error}
              </Alert>
            )}

            {/* Name input */}
            <Box sx={{ mb: 3 }}>
              <LabelField>Name</LabelField>
              <InputWrapper>
                <IconWrapper>
                  <PersonOutlineIcon sx={{ fontSize: 18, color: "#9ca3af" }} />
                </IconWrapper>
                <Input
                  autoComplete="off"
                  name="name"
                  value={formData.name}
                  onChange={handleChange}
                  placeholder="John Doe"
                  disabled={loading}
                  autoFocus
                  className={fieldErrors.name ? "error" : ""}
                />
              </InputWrapper>
            </Box>

            {/* Email input */}
            <Box sx={{ mb: 3 }}>
              <LabelField>Email</LabelField>
              <InputWrapper>
                <IconWrapper>
                  <MailOutlineIcon sx={{ fontSize: 18, color: "#9ca3af" }} />
                </IconWrapper>
                <Input
                  autoComplete="off"
                  type="email"
                  name="email"
                  value={formData.email}
                  onChange={handleChange}
                  placeholder="name@example.com"
                  disabled={loading}
                  className={fieldErrors.email ? "error" : ""}
                />
              </InputWrapper>
            </Box>

            {/* Password input */}
            <Box sx={{ mb: 3 }}>
              <LabelField>Password</LabelField>
              <InputWrapper>
                <IconWrapper>
                  <LockOutlinedIcon sx={{ fontSize: 18, color: "#9ca3af" }} />
                </IconWrapper>
                <Input
                  autoComplete="off"
                  type={showPassword ? "text" : "password"}
                  name="password"
                  value={formData.password}
                  onChange={handleChange}
                  placeholder="••••••••"
                  disabled={loading}
                  className={fieldErrors.password ? "error" : ""}
                />
              </InputWrapper>
              <HelperText>
                At least 6 characters including letters, numbers, and{" "}
                <Tooltip title={ALLOWED_SPECIAL_CHARACTERS} arrow>
                  <SpecialCharsLink>special characters</SpecialCharsLink>
                </Tooltip>
              </HelperText>
            </Box>

            {/* Confirm password input */}
            <Box sx={{ mb: 3 }}>
              <LabelField>Confirm Password</LabelField>
              <InputWrapper>
                <IconWrapper>
                  <LockOutlinedIcon sx={{ fontSize: 18, color: "#9ca3af" }} />
                </IconWrapper>
                <Input
                  autoComplete="off"
                  type={showPassword ? "text" : "password"}
                  name="confirmPassword"
                  value={formData.confirmPassword}
                  onChange={handleChange}
                  placeholder="••••••••"
                  disabled={loading}
                  className={fieldErrors.confirmPassword ? "error" : ""}
                />
              </InputWrapper>
            </Box>

            {/* Show password checkbox */}
            <CheckboxWrapper>
              <Checkbox
                type="checkbox"
                id="show-password"
                checked={showPassword}
                onChange={(e) => setShowPassword(e.target.checked)}
              />
              <CheckboxLabel htmlFor="show-password">
                Show Password
              </CheckboxLabel>
            </CheckboxWrapper>

            {/* Terms of Service / Privacy Policy agreement */}
            <CheckboxWrapper>
              <Checkbox
                type="checkbox"
                id="agree-to-terms"
                checked={agreedToTerms}
                onChange={(e) => {
                  setAgreedToTerms(e.target.checked)
                  if (validationError) setValidationError("")
                }}
                disabled={loading}
              />
              <CheckboxLabel htmlFor="agree-to-terms">
                I agree to the{" "}
                <PolicyLink
                  to="/terms"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Terms of Service
                </PolicyLink>{" "}
                and{" "}
                <PolicyLink
                  to="/privacy"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Privacy Policy
                </PolicyLink>
              </CheckboxLabel>
            </CheckboxWrapper>

            {/* Submit button */}
            <SubmitButton type="submit" disabled={loading}>
              {loading ? "Registering..." : "Sign Up"}
            </SubmitButton>

            {/* Login link */}
            <LoginWrapper>
              Already have an account? <LoginLink to="/login">Login</LoginLink>
            </LoginWrapper>
          </form>
        </FormCard>
      </PageWrapper>
    </>
  )
}

// ========================================
// Styled Components
// ========================================

const PageWrapper = styled(Box)({
  width: "100%",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  paddingTop: 64,
})

const FormCard = styled(Box)({
  width: "100%",
  maxWidth: 448,
  padding: "32px",
  backgroundColor: "#ffffff",
  border: "1px solid #e5e7eb",
  borderRadius: 8,
  boxShadow:
    "0 10px 15px -3px rgba(0, 0, 0, 0.1), 0 4px 6px -2px rgba(0, 0, 0, 0.05)",
})

const CardHeader = styled(Box)({ marginBottom: 32, textAlign: "center" })

const LogoWrapper = styled(Box)({
  display: "flex",
  justifyContent: "center",
  marginBottom: 24,
})

const Logo = styled("img")({ height: 60, width: "auto" })

const Title = styled(Typography)({
  fontSize: 24,
  fontWeight: 600,
  color: "#000000",
  marginBottom: 8,
})

const Subtitle = styled(Typography)({
  fontSize: 14,
  color: "#6b7280",
  marginBottom: 16,
})

const LabelField = styled("label")({
  display: "block",
  fontSize: 14,
  fontWeight: 500,
  color: "#374151",
  marginBottom: 8,
})

const InputWrapper = styled(Box)({
  position: "relative",
  display: "flex",
  alignItems: "center",
})

const IconWrapper = styled(Box)({
  position: "absolute",
  left: 12,
  display: "flex",
  alignItems: "center",
  pointerEvents: "none",
})

const Input = styled("input")({
  width: "100%",
  height: 40,
  borderRadius: 6,
  border: "1px solid #d1d5db",
  paddingLeft: 40,
  paddingRight: 12,
  backgroundColor: "#ffffff",
  color: "#000000",
  fontSize: 14,
  transition: "all 0.2s",
  outline: "none",
  boxSizing: "border-box",
  "::placeholder": { color: "#9ca3af" },
  ":focus": {
    borderColor: "#000000",
    boxShadow: "0 0 0 3px rgba(0, 0, 0, 0.1)",
  },
  ":disabled": { backgroundColor: "#f3f4f6", cursor: "not-allowed" },
  "&.error": {
    borderColor: "#ef4444",
    boxShadow: "0 0 0 3px rgba(239, 68, 68, 0.1)",
  },
  "&.error:focus": {
    borderColor: "#ef4444",
    boxShadow: "0 0 0 3px rgba(239, 68, 68, 0.2)",
  },
})

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

const CheckboxWrapper = styled(Box)({
  display: "flex",
  alignItems: "center",
  marginBottom: 16,
})

const Checkbox = styled("input")({
  width: 16,
  height: 16,
  marginRight: 8,
  cursor: "pointer",
})

const CheckboxLabel = styled("label")({
  fontSize: 14,
  color: "#374151",
  cursor: "pointer",
})

const SubmitButton = styled("button")({
  width: "100%",
  height: 40,
  backgroundColor: "#000000",
  color: "#ffffff",
  borderRadius: 6,
  border: "none",
  outline: "none",
  fontSize: 14,
  fontWeight: 500,
  cursor: "pointer",
  transition: "background-color 0.2s",
  ":hover": { backgroundColor: "#1f2937" },
  ":disabled": { backgroundColor: "#9ca3af", cursor: "not-allowed" },
  marginBottom: 16,
})

const ResendButton = styled("button")({
  width: "100%",
  height: 40,
  backgroundColor: "#ffffff",
  color: "#000000",
  borderRadius: 6,
  border: "1px solid #d1d5db",
  outline: "none",
  fontSize: 14,
  fontWeight: 500,
  cursor: "pointer",
  transition: "all 0.2s",
  marginBottom: 16,
  ":hover": { backgroundColor: "#f9fafb" },
  ":disabled": { opacity: 0.6, cursor: "not-allowed" },
})

const LoginWrapper = styled(Typography)({
  textAlign: "center",
  fontSize: 14,
  color: "#6b7280",
})

const LoginLink = styled(Link)({
  color: "#000000",
  textDecoration: "none",
  fontWeight: 500,
  ":hover": { textDecoration: "underline" },
})

const PolicyLink = styled(Link)({
  color: TEXT_COLOR.BLACK,
  textDecoration: "underline",
  fontWeight: FONT_WEIGHT.MEDIUM,
})

const SuccessContent = styled(Box)({ textAlign: "center" })

export default RegistrationForm
