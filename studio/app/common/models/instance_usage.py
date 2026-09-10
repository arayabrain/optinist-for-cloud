from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import TIMESTAMP
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Index
from sqlalchemy.dialects.mysql import BIGINT, VARCHAR
from sqlalchemy.sql.functions import current_timestamp
from sqlmodel import Column, Field, SQLModel


class UsageTier(str, Enum):
    FREE = "free"
    PREMIUM = "premium"


class InstanceUsageLog(SQLModel, table=True):
    __tablename__ = "instance_usage_log"

    __table_args__ = (
        Index("idx_usage_log_user_tier", "user_id", "tier"),
        Index("idx_usage_log_active", "ended_at"),
        Index("idx_usage_log_tier_started", "tier", "started_at"),
    )

    id: Optional[int] = Field(
        sa_column=Column(
            BIGINT(unsigned=True),
            primary_key=True,
            nullable=False,
            autoincrement=True,
        ),
        default=None,
    )
    user_id: int = Field(
        sa_column=Column(BIGINT(unsigned=True), nullable=False),
    )
    instance_id: str = Field(sa_column=Column(VARCHAR(20), nullable=False))
    tier: UsageTier = Field(
        sa_column=Column(
            SQLEnum("free", "premium", name="usage_tier_enum"),
            nullable=False,
        ),
    )
    started_at: Optional[datetime] = Field(
        sa_column=Column(TIMESTAMP, nullable=False, server_default=current_timestamp()),
    )
    ended_at: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            nullable=True,
            comment="NULL means session is still active",
        ),
        default=None,
    )
