"""add unique constraint on subscription_users(user_id)

Revision ID: o901o0716028
Revises: n901n0716027
Create Date: 2026-07-16 12:00:00.000000

"""

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "o901o0716028"
down_revision = "n901n0716027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keep the MAX(expiration) row per user (id as tiebreak): all read paths
    # resolve duplicates by MAX(expiration), so this preserves read-visible state
    op.execute(
        text(
            "DELETE s1 FROM subscription_users s1 "
            "INNER JOIN subscription_users s2 "
            "ON s1.user_id = s2.user_id "
            "AND (s1.expiration < s2.expiration "
            "OR (s1.expiration = s2.expiration AND s1.id < s2.id))"
        )
    )
    op.create_unique_constraint("idx_user_id_unique", "subscription_users", ["user_id"])


def downgrade() -> None:
    op.drop_constraint("idx_user_id_unique", "subscription_users", type_="unique")
