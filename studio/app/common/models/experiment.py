from sqlalchemy.dialects.mysql import BIGINT
from sqlmodel import Column, Field, ForeignKey, String

from studio.app.common.db.database import session_scope
from studio.app.common.models.base import Base, TimestampMixin


class ExperimentRecord(Base, TimestampMixin, table=True):
    __tablename__ = "experiment_records"

    workspace_id: int = Field(
        sa_column=Column(
            BIGINT(unsigned=True), ForeignKey("workspaces.id"), nullable=False
        ),
    )
    uid: str = Field(sa_column=Column(String(100), nullable=False, index=True))
    data_usage: int = Field(
        sa_column=Column(
            BIGINT(unsigned=True), nullable=False, comment="data usage in bytes"
        ),
        default=0,
    )

    @classmethod
    def exists(cls, workspace_id: int, uid: str) -> bool:
        with session_scope() as session:
            return (
                session.query(cls).filter_by(workspace_id=workspace_id, uid=uid).first()
                is not None
            )
