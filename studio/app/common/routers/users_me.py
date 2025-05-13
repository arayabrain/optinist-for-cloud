from fastapi import APIRouter, Depends, HTTPException
from firebase_admin import auth as firebase_auth
from nbstripout import status
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from studio.app.common.core.auth.auth_dependencies import get_current_user
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.users import crud_users
from studio.app.common.core.workspace.workspace_services import WorkspaceService
from studio.app.common.db.database import get_db
from studio.app.common.models.workspace import Workspace
from studio.app.common.schemas.users import SelfUserUpdate, User, UserPasswordUpdate

router = APIRouter(prefix="/users/me", tags=["users/me"])
logger = AppLogger.get_logger()


@router.get("", response_model=User)
async def me(current_user: User = Depends(get_current_user)):
    return current_user


@router.put("", response_model=User)
async def update_me(
    data: SelfUserUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await crud_users.update_user(
        db, current_user.id, data, organization_id=current_user.organization.id
    )


@router.put("/password", response_model=bool)
async def update_password(
    data: UserPasswordUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return await crud_users.update_password(
        db, current_user.id, data, organization_id=current_user.organization.id
    )


@router.delete("", response_model=bool)
async def delete_me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        workspace_ids = [
            ws.id
            for ws in db.query(Workspace)
            .filter(Workspace.user_id == current_user.id)
            .all()
        ]

        for workspace_id in workspace_ids:
            WorkspaceService.delete_workspace_experiment_by_workspace_id(
                db, workspace_id
            )

        WorkspaceService.delete_workspace_data_by_user_id(db, current_user.id)

        user_uid = await crud_users.delete_user(
            db, current_user.id, organization_id=current_user.organization.id
        )

        db.commit()

        for workspace_id in workspace_ids:
            WorkspaceService.delete_workspace_data(workspace_id=workspace_id, db=db)

        firebase_auth.delete_user(user_uid)

        return True

    except SQLAlchemyError as db_err:
        db.rollback()
        logger.error("DB error during user self-deletion: %s", db_err, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not delete your account from the database.",
        )
    except Exception as e:
        db.rollback()
        logger.error("Error during user self-deletion: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An error occurred while deleting your account.",
        )
