import {
  ChangeEvent,
  FocusEvent,
  KeyboardEvent,
  useEffect,
  useRef,
  useState,
} from "react"
import { useSelector, useDispatch } from "react-redux"
import { useNavigate } from "react-router-dom"

import { useSnackbar, VariantType } from "notistack"

import { Edit, HelpOutline } from "@mui/icons-material"
import {
  Box,
  Button,
  FormControl,
  IconButton,
  Input,
  MenuItem,
  Select,
  styled,
  Switch,
  Tooltip,
  Typography,
} from "@mui/material"
import { isRejectedWithValue } from "@reduxjs/toolkit"

// import { ROLE } from "@types"
import ChangePasswordModal from "components/Account/ChangePasswordModal"
import DeleteConfirmModal from "components/common/DeleteConfirmModal"
import Loading from "components/common/Loading"
import {
  DeletionPriority,
  PlanName,
  SubscriptionUserStatus,
} from "const/Subscription"
import {
  getDeletionPriority,
  getUserSubscription,
  updateDeletionPriority,
} from "store/slice/Subscriptions/SubscriptionActions"
import {
  selectDeletionPriority,
  selectDeletionPriorityLoading,
  selectUserSubscription,
  selectUserSubscriptionLoading,
} from "store/slice/Subscriptions/SubscriptionSelector"
import {
  deleteMe,
  getMe,
  updateMe,
  updateMePassword,
} from "store/slice/User/UserActions"
import { selectCurrentUser, selectLoading } from "store/slice/User/UserSelector"
import { AppDispatch } from "store/store"
import { convertBytes } from "utils"
import {
  getAnalyticsConsent,
  isGtmEnabled,
  setAnalyticsConsent,
} from "utils/analytics"

const Account = () => {
  const user = useSelector(selectCurrentUser)
  const loading = useSelector(selectLoading)
  const userSubscription = useSelector(selectUserSubscription)
  const subscriptionLoading = useSelector(selectUserSubscriptionLoading)
  const deletionPriority = useSelector(selectDeletionPriority)
  const deletionPriorityLoading = useSelector(selectDeletionPriorityLoading)

  const dispatch = useDispatch<AppDispatch>()
  const navigate = useNavigate()

  const [isDeleteConfirmModalOpen, setIsDeleteConfirmModalOpen] =
    useState(false)
  const [isChangePwModalOpen, setIsChangePwModalOpen] = useState(false)
  const [isEditName, setIsEditName] = useState(false)
  const [isEditDeletionPriority, setIsEditDeletionPriority] = useState(false)
  const [isName, setIsName] = useState<string>()
  const [analyticsConsent, setAnalyticsConsentState] =
    useState(getAnalyticsConsent)

  const ref = useRef<HTMLInputElement>(null)

  const { enqueueSnackbar } = useSnackbar()

  const handleClickVariant = (variant: VariantType, mess: string) => {
    enqueueSnackbar(mess, { variant })
  }

  useEffect(() => {
    dispatch(getMe())
  }, [dispatch])

  useEffect(() => {
    if (!user) return
    setIsName(user.name)

    // Fetch user subscription and deletion priority when user is loaded
    if (user.id) {
      dispatch(getUserSubscription())
      dispatch(getDeletionPriority())
    }
  }, [user, dispatch])

  const handleCloseDeleteComfirmModal = () => {
    setIsDeleteConfirmModalOpen(false)
  }

  const onDeleteAccountClick = () => {
    setIsDeleteConfirmModalOpen(true)
  }

  const isPremiumUser =
    userSubscription &&
    !userSubscription.is_expired &&
    userSubscription.status === SubscriptionUserStatus.SUBSCRIBED

  const getDeleteAccountDescription = () => {
    if (isPremiumUser) {
      return `You have an active ${userSubscription.plan_name} subscription.`
    }
    return "Delete account will erase all of your data."
  }

  const getDeleteAccountWarnings = (): string[] | undefined => {
    if (isPremiumUser) {
      return [
        "Your subscription will be immediately canceled",
        "You will not receive a refund for the remaining period",
        "All your data (workspaces, experiments, files) will be permanently deleted",
        "This action cannot be undone",
      ]
    }
    return [
      "All your data (workspaces, experiments, files) will be permanently deleted",
      "This action cannot be undone",
    ]
  }

  const onConfirmDelete = async () => {
    if (!user) return
    try {
      const data = await dispatch(deleteMe())
      if (isRejectedWithValue(data)) {
        handleClickVariant("error", "Account deleted failed!")
      } else {
        navigate("/login")
      }
    } catch {
      handleClickVariant("error", "Account deleted failed!")
    } finally {
      handleCloseDeleteComfirmModal()
    }
  }

  const handleCloseChangePw = () => {
    setIsChangePwModalOpen(false)
  }

  const onChangePwClick = () => {
    setIsChangePwModalOpen(true)
  }

  const onConfirmChangePw = async (oldPass: string, newPass: string) => {
    const data = await dispatch(
      updateMePassword({ old_password: oldPass, new_password: newPass }),
    )
    if (isRejectedWithValue(data)) {
      handleClickVariant("error", "Failed to Change Password!")
    } else {
      handleClickVariant(
        "success",
        "Your password has been successfully changed!",
      )
    }
    handleCloseChangePw()
  }

  const onEditName = (e: ChangeEvent<HTMLInputElement>) => {
    setIsName(e.target.value)
  }

  const onBlur = async (event: FocusEvent) => {
    if (!user || !user.name || !user.email) return
    if (isName === user.name) {
      setIsEditName(false)
      return
    }
    const target = event.target as HTMLInputElement | HTMLTextAreaElement
    if (!target.value) {
      handleClickVariant("error", "Full name can't be empty!")
      setIsName(user?.name)
    } else {
      const data = await dispatch(
        updateMe({
          name: target.value,
          email: user.email,
        }),
      )
      if (isRejectedWithValue(data)) {
        handleClickVariant("error", "Full name edited failed!")
      } else {
        handleClickVariant("success", "Full name edited successfully!")
      }
    }
    setIsEditName(false)
  }

  const onClickUpgrade = () => {
    navigate("/subscription")
  }

  const onClickManage = () => {
    navigate("/subscription/manage")
  }
  // Not used in cloud implementation. Comment to remove ESLint warning.
  // const getRole = (role?: number) => {
  //   if (!role) return
  //   let newRole = ""
  //   switch (role) {
  //     case ROLE.ADMIN:
  //       newRole = "Admin"
  //       break
  //     case ROLE.OPERATOR:
  //       newRole = "Operator"
  //       break
  //   }
  //   return newRole
  // }

  const handleName = (event: KeyboardEvent) => {
    if (event.key === "Escape") {
      setIsName(user?.name)
      setIsEditName(false)
      return
    }
    if (event.key === "Enter") {
      if (ref.current) ref.current?.querySelector("input")?.blur?.()
      return
    }
  }

  const determineSubscriptionButtonStatus = () => {
    if (!userSubscription) {
      return SubscriptionUserStatus.FREE
    } else if (userSubscription.is_expired) {
      return SubscriptionUserStatus.EXPIRED
    } else {
      return SubscriptionUserStatus.SUBSCRIBED
    }
  }

  // Updated function to handle showing both buttons for users with subscription records
  const renderSubscriptionButtons = () => {
    const status = determineSubscriptionButtonStatus()

    // For users who never had a subscription (completely free users)
    if (status === SubscriptionUserStatus.FREE) {
      return (
        <Button
          variant="contained"
          color="primary"
          sx={{ ml: 2 }}
          onClick={onClickUpgrade}
          disabled={subscriptionLoading}
        >
          Upgrade
        </Button>
      )
    }

    // For users with subscription records (active or expired)
    if (
      status === SubscriptionUserStatus.SUBSCRIBED ||
      status === SubscriptionUserStatus.EXPIRED
    ) {
      return (
        <Box sx={{ ml: 2, display: "flex", gap: 1 }}>
          {status === SubscriptionUserStatus.EXPIRED && (
            <Button
              variant="contained"
              color="primary"
              onClick={onClickUpgrade}
              disabled={subscriptionLoading}
            >
              Upgrade
            </Button>
          )}
          <Button
            variant="contained"
            color="secondary"
            onClick={onClickManage}
            disabled={subscriptionLoading}
          >
            Manage
          </Button>
        </Box>
      )
    }

    // Fallback for loading/error states
    return null
  }

  // Helper function to format expiration date with server-validated expiration status
  const getExpirationInfo = () => {
    if (!userSubscription || !userSubscription.expiration) return null

    const expirationDate = new Date(userSubscription.expiration)

    if (
      userSubscription.is_expired ||
      userSubscription.status === SubscriptionUserStatus.CANCELED
    ) {
      return (
        <Typography variant="caption" color="error" sx={{ ml: 1 }}>
          (Expired on {expirationDate.toLocaleDateString()})
        </Typography>
      )
    }

    if (userSubscription.scheduled_downgrade) {
      return (
        <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
          (Expires on {expirationDate.toLocaleDateString()})
        </Typography>
      )
    }

    // Show expiration date for all active paid subscriptions (not FREE tier)
    if (userSubscription.status === SubscriptionUserStatus.SUBSCRIBED) {
      return (
        <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
          (Renew on {expirationDate.toLocaleDateString()})
        </Typography>
      )
    }

    return null
  }

  return (
    <AccountWrapper>
      <DeleteConfirmModal
        titleSubmit="Delete My Account"
        onClose={handleCloseDeleteComfirmModal}
        open={isDeleteConfirmModalOpen}
        onSubmit={onConfirmDelete}
        description={getDeleteAccountDescription()}
        warningItems={getDeleteAccountWarnings()}
        iconType="warning"
        loading={loading}
      />
      <ChangePasswordModal
        onSubmit={onConfirmChangePw}
        open={isChangePwModalOpen}
        onClose={handleCloseChangePw}
      />
      <Title>Account Profile</Title>
      <BoxFlex>
        <TitleData>Name</TitleData>
        {isEditName ? (
          <Input
            sx={{ width: 400 }}
            autoFocus
            onBlur={onBlur}
            placeholder="Name"
            inputProps={{ "aria-label": "Name" }}
            value={isName}
            onChange={onEditName}
            onKeyDown={handleName}
            ref={ref}
          />
        ) : (
          <>
            <Box>{isName ? isName : user?.name}</Box>
            <IconButton
              sx={{ ml: 1 }}
              aria-label="Edit name"
              onClick={() => setIsEditName(true)}
            >
              <Edit />
            </IconButton>
          </>
        )}
      </BoxFlex>
      <BoxFlex>
        <TitleData>Email</TitleData>
        <BoxData>{user?.email}</BoxData>
      </BoxFlex>
      <BoxFlex>
        <TitleData>Data size</TitleData>
        <BoxData>{convertBytes(user?.data_usage || 0)}</BoxData>
      </BoxFlex>
      <BoxFlex>
        <TitleData>Bucket name</TitleData>
        <BoxData>{user?.attributes?.remote_bucket_name || "-"}</BoxData>
      </BoxFlex>
      <BoxFlex>
        <TitleData>Subscription</TitleData>
        <Box
          sx={{
            display: "flex",
            flexDirection: "column",
            alignItems: "flex-start",
          }}
        >
          <BoxData data-testid="account-plan-name">
            {userSubscription?.plan_name && !userSubscription.is_expired
              ? userSubscription.plan_name
              : PlanName.FREE}
          </BoxData>
          {getExpirationInfo()}
        </Box>
        {renderSubscriptionButtons()}
      </BoxFlex>
      <BoxFlex>
        <TitleData sx={{ display: "flex", alignItems: "center" }}>
          Data Deletion Priority
          <Tooltip
            title={
              <Box>
                <Box>
                  Controls which data is kept when expired subscription data is
                  cleaned up.
                </Box>
                <Box component="ul" sx={{ m: 0, mt: 0.5, pl: 2 }}>
                  <li>&quot;Preserve Outputs&quot;: keeps workflow results.</li>
                  <li>
                    &quot;Preserve Inputs&quot;: keeps uploaded source files.
                  </li>
                </Box>
              </Box>
            }
            arrow
          >
            <HelpOutline
              fontSize="small"
              color="action"
              sx={{ ml: 1, cursor: "pointer" }}
            />
          </Tooltip>
        </TitleData>
        {isEditDeletionPriority ? (
          <FormControl size="small" sx={{ minWidth: 200 }}>
            <Select
              value={deletionPriority || DeletionPriority.PRESERVE_OUTPUTS}
              disabled={deletionPriorityLoading}
              autoFocus
              onBlur={() => setIsEditDeletionPriority(false)}
              onChange={async (e) => {
                const result = await dispatch(
                  updateDeletionPriority(e.target.value as string),
                )
                if (isRejectedWithValue(result)) {
                  handleClickVariant(
                    "error",
                    "Failed to update deletion priority",
                  )
                } else {
                  handleClickVariant(
                    "success",
                    "Deletion priority updated successfully",
                  )
                }
                setIsEditDeletionPriority(false)
              }}
            >
              <MenuItem value={DeletionPriority.PRESERVE_OUTPUTS}>
                Preserve Outputs
              </MenuItem>
              <MenuItem value={DeletionPriority.PRESERVE_INPUTS}>
                Preserve Inputs
              </MenuItem>
            </Select>
          </FormControl>
        ) : (
          <>
            <BoxData>
              {(deletionPriority || DeletionPriority.PRESERVE_OUTPUTS) ===
              DeletionPriority.PRESERVE_OUTPUTS
                ? "Preserve Outputs"
                : "Preserve Inputs"}
            </BoxData>
            <IconButton
              sx={{ ml: 1 }}
              aria-label="Edit deletion priority"
              onClick={() => setIsEditDeletionPriority(true)}
              disabled={deletionPriorityLoading}
            >
              <Edit />
            </IconButton>
          </>
        )}
      </BoxFlex>
      {/* ponytail: shown only once a decision exists, so this and the notice cannot disagree without any shared state. */}
      {isGtmEnabled() && analyticsConsent !== null && (
        <BoxFlex>
          <TitleData>Analytics Cookies</TitleData>
          <Switch
            checked={analyticsConsent === "granted"}
            onChange={(e) => {
              const decision = e.target.checked ? "granted" : "denied"
              setAnalyticsConsent(decision)
              setAnalyticsConsentState(decision)
            }}
            inputProps={{ "aria-label": "Allow analytics cookies" }}
          />
        </BoxFlex>
      )}
      <BoxFlex sx={{ justifyContent: "space-between", mt: 10, maxWidth: 600 }}>
        <Button variant="contained" color="primary" onClick={onChangePwClick}>
          Change Password
        </Button>
        <Button
          variant="contained"
          color="error"
          onClick={onDeleteAccountClick}
        >
          Delete Account
        </Button>
      </BoxFlex>
      <Loading loading={loading} />
    </AccountWrapper>
  )
}

const AccountWrapper = styled(Box)({
  padding: "0 20px",
})

const BoxFlex = styled(Box)({
  display: "flex",
  margin: "20px 0 10px 0",
  alignItems: "center",
  maxWidth: 1000,
})

const Title = styled("h2")({
  marginBottom: 40,
})

const BoxData = styled(Typography)({
  fontWeight: 700,
  minWidth: 272,
})

const TitleData = styled(Typography)({
  width: 250,
  minWidth: 250,
})

export default Account
