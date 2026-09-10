from datetime import datetime
from typing import Optional

from sqlalchemy import TIMESTAMP
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.sql import func
from sqlalchemy.sql.functions import current_timestamp
from sqlmodel import Column, Field, ForeignKey, SQLModel

from studio.app.common.core.subscription.constants import DeletionPriority  # noqa: F401


class UserPreferences(SQLModel, table=True):
    __tablename__ = "user_preferences"
    __table_args__ = (
        UniqueConstraint("user_id"),
        Index("ix_user_preferences_user_id", "user_id"),
    )

    id: Optional[int] = Field(
        sa_column=Column(
            BIGINT(unsigned=True), primary_key=True, nullable=False, autoincrement=True
        ),
        default=None,
    )
    user_id: int = Field(
        sa_column=Column(
            BIGINT(unsigned=True),
            ForeignKey("users.id", name="fk_user_preferences_user"),
            nullable=False,
        ),
    )
    deletion_priority: Optional[str] = Field(
        sa_column=Column(
            SQLEnum(
                DeletionPriority.PRESERVE_OUTPUTS.value,
                DeletionPriority.PRESERVE_INPUTS.value,
                name="deletion_priority_enum",
                create_type=False,
            ),
            nullable=True,
        ),
        default=None,
    )
    created_at: Optional[datetime] = Field(
        sa_column_kwargs={"server_default": current_timestamp()},
    )
    updated_at: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            server_default=func.current_timestamp(),
            onupdate=func.current_timestamp(),
        ),
    )
