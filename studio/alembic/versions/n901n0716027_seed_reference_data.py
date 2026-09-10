"""seed reference data required for user registration

Revision ID: n901n0716027
Revises: i901i9230023
Create Date: 2026-07-16 12:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "n901n0716027"
down_revision = "i901i9230023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Registration FK-requires these rows; env-specific overrides (org name,
    # plan prices/Stripe ids) are applied later by their existing seeders
    op.execute(
        "INSERT IGNORE INTO organization (id, name) VALUES (1, 'Default Organization')"
    )
    op.execute(
        "INSERT IGNORE INTO roles (id, role) VALUES "
        "(1, 'admin'), (10, 'data manager'), (20, 'operator'), (30, 'guest operator')"
    )
    op.execute(
        "INSERT IGNORE INTO subscription_plans "
        "(id, name, price, billing_cycle, features, currency, status) "
        "VALUES (1, 'Free', 0, 1, '{}', 1, 1)"
    )


def downgrade() -> None:
    pass  # seed data; nothing to unwind
