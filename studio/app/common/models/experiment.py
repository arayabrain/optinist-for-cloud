from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from sqlalchemy import TIMESTAMP, Boolean
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Index, Integer, Text, text
from sqlalchemy.dialects.mysql import BIGINT
from sqlalchemy.sql.functions import current_timestamp
from sqlmodel import JSON, Column, DateTime, Field, ForeignKey, Relationship, String

from studio.app.common.models.base import Base, TimestampMixin
from studio.app.common.schemas.dataview import LocalSyncStatus, PublishStatus


class BackgroundTaskStatus(str, Enum):
    """Status of background task."""

    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRYING = "retrying"


class BackgroundTaskType(str, Enum):
    """Type of background task."""

    EXPERIMENT = "experiment"
    WORKSPACE = "workspace"


class ExperimentRecord(Base, TimestampMixin, table=True):
    __tablename__ = "experiment_records"

    __table_args__ = (
        Index("idx_local_sync_status", "local_sync_status"),
        Index("idx_publish_sync_status", "publish_status", "local_sync_status"),
    )

    workspace_id: int = Field(
        sa_column=Column(
            BIGINT(unsigned=True), ForeignKey("workspaces.id"), nullable=False
        ),
    )
    uid: str = Field(sa_column=Column(String(100), nullable=False, index=True))

    name: Optional[str] = Field(sa_column=Column(String(100), nullable=True))

    data_usage: int = Field(
        sa_column=Column(
            BIGINT(unsigned=True), nullable=False, comment="data usage in bytes"
        ),
        default=0,
    )

    input_paths: Optional[List] = Field(default=[], sa_column=Column(JSON))

    thumbnails: Optional[Dict] = Field(default={}, sa_column=Column(JSON))

    success: bool = Field(nullable=False, default=False)

    analyzed_at: Optional[datetime] = Field(sa_column=Column(DateTime(timezone=True)))

    publish_status: int = Field(
        sa_column=Column(
            Integer(),
            nullable=False,
            default=PublishStatus.off.value,
            comment="0: private, 1: public",
        )
    )

    local_sync_status: str = Field(
        sa_column=Column(
            String(20),
            nullable=False,
            default=LocalSyncStatus.synced.value,
            comment="Sync status on local storage: pending, synced, error",
        )
    )

    version: int = Field(
        sa_column=Column(
            Integer(),
            nullable=False,
            default=0,
            comment="Version number for optimistic locking",
        ),
        default=0,
    )

    # Data existence flags — set True on workflow completion, cleared by deletion job
    has_intermediates: bool = Field(
        sa_column=Column(
            Boolean,
            nullable=False,
            default=True,
            server_default="1",
            comment="Whether intermediate data (function subdirs) exists",
        ),
        default=True,
    )
    has_outputs: bool = Field(
        sa_column=Column(
            Boolean,
            nullable=False,
            default=True,
            server_default="1",
            comment="Whether output data (root-level non-YAML) exists",
        ),
        default=True,
    )
    # Note: has_inputs is a workspace-level property stored on each experiment
    # row for query convenience. The deletion job clears it on all experiments
    # in a workspace when workspace inputs are deleted.
    has_inputs: bool = Field(
        sa_column=Column(
            Boolean,
            nullable=False,
            default=True,
            server_default="1",
            comment="Whether input data for this experiment's workspace exists",
        ),
        default=True,
    )
    has_nwb: bool = Field(
        sa_column=Column(
            Boolean,
            nullable=False,
            default=False,
            server_default="0",
            comment="Whether NWB file exists (DB-authoritative, replaces YAML hasNWB)",
        ),
        default=False,
    )

    # Deletion error tracking
    # When S3 deletion succeeds but DB deletion fails, we mark the record
    # so the UI can show an appropriate message instead of a ghost experiment
    deletion_error: Optional[str] = Field(
        sa_column=Column(
            String(255),
            nullable=True,
            default=None,
            comment="Error if deletion partially failed (S3 ok, DB not)",
        ),
        default=None,
    )

    workspace: Optional["Workspace"] = Relationship(  # noqa: F821
        back_populates="experiments"
    )


class BackgroundTask(Base, TimestampMixin, table=True):
    """
    Persistent background task queue.
    Ensures tasks complete even if user logs out.
    Tasks are processed by a background worker independently of user session.
    """

    __tablename__ = "background_tasks"

    __table_args__ = (
        Index("idx_background_tasks_status_created", "status", "created_at"),
    )

    user_id: int = Field(
        sa_column=Column(
            BIGINT(unsigned=True),
            nullable=False,
            index=True,
            comment="User who initiated the task",
        ),
        description="User who initiated the task",
    )
    task_type: str = Field(
        sa_column=Column(
            SQLEnum(
                "experiment",
                "workspace",
                name="background_task_type_enum",
            ),
            nullable=False,
        ),
        description="Type of resource being processed",
    )
    resource_id: str = Field(
        sa_column=Column(
            String(100),
            nullable=False,
            index=True,
            comment="ID of the resource (experiment UID or workspace ID)",
        ),
        description="ID of the resource (experiment UID or workspace ID)",
    )
    workspace_id: Optional[int] = Field(
        sa_column=Column(
            BIGINT(unsigned=True),
            nullable=True,
            comment="Workspace ID for experiment tasks",
        ),
        default=None,
        description="Workspace ID for experiment tasks",
    )
    status: str = Field(
        sa_column=Column(
            SQLEnum(
                "queued",
                "in_progress",
                "completed",
                "failed",
                "retrying",
                name="background_task_status_enum",
            ),
            nullable=False,
            default="queued",
        ),
        default=BackgroundTaskStatus.QUEUED.value,
    )
    retry_count: int = Field(
        sa_column=Column(
            Integer(),
            nullable=False,
            default=0,
            comment="Number of retry attempts",
        ),
        default=0,
        description="Number of retry attempts",
    )
    max_retries: int = Field(
        sa_column=Column(
            Integer(),
            nullable=False,
            default=3,
            comment="Maximum retry attempts before marking as failed",
        ),
        default=3,
        description="Maximum retry attempts before marking as failed",
    )
    error_message: Optional[str] = Field(
        sa_column=Column(
            Text,
            nullable=True,
            comment="Error message if task failed",
        ),
        default=None,
        description="Error message if task failed",
    )
    started_at: Optional[datetime] = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=True,
            comment="When processing started",
        ),
        default=None,
        description="When processing started",
    )
    completed_at: Optional[datetime] = Field(
        sa_column=Column(
            DateTime(timezone=True),
            nullable=True,
            comment="When processing completed",
        ),
        default=None,
        description="When processing completed (success or final failure)",
    )

    # created_at/updated_at from TimestampMixin are nullable; this table's
    # migration created them NOT NULL, so override nullable here to match.
    created_at: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            nullable=False,
            server_default=current_timestamp(),
        ),
    )
    updated_at: Optional[datetime] = Field(
        sa_column=Column(
            TIMESTAMP,
            nullable=False,
            server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
    )
