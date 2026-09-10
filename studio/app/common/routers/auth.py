from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session

from studio.app.common.core.auth import auth
from studio.app.common.core.auth.auth_dependencies import get_admin_user
from studio.app.common.core.cloud.cloud_utils import (
    calculate_limit_warning,
    ensure_user_bucket_exists,
)
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.middleware.user_activity_middleware import (
    clear_free_user_logged_out_at,
    clear_logged_out_status,
)
from studio.app.common.db.database import get_db
from studio.app.common.schemas.auth import AccessToken, RefreshToken, Token, UserAuth

router = APIRouter(prefix="/auth", tags=["auth"])

logger = AppLogger.get_logger()


@router.post("/login", response_model=Token)
async def login(user_data: UserAuth, db: Session = Depends(get_db)):
    try:
        token, user = await auth.authenticate_user(db, user_data)

        # Clear logged_out_at for free users to prevent cleanup job from
        # deleting their data after re-login
        try:
            clear_logged_out_status(user.id)
            clear_free_user_logged_out_at(user.id)
        except Exception as e:
            logger.warning(f"Failed to clear logout status for user {user.id}: {e}")

        # Ensure user's S3 bucket exists on sign-in
        try:
            bucket = await ensure_user_bucket_exists(user.id)
            if bucket:
                logger.info(f"Bucket recovery on login for user {user.id}: {bucket}")
        except Exception as bucket_error:
            logger.warning(f"Bucket check failed for user {user.id}: {bucket_error}")

        # Check for limit warnings after successful login
        try:
            limit_warning = await calculate_limit_warning(user.id)
            if limit_warning:
                logger.warning(
                    f"User {user.id} ({user.email}) has limit warning: "
                    f"{limit_warning.alert_type} - "
                    f"{limit_warning.days_remaining} days remaining, "
                    f"{limit_warning.excess_data_gb} GB over limit"
                )
            else:
                logger.debug(f"No limit warning for user {user.id}")
        except Exception as warning_error:
            # Don't fail login due to warning check failure
            logger.warning(
                f"Failed to check limit warning for user " f"{user.id}: {warning_error}"
            )

    except HTTPException:
        # The auth layer already logged the cause; a 4xx (e.g. a mistyped
        # password) is routine, so re-raise without a second ERROR traceback.
        raise

    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Some error occurred during authentication.",
        )

    return token


@router.post("/refresh", response_model=AccessToken)
async def refresh(refresh_token: RefreshToken):
    return await auth.refresh_current_user_token(refresh_token.refresh_token)


@router.post("/send_reset_password_mail")
async def send_reset_password_mail(email: str, db: Session = Depends(get_db)):
    return await auth.send_reset_password_mail(db, email)


@router.post("/proxy-login/{uid}", response_model=Token)
async def login_with_uid(
    uid: str, admin_user=Depends(get_admin_user), db: Session = Depends(get_db)
):
    try:
        token = await auth.login_with_uid(db, uid, admin_user)
    except HTTPException:
        # The auth layer already logged the cause; a 4xx (e.g. a mistyped
        # password) is routine, so re-raise without a second ERROR traceback.
        raise

    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Some error occurred during authentication.",
        )

    return token
