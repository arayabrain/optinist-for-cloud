import json
from typing import Optional, Tuple

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse
from firebase_admin import auth
from requests.exceptions import HTTPError, RequestException
from sqlmodel import Session

from studio.app.common.core.auth import pyrebase_app
from studio.app.common.core.auth.auth_dependencies import get_admin_user
from studio.app.common.core.auth.security import (
    create_access_token,
    create_refresh_token,
    validate_refresh_token,
)
from studio.app.common.core.logger import AppLogger
from studio.app.common.models.user import User as UserModel
from studio.app.common.schemas.auth import AccessToken, Token, UserAuth
from studio.app.common.schemas.users import User

logger = AppLogger.get_logger()


def _extract_firebase_error(error: RequestException) -> str:
    # The raw error string embeds the request URL with the Firebase API key,
    # so read the error code from the response body instead of logging it.
    bodies = []
    if len(error.args) > 1:
        bodies.append(error.args[1])
    response = getattr(error, "response", None)
    if response is not None:
        bodies.append(response.text)
    bodies.append(getattr(error, "strerror", None))
    for body in bodies:
        try:
            return json.loads(body)["error"]["message"]
        except (KeyError, ValueError, TypeError):
            continue
    return "authentication failed"


async def authenticate_user(db: Session, data: UserAuth) -> Tuple[Token, UserModel]:
    try:
        user = pyrebase_app.auth().sign_in_with_email_and_password(
            data.email, data.password
        )

        user_db: UserModel = (
            db.query(UserModel)
            .filter(UserModel.uid == user["localId"], UserModel.active.is_(True))
            .first()
        )

        try:
            firebase_user = auth.get_user(user["localId"])

            if user_db is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found",
                )

            # Email verification required
            if not firebase_user.email_verified:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Email address is not verified. Please click the "
                    "verification link sent to your email.",
                )

        except auth.UserNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )

        assert user_db is not None, "Invalid user uid"
        ex_token = create_access_token(subject=user_db.uid)
        token = Token(
            access_token=user["idToken"],
            refresh_token=create_refresh_token(subject=user["refreshToken"]),
            token_type="bearer",
            ex_token=ex_token,
        )
        return token, user_db

    except HTTPException:
        raise

    except (HTTPError, AssertionError) as e:
        message = _extract_firebase_error(e) if isinstance(e, HTTPError) else str(e)
        logger.warning(f"Authentication failed: {message}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )


async def refresh_current_user_token(refresh_token: Optional[str]):
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="re-login required",
        )

    token, err = validate_refresh_token(refresh_token)

    if err:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    try:
        user = pyrebase_app.auth().refresh(refresh_token=token["sub"])
        return AccessToken(access_token=user["idToken"])
    except Exception as e:
        message = _extract_firebase_error(e) if isinstance(e, HTTPError) else str(e)
        logger.warning(f"Token refresh failed: {message}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)


async def send_reset_password_mail(db: Session, email: str):
    try:
        db.query(UserModel).filter(
            UserModel.email == email, UserModel.active.is_(True)
        ).one()
        pyrebase_app.auth().send_password_reset_email(email)
        return JSONResponse(content=None, status_code=status.HTTP_200_OK)
    except HTTPError as e:
        message = _extract_firebase_error(e)
        logger.warning(f"Password reset failed: {message}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=message,
        )
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


async def login_with_uid(db: Session, uid: str, current_user: User) -> Token:
    _ = get_admin_user(current_user)
    try:
        user_db = (
            db.query(UserModel)
            .filter(UserModel.uid == uid, UserModel.active.is_(True))
            .first()
        )
        assert user_db is not None, "Invalid user uid"

        token = auth.create_custom_token(uid)
        user = pyrebase_app.auth().sign_in_with_custom_token(token.decode())

        ex_token = create_access_token(uid)
        token = Token(
            access_token=user["idToken"],
            refresh_token=create_refresh_token(subject=user["refreshToken"]),
            token_type="bearer",
            ex_token=ex_token,
        )

        return token

    except (HTTPError, AssertionError) as e:
        message = _extract_firebase_error(e) if isinstance(e, HTTPError) else str(e)
        logger.warning(f"Authentication failed: {message}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=message)

    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        )
