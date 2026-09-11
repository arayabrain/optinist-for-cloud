import { useEffect, useState } from "react"
import { useDispatch, useSelector } from "react-redux"
import { useNavigate } from "react-router-dom"

import CheckIcon from "@mui/icons-material/Check"
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined"
import {
  Box,
  Button,
  styled,
  Typography,
  CircularProgress,
  Chip,
  Alert,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
} from "@mui/material"

import { PlanName } from "const/Subscription"
import {
  getSubscriptionPlan,
  getUserSubscription,
  createCheckoutSession,
  cancelSubscription,
  reactivateSubscription,
} from "store/slice/Subscriptions/SubscriptionActions"
import {
  selectSubscriptionPlans,
  selectUserSubscription,
  selectSubscriptionLoading,
  selectSubscriptionError,
  selectIsSubscriptionExpired,
  selectCurrentPlanId,
} from "store/slice/Subscriptions/SubscriptionSelector"
import { clearError } from "store/slice/Subscriptions/SubscriptionSlice"
import type {
  SubscriptionPlan,
  PlanFeature,
} from "store/slice/Subscriptions/SubscriptionType"
import { selectCurrentUser } from "store/slice/User/UserSelector"
import { AppDispatch } from "store/store"
import {
  getBillingCycleText,
  getCurrencySymbol,
  getPlanFeatures,
} from "utils/subscriptions/SubscriptionUtils"

const SubscriptionPlans = () => {
  const user = useSelector(selectCurrentUser)
  const plans = useSelector(selectSubscriptionPlans)
  const userSubscription = useSelector(selectUserSubscription)
  const loading = useSelector(selectSubscriptionLoading)
  const error = useSelector(selectSubscriptionError)
  const isSubscriptionExpired = useSelector(selectIsSubscriptionExpired)
  const currentPlanId = useSelector(selectCurrentPlanId)

  const dispatch = useDispatch<AppDispatch>()
  const navigate = useNavigate()

  // State for downgrade confirmation dialog
  const [showDowngradeDialog, setShowDowngradeDialog] = useState(false)
  const [selectedPlanId, setSelectedPlanId] = useState<number | null>(null)
  const [processingPlanId, setProcessingPlanId] = useState<number | null>(null) // Track which plan is being processed

  // State for data storage info dialog
  const [showDataStorageDialog, setShowDataStorageDialog] = useState(false)
  const [recoveryMessage, setRecoveryMessage] = useState<string | null>(null)

  // Fetch data on component mount
  useEffect(() => {
    const loadData = async () => {
      dispatch(clearError())
      dispatch(getSubscriptionPlan())

      if (user?.id) {
        dispatch(getUserSubscription())
      }
    }

    loadData()
  }, [dispatch, user?.id])

  // Check if user has a specific plan
  const isCurrentPlan = (planId: number) => {
    return currentPlanId === planId
  }

  // Check if the selected plan is a downgrade (free plan)
  const isDowngrade = (planId: number) => {
    const plan = plans.find((p) => p.id === planId)
    return plan?.price === 0
  }

  const handleReactivatePlan = async (planId: number) => {
    if (!user?.id) {
      // Handle case where user is not logged in
      navigate("/login")
      return
    }

    try {
      setProcessingPlanId(planId)

      // Dispatch the action to reactivate subscription
      const resultAction = await dispatch(reactivateSubscription(user.id))

      // Check if the action was fulfilled
      if (reactivateSubscription.fulfilled.match(resultAction)) {
        // eslint-disable-next-line no-console
        console.log("Subscription reactivated successfully")
      } else {
        // eslint-disable-next-line no-console
        console.error("Failed to reactivate subscription:", resultAction.error)
      }
    } catch (error) {
      // eslint-disable-next-line no-console
      console.error("Error reactivating subscription:", error)
    } finally {
      setProcessingPlanId(null)
    }
  }

  const handleUpgradeClick = async (planId: number) => {
    // Check if it's a downgrade (free plan)
    if (isDowngrade(planId)) {
      setSelectedPlanId(planId)
      setShowDowngradeDialog(true)
    } else {
      // For upgrades, create checkout session and redirect to Stripe
      if (!user?.id) {
        // Handle case where user is not logged in
        navigate("/login")
        return
      }

      try {
        setProcessingPlanId(planId)

        // Dispatch the action to create checkout session
        const resultAction = await dispatch(createCheckoutSession(planId))

        // Check if the action was fulfilled
        if (createCheckoutSession.fulfilled.match(resultAction)) {
          const { checkout_url } = resultAction.payload

          // Redirect to Stripe checkout
          window.location.href = checkout_url
        } else if (createCheckoutSession.rejected.match(resultAction)) {
          const errorMessage = resultAction.payload || ""
          // Check if this is a subscription recovery case (409)
          if (errorMessage.includes("already a premium user")) {
            dispatch(clearError())
            setRecoveryMessage(errorMessage)
            // Refresh subscription data to reflect the recovered subscription
            dispatch(getUserSubscription())
          } else {
            // Non-recovery rejection — Redux error state handles the UI display
            // eslint-disable-next-line no-console
            console.error("Checkout session failed:", errorMessage)
          }
        } else {
          // Defensive guard: createAsyncThunk should only produce
          // fulfilled or rejected, but log if an unexpected state occurs.
          // eslint-disable-next-line no-console
          console.warn("Unexpected checkout result action:", resultAction)
        }
      } catch (error) {
        // eslint-disable-next-line no-console
        console.error("Error creating checkout session:", error)
        // Handle error - maybe show a toast notification
      } finally {
        setProcessingPlanId(null)
      }
    }
  }

  const handleConfirmDowngrade = () => {
    if (selectedPlanId) {
      if (user?.id) {
        dispatch(cancelSubscription())
      } else {
        // eslint-disable-next-line no-console
        console.error("User not logged in")
      }
    }
    setShowDowngradeDialog(false)
    setSelectedPlanId(null)
  }

  const handleCancelDowngrade = () => {
    setShowDowngradeDialog(false)
    setSelectedPlanId(null)
  }

  const handleDataStorageDialogOpen = () => {
    setShowDataStorageDialog(true)
  }

  const handleDataStorageDialogClose = () => {
    setShowDataStorageDialog(false)
  }

  const getExpirationDate = () => {
    if (userSubscription?.expiration) {
      return new Date(userSubscription.expiration).toLocaleDateString()
    }
    return "N/A"
  }

  const handleRetry = () => {
    dispatch(clearError())
    dispatch(getSubscriptionPlan())
    if (user?.id) {
      dispatch(getUserSubscription())
    }
  }

  // Filter only active plans
  const activePlans = plans.filter((plan) => plan.status === true)

  // Get price display for a plan
  const getPriceDisplay = (plan: SubscriptionPlan) => {
    const currencySymbol = getCurrencySymbol(plan.currency)
    const billingCycle = getBillingCycleText(plan.billing_cycle)

    if (plan.price === 0) {
      return PlanName.FREE
    }

    const basePrice = (plan.price / 100).toFixed(0)
    return `${currencySymbol}${basePrice}/${billingCycle}`
  }

  // Check if a specific plan is currently being processed
  const isPlanProcessing = (planId: number) => {
    return processingPlanId === planId
  }

  // Loading state
  if (loading) {
    return (
      <BoxWrapper>
        <CircularProgress />
        <Typography variant="body1" sx={{ mt: 2 }}>
          Loading subscription plans...
        </Typography>
      </BoxWrapper>
    )
  }

  // Error state
  if (error) {
    return (
      <BoxWrapper>
        <Typography variant="h6" color="error" sx={{ mb: 2 }}>
          Error loading subscription plans
        </Typography>
        <Typography variant="body2" color="text.secondary">
          {typeof error === "string" ? error : "An unexpected error occurred"}
        </Typography>
        <Button variant="outlined" onClick={handleRetry} sx={{ mt: 2 }}>
          Retry
        </Button>
      </BoxWrapper>
    )
  }

  // No plans state
  if (activePlans.length === 0) {
    return (
      <BoxWrapper>
        <Typography variant="h6" color="text.secondary" sx={{ mb: 2 }}>
          No subscription plans available
        </Typography>
        <Typography variant="body2" color="text.secondary">
          Please try again later or contact support.
        </Typography>
        <Button variant="outlined" onClick={handleRetry} sx={{ mt: 2 }}>
          Retry
        </Button>
      </BoxWrapper>
    )
  }

  return (
    <BoxWrapper>
      <SubscriptionTitle variant="h3">Subscription Plans</SubscriptionTitle>

      {/* Scheduled downgrade notification */}
      {userSubscription?.scheduled_downgrade && (
        <Alert severity="warning" sx={{ mb: 3, maxWidth: "600px" }}>
          <Typography variant="body2">
            <strong>Subscription Canceled:</strong> Your subscription has been
            canceled and will remain active until{" "}
            <strong>{getExpirationDate()}</strong>. You can renew your
            subscription after this date.
          </Typography>
        </Alert>
      )}

      {/* Subscription recovery notification */}
      {recoveryMessage && (
        <Alert severity="success" sx={{ mb: 3, maxWidth: "600px" }}>
          <Typography variant="body2">{recoveryMessage}</Typography>
        </Alert>
      )}

      {/* Tax information notice */}
      <TaxNotice severity="info" sx={{ mb: 2, maxWidth: "600px" }}>
        <Typography variant="body2">
          <strong>Tax Information:</strong> Applicable taxes will be calculated
          automatically based on your location during checkout. Final price may
          include consumption tax.
        </Typography>
      </TaxNotice>

      {/* Data storage policy info button */}
      <Box
        sx={{
          display: "flex",
          justifyContent: "center",
          mb: 3,
          maxWidth: "600px",
        }}
      >
        <Button
          onClick={handleDataStorageDialogOpen}
          variant="outlined"
          startIcon={<InfoOutlinedIcon />}
          sx={{
            borderColor: "#e3f2fd",
            color: "#1976d2",
            backgroundColor: "#f8faff",
            textTransform: "none",
            borderRadius: "0.5rem",
            px: 2,
            py: 1,
            "&:hover": {
              backgroundColor: "#e3f2fd",
              borderColor: "#bbdefb",
            },
          }}
        >
          <Typography variant="body2" sx={{ fontWeight: 500 }}>
            Data Storage Policy
          </Typography>
        </Button>
      </Box>

      {/* Current subscription status */}
      {userSubscription &&
        userSubscription.plan_name !== PlanName.FREE &&
        !isSubscriptionExpired && (
          <SubscriptionStatus>
            <Typography variant="body1">
              Current Plan: <strong>{userSubscription.plan_name}</strong>
            </Typography>
            <Typography variant="body2" color="text.secondary">
              {isSubscriptionExpired ? (
                <span style={{ color: "#dc2626" }}>
                  Expired on{" "}
                  {new Date(userSubscription.expiration).toLocaleDateString()}
                </span>
              ) : (
                `Expires on ${new Date(userSubscription.expiration).toLocaleDateString()}`
              )}
            </Typography>
          </SubscriptionStatus>
        )}

      <SubscriptionWrapper>
        <SubscriptionContent>
          {activePlans.map((plan) => {
            const features = getPlanFeatures(plan)
            const isCurrent = isCurrentPlan(plan.id)
            const isFree = plan.price === 0
            const priceDisplay = getPriceDisplay(plan)
            const isProcessing = isPlanProcessing(plan.id)

            return (
              <PlanCard
                key={plan.id}
                data-testid={`plan-card-${plan.name}`}
                isHighlighted={plan.name === PlanName.PREMIUM}
              >
                <PlanHeader>
                  <PlanTitle variant="h4">{plan.name}</PlanTitle>

                  <PriceContainer>
                    <PlanPrice variant="h5">{priceDisplay}</PlanPrice>
                    {!isFree && (
                      <Typography variant="caption" color="text.secondary">
                        + applicable taxes
                      </Typography>
                    )}
                  </PriceContainer>

                  {plan.billing_cycle === 2 && (
                    <Chip
                      label="Best Value"
                      color="primary"
                      size="small"
                      sx={{ mt: 1 }}
                    />
                  )}
                </PlanHeader>

                <FeaturesList>
                  {features.length > 0 ? (
                    features.map((feature: PlanFeature, index: number) => (
                      <FeatureItem key={index}>
                        <CheckIcon
                          sx={{
                            color: feature.isPremium ? "#16a34a" : "#6b7280",
                            fontSize: "1.25rem",
                          }}
                        />
                        <FeatureText isPremium={feature.isPremium}>
                          {feature.text}
                        </FeatureText>
                      </FeatureItem>
                    ))
                  ) : (
                    <Typography variant="body2" color="text.secondary">
                      No features available for this plan
                    </Typography>
                  )}
                </FeaturesList>

                <ButtonWrapper>
                  {isCurrent && !userSubscription?.scheduled_downgrade ? (
                    <CurrentPlanButton disabled>Current Plan</CurrentPlanButton>
                  ) : (
                    <UpgradeButton
                      variant="contained"
                      onClick={() => {
                        if (userSubscription?.scheduled_downgrade && !isFree) {
                          handleReactivatePlan(plan.id)
                        } else {
                          handleUpgradeClick(plan.id)
                        }
                      }}
                      disabled={
                        !user ||
                        isProcessing ||
                        (isFree && userSubscription?.scheduled_downgrade)
                      }
                      startIcon={
                        isProcessing ? <CircularProgress size={16} /> : null
                      }
                    >
                      {isProcessing
                        ? "Processing..."
                        : isFree
                          ? "Downgrade"
                          : userSubscription?.scheduled_downgrade
                            ? "Continue Plan"
                            : "Upgrade"}
                    </UpgradeButton>
                  )}
                </ButtonWrapper>
              </PlanCard>
            )
          })}
        </SubscriptionContent>
      </SubscriptionWrapper>

      {/* Data Storage Information Dialog */}
      <Dialog
        open={showDataStorageDialog}
        onClose={handleDataStorageDialogClose}
        maxWidth="md"
        fullWidth
      >
        <DialogTitle>
          <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
            <InfoOutlinedIcon color="primary" />
            <Typography
              variant="h6"
              component="div"
              sx={{ fontWeight: "bold" }}
            >
              Data Storage Policy
            </Typography>
          </Box>
        </DialogTitle>
        <DialogContent>
          <Box sx={{ display: "flex", flexDirection: "column", gap: 2.5 }}>
            <Typography
              variant="body2"
              sx={{ fontWeight: 600, fontSize: "1rem" }}
            >
              What happens to your data after subscription cancellation?
            </Typography>

            <Box>
              <Typography
                variant="body2"
                sx={{
                  mb: 1,
                  fontWeight: 500,
                  display: "flex",
                  alignItems: "center",
                  gap: 1,
                }}
              >
                📅 <span>Data Retention Period:</span>
              </Typography>
              <Typography variant="body2" color="text.secondary" sx={{ ml: 3 }}>
                Your data will be stored for <strong>30 days</strong> after
                subscription cancellation.
              </Typography>
            </Box>

            <Box>
              <Typography
                variant="body2"
                sx={{
                  mb: 1.5,
                  fontWeight: 500,
                  color: "#dc2626",
                  display: "flex",
                  alignItems: "center",
                  gap: 1,
                }}
              >
                🗑️ <span>Data that will be deleted after 30 days:</span>
              </Typography>
              <Box
                sx={{
                  ml: 3,
                  display: "flex",
                  flexDirection: "column",
                  gap: 0.8,
                }}
              >
                <Typography variant="body2" color="text.secondary">
                  • Premium project files and documents
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  • Export/import data beyond basic limits
                </Typography>
              </Box>
            </Box>

            <Box>
              <Typography
                variant="body2"
                sx={{
                  mb: 1.5,
                  fontWeight: 500,
                  color: "#16a34a",
                  display: "flex",
                  alignItems: "center",
                  gap: 1,
                }}
              >
                <span>Data that will be preserved:</span>
              </Typography>
              <Box
                sx={{
                  ml: 3,
                  display: "flex",
                  flexDirection: "column",
                  gap: 0.8,
                }}
              >
                <Typography variant="body2" color="text.secondary">
                  • Basic account information and profile
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  • Essential project data (up to free plan limits)
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  • Basic usage history and preferences
                </Typography>
              </Box>
            </Box>

            <Alert severity="warning" sx={{ mt: 1 }}>
              <Typography variant="body2">
                <strong>Important:</strong> We recommend downloading and backing
                up your important data before cancellation. You can reactivate
                your subscription anytime within the 30-day period to restore
                full access to your premium data.
              </Typography>
            </Alert>
          </Box>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={handleDataStorageDialogClose} variant="contained">
            I Understand
          </Button>
        </DialogActions>
      </Dialog>
      <Dialog
        open={showDowngradeDialog}
        onClose={handleCancelDowngrade}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle>
          <Typography variant="h6" component="div" sx={{ fontWeight: "bold" }}>
            Cancel Subscription
          </Typography>
        </DialogTitle>
        <DialogContent>
          <Typography variant="body1" sx={{ mb: 2 }}>
            Are you sure you want to cancel your subscription? Your subscription
            will be canceled at <strong>{getExpirationDate()}</strong>.
          </Typography>

          {/* Data Storage Warning in Dialog */}
          <Alert severity="warning" sx={{ mb: 2 }}>
            <Typography variant="body2" sx={{ fontWeight: 600, mb: 1 }}>
              Data Storage Notice:
            </Typography>
            <Typography variant="body2">
              Your premium data will be stored for <strong>30 days</strong>{" "}
              after cancellation. After this period, premium content including
              project files, advanced reports, and custom templates will be
              permanently deleted.
            </Typography>
          </Alert>

          <Typography variant="body2" color="text.secondary">
            You will lose access to premium features after this date, but you
            can resubscribe at any time within 30 days to restore full access to
            your data.
          </Typography>
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button
            onClick={handleCancelDowngrade}
            variant="outlined"
            sx={{ mr: 1 }}
          >
            No
          </Button>
          <Button
            onClick={handleConfirmDowngrade}
            variant="contained"
            color="error"
            autoFocus
          >
            Yes, Cancel Subscription
          </Button>
        </DialogActions>
      </Dialog>
    </BoxWrapper>
  )
}

// Styled Components
const BoxWrapper = styled(Box)({
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  padding: "5rem 2rem",
})

const SubscriptionTitle = styled(Typography)(() => ({
  fontWeight: "bold",
  color: "#111827",
  marginBottom: "2rem",
  textAlign: "center",
}))

const TaxNotice = styled(Alert)(() => ({
  borderRadius: "0.75rem",
  "& .MuiAlert-message": {
    width: "100%",
  },
}))

const SubscriptionStatus = styled(Box)(() => ({
  backgroundColor: "#f8fafc",
  border: "1px solid #e2e8f0",
  borderRadius: "0.5rem",
  padding: "1rem 1.5rem",
  marginBottom: "2rem",
  textAlign: "center",
  maxWidth: "400px",
}))

const SubscriptionWrapper = styled(Box)(() => ({
  width: "100%",
  maxWidth: "64rem",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
}))

const SubscriptionContent = styled(Box)(() => ({
  display: "flex",
  flexDirection: "row",
  gap: "2rem",
  width: "100%",
  "@media (max-width: 768px)": {
    flexDirection: "column",
  },
}))

const PlanCard = styled(Box, {
  shouldForwardProp: (prop) => prop !== "isHighlighted",
})<{ isHighlighted?: boolean }>(({ isHighlighted }) => ({
  flex: 1,
  backgroundColor: "white",
  borderRadius: "1rem",
  border: isHighlighted ? "2px solid #3b82f6" : "2px solid #dbeafe",
  padding: "2rem",
  boxShadow: isHighlighted
    ? "0 10px 25px -3px rgba(59, 130, 246, 0.1)"
    : "0 1px 3px 0 rgba(0, 0, 0, 0.1)",
  display: "flex",
  flexDirection: "column",
  position: "relative",
  transform: isHighlighted ? "scale(1.05)" : "scale(1)",
  transition: "transform 0.2s ease-in-out",
}))

const PlanHeader = styled(Box)(() => ({
  textAlign: "center",
  marginBottom: "2rem",
}))

const PlanTitle = styled(Typography)(() => ({
  fontWeight: "bold",
  color: "#111827",
  marginBottom: "0.5rem",
}))

const PriceContainer = styled(Box)(() => ({
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  gap: "0.25rem",
}))

const PlanPrice = styled(Typography)(() => ({
  fontWeight: "600",
  color: "#3b82f6",
}))

const FeaturesList = styled(Box)(() => ({
  display: "flex",
  flexDirection: "column",
  gap: "1rem",
  marginBottom: "3rem",
  flex: 1,
}))

const FeatureItem = styled(Box)(() => ({
  display: "flex",
  alignItems: "center",
  gap: "0.75rem",
}))

const FeatureText = styled(Typography, {
  shouldForwardProp: (prop) => prop !== "isPremium",
})<{ isPremium?: boolean }>(({ isPremium }) => ({
  color: isPremium ? "#16a34a" : "#374151",
  fontSize: "1rem",
  fontWeight: isPremium ? "500" : "400",
}))

const ButtonWrapper = styled(Box)(() => ({
  textAlign: "center",
}))

const CurrentPlanButton = styled(Button)(() => ({
  backgroundColor: "#f3f4f6",
  color: "#6b7280",
  padding: "0.75rem 1.5rem",
  borderRadius: "0.5rem",
  fontWeight: "500",
  width: "100%",
  textTransform: "none",
  "&:disabled": {
    backgroundColor: "#f3f4f6",
    color: "#6b7280",
  },
}))

const UpgradeButton = styled(Button)(() => ({
  backgroundColor: "#3b82f6",
  color: "white",
  padding: "0.75rem 2rem",
  borderRadius: "0.5rem",
  fontWeight: "500",
  width: "100%",
  textTransform: "none",
  "&:hover": {
    backgroundColor: "#2563eb",
  },
  "&:disabled": {
    backgroundColor: "#9ca3af",
    color: "#ffffff",
  },
}))

export default SubscriptionPlans
