import os
from datetime import datetime
from typing import Optional

import sqlalchemy
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import Session

from studio.app.common.core.auth.auth_config import AUTH_CONFIG
from studio.app.common.core.auth.auth_helper import (
    extract_uid_from_firebase_credential,
    extract_uid_from_jwt_token,
)
from studio.app.common.core.dataview.dataview_services import DataviewService
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.mode import MODE
from studio.app.common.core.storage.remote_storage_controller import RemoteStorageType
from studio.app.common.core.subscription.constants import PlanName
from studio.app.common.core.subscription.subscription_service import (
    derive_subscription_status,
)
from studio.app.common.core.utils.datetime_utils import get_current_datetime
from studio.app.common.db.database import get_db
from studio.app.common.models import User as UserModel
from studio.app.common.models import UserRole as UserRoleModel
from studio.app.common.models.subscription import (
    SubscriptionPlans,
    UserStorageUsage,
    UserSubscription,
)
from studio.app.common.schemas.users import User

# Request-scoped cache keys
_REQUEST_USER_CACHE_KEY = "_cached_user_context"
_REQUEST_OUTPUTS_BUCKET_CACHE_KEY = "_cached_outputs_bucket_name"

logger = AppLogger.get_logger()


def _enrich_user_with_basic_attributes(
    user: UserModel,
    role_id: int,
    data_usage: int,
    subscription_plan_name: Optional[str],
    storage_usage_bytes: Optional[int],
    storage_quota_bytes: Optional[int],
) -> None:
    """
    Enrich user object with basic attributes (role, storage, plan name).

    Args:
        user: User model to enrich
        role_id: User's role ID
        data_usage: Total data usage
        subscription_plan_name: Name of subscription plan
        storage_usage_bytes: Storage usage in bytes
        storage_quota_bytes: Storage quota in bytes
    """
    user.__dict__["role_id"] = role_id
    user.__dict__["data_usage"] = data_usage
    user.__dict__["subscription_plan_name"] = (
        subscription_plan_name or PlanName.FREE.value
    )
    user.__dict__["storage_usage_bytes"] = storage_usage_bytes or 0
    user.__dict__["storage_quota_bytes"] = storage_quota_bytes or 0
    user.__dict__["storage_usage_percent"] = round(
        (storage_usage_bytes or 0) / (storage_quota_bytes or 1) * 100, 2
    )


def _enrich_user_with_subscription_status(
    user: UserModel,
    subscription_expiration: Optional[datetime],
    subscription_plan_id: Optional[int],
    subscription_plan_name: Optional[str],
) -> None:
    """
    Calculate and set subscription status and days remaining.

    Args:
        user: User model to enrich
        subscription_expiration: Subscription expiration datetime
        subscription_plan_id: ID of subscription plan
        subscription_plan_name: Name of subscription plan (fallback)
    """
    # One derivation, shared with crud_users: this block existed twice, so the
    # day-truncation bug it used to carry existed twice too.
    status, days_remaining = derive_subscription_status(
        subscription_expiration,
        subscription_plan_id,
        subscription_plan_name,
        get_current_datetime(),
    )
    user.__dict__["subscription_status"] = status
    user.__dict__["subscription_days_remaining"] = days_remaining
    # /users/me reads this path, and used to leave the expiration unset while
    # crud_users set it - so the same account reported an expiration on
    # /api/subsc/mgmts and null here.
    user.__dict__["subscription_expiration"] = subscription_expiration


async def get_current_user_with_dataview_outputs_check(
    req: Request,
    res: Response,
    ex_token: Optional[str] = Depends(APIKeyHeader(name="ExToken", auto_error=False)),
    credential: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=False)),
    db: Session = Depends(get_db),
) -> User:
    """
    Authentication process specialized for outputs requests from the dataview screen
    """

    # Checks whether public outputs are accessed from a dataview
    if DataviewService.is_dataview_public_outputs_request(req):
        is_allowed_access = DataviewService.validate_dataview_public_outputs_request(
            req, db
        )
        # Return the pooled connection now; the route then streams a file and a
        # slow output download must not keep a DB connection checked out.
        db.close()

        if is_allowed_access:
            # To access public resources, skip authentication (return)
            return
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This resource is not publicly available.",
            )

    # Fallback to get_current_user()
    user = await get_current_user(res, req, ex_token, credential, db)
    db.close()
    return user


async def get_current_user_for_dataview_outputs(
    req: Request,
    res: Response,
    ex_token: Optional[str] = Depends(APIKeyHeader(name="ExToken", auto_error=False)),
    credential: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=False)),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    Get current user for dataview outputs, returning None for public requests.

    Unlike get_current_user_with_dataview_outputs_check, this function:
    - Returns None for public dataview requests (for bucket lookup purposes)
    - Returns authenticated user if credentials are provided
    - Does not raise 403 for invalid public requests (just returns None)

    Used by get_outputs_remote_bucket_name to determine which S3 bucket to use.
    """
    # For public dataview requests, return None to indicate no authenticated user
    if DataviewService.is_dataview_public_outputs_request(req):
        # Try to get user if credentials provided, but don't require it
        if credential or ex_token:
            try:
                return await get_current_user(res, req, ex_token, credential, db)
            except HTTPException:
                return None
        return None

    # For non-public requests, require authentication
    return await get_current_user(res, req, ex_token, credential, db)


async def get_current_user(
    res: Response,
    req: Request = None,
    ex_token: Optional[str] = Depends(APIKeyHeader(name="ExToken", auto_error=False)),
    credential: HTTPAuthorizationCredentials = Depends(HTTPBearer(auto_error=False)),
    db: Session = Depends(get_db),
) -> User:
    """
    Get the current authenticated user with request-scoped caching.

    This function caches the user context in the request state to avoid
    redundant database queries when multiple dependencies require user info
    within the same request. Cache is automatically cleared when request ends.

    Performance improvement: ~200x faster for cached hits (DB query avoided).
    """
    # Check for cached user in request state (request-scoped cache)
    if req is not None and hasattr(req.state, _REQUEST_USER_CACHE_KEY):
        return getattr(req.state, _REQUEST_USER_CACHE_KEY)

    use_firebase_auth = AUTH_CONFIG.USE_FIREBASE_TOKEN
    try:
        assert credential is not None if use_firebase_auth else True
        assert ex_token is not None if not use_firebase_auth else True

        # Extract uid using helper functions
        uid = None
        err = None
        if use_firebase_auth:
            uid, err = extract_uid_from_firebase_credential(credential)
        else:
            uid, err = extract_uid_from_jwt_token(ex_token)

        assert err is None, str(err)
        assert uid is not None, "Failed to extract user ID"

        # Query user record
        user_data = __get_current_user_record(db, uid)
        assert user_data is not None, "Invalid user data"
        (
            authed_user,
            role_id,
            data_usage,
            subscription_plan_name,
            storage_usage_bytes,
            storage_quota_bytes,
            subscription_expiration,
            subscription_plan_id,
        ) = user_data

        # Enrich user with basic attributes
        _enrich_user_with_basic_attributes(
            authed_user,
            role_id,
            data_usage,
            subscription_plan_name,
            storage_usage_bytes,
            storage_quota_bytes,
        )

        # Calculate and set subscription status
        _enrich_user_with_subscription_status(
            authed_user,
            subscription_expiration,
            subscription_plan_id,
            subscription_plan_name,
        )

        user = User.from_orm(authed_user)

        # Cache in request state for subsequent calls within this request
        if req is not None:
            setattr(req.state, _REQUEST_USER_CACHE_KEY, user)

        return user

    except ValidationError as e:
        logger.warning(e)
        raise HTTPException(status_code=422, detail=f"Validator Error: {e}")
    except Exception as e:
        logger.warning(e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": 'Bearer realm="auth_required"'},
            detail=str(e) or "Could not validate credentials",
        )


def __get_current_user_record(db: Session, uid: str) -> sqlalchemy.engine.row.Row:
    user_data = (
        db.query(
            UserModel,
            func.min(UserRoleModel.role_id),
            # Use pre-tracked storage_usage_bytes as data_usage
            # (same value, already optimized)
            func.coalesce(UserStorageUsage.storage_usage_bytes, 0).label("data_usage"),
            func.max(SubscriptionPlans.name).label("subscription_plan_name"),
            UserStorageUsage.storage_usage_bytes,
            UserStorageUsage.storage_quota_bytes,
            func.max(UserSubscription.expiration).label("subscription_expiration"),
            func.max(UserSubscription.plan_id).label("subscription_plan_id"),
        )
        .outerjoin(UserRoleModel, UserRoleModel.user_id == UserModel.id)
        .outerjoin(UserSubscription, UserSubscription.user_id == UserModel.id)
        .outerjoin(SubscriptionPlans, SubscriptionPlans.id == UserSubscription.plan_id)
        .outerjoin(UserStorageUsage, UserStorageUsage.user_id == UserModel.id)
        .filter(UserModel.uid == uid, UserModel.active.is_(True))
        .group_by(UserModel.id)
        .first()
    )

    return user_data


async def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if current_user.is_admin:
        return current_user
    else:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient privileges",
        )


async def get_user_remote_bucket_name(
    current_user: User = Depends(get_current_user),
) -> str:
    """
    get user remote_bucket_name from users.attributes
    """
    return _get_user_remote_bucket_name(current_user)


def _get_user_remote_bucket_name(
    current_user: User = None,
) -> str:
    """
    get user remote_bucket_name from users.attributes
    """

    if current_user:
        remote_bucket_name = current_user.remote_bucket_name
    else:
        remote_bucket_name = None

    if not remote_bucket_name:
        remote_storage_type = RemoteStorageType.get_activated_type()

        if MODE.IS_TEST:
            remote_bucket_name = os.environ.get(
                "S3_DEFAULT_BUCKET_NAME", "TEST_DUMMY_BUCKET_NAME"
            )
        elif remote_storage_type == RemoteStorageType.S3:
            remote_bucket_name = os.environ.get("S3_DEFAULT_BUCKET_NAME")
        else:
            remote_bucket_name = "MOCK_DUMMY_BUCKET_NAME"

    assert remote_bucket_name, f"Invalid remote_bucket_name: {remote_bucket_name}"

    return remote_bucket_name


async def get_outputs_remote_bucket_name(
    req: Request,
    current_user: User = Depends(get_current_user_for_dataview_outputs),
    db: Session = Depends(get_db),
) -> str:
    """
    Get remote bucket name for outputs requests.
    Always looks up the workspace owner's bucket based on workspace_id,
    since data is stored in the owner's bucket even for shared workspaces.
    Falls back to current user's bucket if workspace owner can't be determined.

    Security: For authenticated users, verifies they have access to the workspace
    (owner, shared user, or published data) before returning the owner's bucket.

    Uses request-scoped caching to avoid redundant database queries within
    the same request.
    """
    # Check for cached bucket name in request state (request-scoped cache)
    if hasattr(req.state, _REQUEST_OUTPUTS_BUCKET_CACHE_KEY):
        return getattr(req.state, _REQUEST_OUTPUTS_BUCKET_CACHE_KEY)

    bucket_name = await _resolve_outputs_remote_bucket_name(req, current_user, db)
    # Return the pooled connection before download.
    db.close()

    # Cache in request state for subsequent calls within this request
    setattr(req.state, _REQUEST_OUTPUTS_BUCKET_CACHE_KEY, bucket_name)

    return bucket_name


async def _resolve_outputs_remote_bucket_name(
    req: Request,
    current_user: Optional[User],
    db: Session,
) -> str:
    """
    Internal function to resolve the bucket name for outputs requests.
    Called by get_outputs_remote_bucket_name after cache check.
    """
    from sqlmodel import or_

    from studio.app.common.core.experiment.experiment import ExptOutputPathIds
    from studio.app.common.models.experiment import ExperimentRecord
    from studio.app.common.models.workspace import Workspace, WorkspacesShareUser
    from studio.app.common.schemas.dataview import PublishStatus

    request_url_path = req.url.path

    # Extract workspace_id and unique_id from output path using centralized method
    ids = ExptOutputPathIds.from_request_url(
        request_url_path, DataviewService.OUTPUTS_URL_PREFIX
    )
    workspace_id = ids.workspace_id
    unique_id = ids.unique_id

    # Also check query params for workspace_id and unique_id
    query_params = dict(req.query_params)
    if not workspace_id:
        workspace_id = query_params.get("workspace_id")
    if not unique_id:
        unique_id = query_params.get("unique_id")

    if workspace_id:
        workspace = None

        # For authenticated users, first check if they have direct access
        # (owner or shared user)
        if current_user is not None:
            workspace = (
                db.query(Workspace)
                .join(
                    WorkspacesShareUser,
                    WorkspacesShareUser.workspace_id == Workspace.id,
                    isouter=True,
                )
                .filter(
                    Workspace.id == int(workspace_id),
                    Workspace.deleted.is_(False),
                    or_(
                        Workspace.user_id == current_user.id,
                        WorkspacesShareUser.user_id == current_user.id,
                    ),
                )
                .first()
            )

            if workspace:
                logger.debug(
                    f"Outputs: user {current_user.id} has direct access to "
                    f"workspace {workspace_id}"
                )

            # If user doesn't have direct access, check if the data is published
            if workspace is None and unique_id:
                logger.debug(
                    f"Outputs: user {current_user.id} has no direct access to "
                    f"workspace {workspace_id}, checking if {unique_id} is published"
                )
                published_record = (
                    db.query(ExperimentRecord)
                    .filter(
                        ExperimentRecord.workspace_id == int(workspace_id),
                        ExperimentRecord.uid == unique_id,
                        ExperimentRecord.publish_status == PublishStatus.on.value,
                    )
                    .first()
                )
                if published_record:
                    # Data is published, allow access to the workspace bucket
                    logger.info(
                        f"Outputs: experiment {workspace_id}/{unique_id} is published, "
                        f"allowing access for user {current_user.id}"
                    )
                    workspace = (
                        db.query(Workspace)
                        .filter(
                            Workspace.id == int(workspace_id),
                            Workspace.deleted.is_(False),
                        )
                        .first()
                    )
                else:
                    logger.debug(
                        f"Outputs: experiment {workspace_id}/{unique_id} "
                        f"is not published"
                    )
        else:
            # For public requests (current_user is None), just look up the workspace
            # Public access is already validated by
            # get_current_user_for_dataview_outputs
            workspace = (
                db.query(Workspace)
                .filter(
                    Workspace.id == int(workspace_id),
                    Workspace.deleted.is_(False),
                )
                .first()
            )

        if workspace and workspace.user:
            owner_bucket = getattr(workspace.user, "remote_bucket_name", None)
            if owner_bucket:
                logger.debug(
                    f"Outputs: using owner bucket {owner_bucket} "
                    f"for workspace {workspace_id}"
                )
                return owner_bucket

    # Fall back to current user's bucket or default
    if current_user is not None:
        fallback_bucket = _get_user_remote_bucket_name(current_user)
        logger.debug(
            f"Outputs: falling back to user {current_user.id}'s bucket "
            f"{fallback_bucket} for workspace {workspace_id}"
        )
        return fallback_bucket

    fallback_bucket = _get_user_remote_bucket_name(None)
    logger.debug(f"Outputs: falling back to default bucket {fallback_bucket}")
    return fallback_bucket
