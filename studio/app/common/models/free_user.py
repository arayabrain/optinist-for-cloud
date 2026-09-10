from datetime import datetime
from typing import Optional

from sqlalchemy import INTEGER, TIMESTAMP, VARCHAR, Index
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.sql.functions import current_timestamp
from sqlmodel import Column, Field, ForeignKey, SQLModel


class FreeUserAssignment(SQLModel, table=True):
    __tablename__ = "free_user_assignments"

    __table_args__ = (
        Index("idx_instance_id", "instance_id"),
        Index("idx_last_activity", "last_activity"),
        Index("idx_logged_out_at", "logged_out_at"),
        Index("idx_active_workflow_count", "active_workflow_count"),
        Index("idx_last_workflow_start", "last_workflow_start"),
        Index("idx_idle_users", "active_workflow_count", "last_activity"),
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
        sa_column=Column(
            BIGINT(unsigned=True),
            ForeignKey("users.id"),
            unique=True,
            nullable=False,
        )
    )
    instance_id: str = Field(sa_column=Column(VARCHAR(20), nullable=False))
    assigned_at: Optional[datetime] = Field(
        sa_column=Column(TIMESTAMP, nullable=False, server_default=current_timestamp())
    )
    last_activity: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            nullable=False,
            server_default=current_timestamp(),
        )
    )
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
    migration_count: int = Field(
        sa_column=Column(
            INTEGER,
            nullable=False,
            server_default="0",
            comment="Number of times user has been migrated between instances",
        ),
        default=0,
    )
    last_migration: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP, nullable=True, comment="Timestamp of last migration event"
        ),
        default=None,
    )
    logged_out_at: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            nullable=True,
            comment="Timestamp when user explicitly logged out",
        ),
        default=None,
    )
