from datetime import datetime
from typing import Optional

from sqlalchemy import INTEGER, TIMESTAMP, VARCHAR, Boolean, Enum, Index
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.sql.functions import current_timestamp
from sqlmodel import Column, Field, ForeignKey, SQLModel


class PremiumUserAssignment(SQLModel, table=True):
    __tablename__ = "premium_user_assignments"

    __table_args__ = (
        Index("idx_instance_id", "instance_id"),
        Index("idx_last_activity", "last_activity"),
        Index("idx_status", "status"),
        Index("idx_instance_state", "instance_state"),
        Index("idx_is_shared", "is_shared"),
        Index("idx_last_state_check", "last_state_check"),
        Index("idx_is_standby", "is_standby"),
        Index("idx_standby_created_at", "standby_created_at"),
        Index("idx_workflow_recovery", "active_workflow_count", "last_workflow_start"),
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
    user_id: Optional[int] = Field(
        sa_column=Column(
            BIGINT(unsigned=True),
            ForeignKey("users.id", name="fk_premium_user"),
            unique=True,
            nullable=True,
        ),
        default=None,
    )
    instance_id: str = Field(sa_column=Column(VARCHAR(20), nullable=False))
    target_group_arn: str = Field(sa_column=Column(VARCHAR(512), nullable=False))
    alb_rule_arn: str = Field(sa_column=Column(VARCHAR(512), nullable=False))
    assigned_at: Optional[datetime] = Field(
        sa_column=Column(TIMESTAMP, nullable=False, server_default=current_timestamp())
    )
    status: str = Field(
        sa_column=Column(
            Enum("active", "migrating", "terminating", name="assignment_status"),
            nullable=False,
            server_default="active",
        ),
        default="active",
    )
    last_activity: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            nullable=False,
            server_default=current_timestamp(),
        )
    )
    instance_state: str = Field(
        sa_column=Column(
            Enum(
                "launching",
                "running",
                "stopping",
                "stopped",
                "terminating",
                name="instance_state",
            ),
            nullable=False,
            server_default="launching",
        ),
        default="launching",
    )
    is_shared: bool = Field(
        sa_column=Column(Boolean, nullable=False, server_default="0"), default=False
    )
    assignment_attempts: int = Field(
        sa_column=Column(INTEGER, nullable=False, server_default="1"), default=1
    )
    last_state_check: Optional[datetime] = Field(
        sa_column=Column(TIMESTAMP, nullable=False, server_default=current_timestamp())
    )
    is_standby: bool = Field(
        sa_column=Column(Boolean, nullable=False, server_default="0"), default=False
    )
    standby_created_at: Optional[datetime] = Field(
        sa_column=Column(TIMESTAMP, nullable=True), default=None
    )

    # Heartbeat tracking - grace period on heartbeat failures
    heartbeat_failures: int = Field(
        sa_column=Column(
            INTEGER,
            nullable=False,
            server_default="0",
            comment="Consecutive heartbeat failures, used for grace period",
        ),
        default=0,
    )

    # Workflow tracking fields
    active_workflow_count: int = Field(
        sa_column=Column(
            INTEGER,
            nullable=False,
            server_default="0",
            comment="Number of active workflows running for this user",
        ),
        default=0,
    )
    last_workflow_start: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP, nullable=True, comment="Timestamp of last workflow start"
        ),
        default=None,
    )
    last_workflow_end: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP, nullable=True, comment="Timestamp of last workflow completion"
        ),
        default=None,
    )
