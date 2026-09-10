"""
Workflow Count Recovery for Free and Premium Tier Users

UNCALLED AND SUPERSEDED. Nothing in the application imports this module. The
sweep that actually runs is `recover_stale_workflow_counts` in the Common User
Manager Lambda, which replaced this copy and is invoked as step 1 of its handler.

Their rules also differ: this one resets any counter whose workflow started more
than STALE_WORKFLOW_THRESHOLD_MINUTES ago, where the Lambda additionally requires
an inactive heartbeat and evidence the workflow ended, specifically so a
legitimate multi-hour run is not reclaimed underneath the user. Reviving this
version would reintroduce that.

Handles recovery of active_workflow_count in case of process crashes:
1. Detect stale workflow counts (workflows marked as active but actually finished)
2. Reset counts to 0 for users with no running processes
3. Prevent workflow count leaks from process crashes
"""

from datetime import timedelta
from typing import List, Tuple

from sqlmodel import select

from studio.app.common.core.logger import AppLogger
from studio.app.common.core.mode import MODE
from studio.app.common.core.utils.datetime_utils import get_current_datetime
from studio.app.common.db.database import session_scope
from studio.app.common.models import FreeUserAssignment, PremiumUserAssignment

logger = AppLogger.get_logger()

# Consider workflow stale if last_workflow_start is older than this
STALE_WORKFLOW_THRESHOLD_MINUTES = 30


def recover_stale_workflow_counts(
    stale_threshold_minutes: int = STALE_WORKFLOW_THRESHOLD_MINUTES,
) -> Tuple[int, List[int]]:
    """
    Reset active_workflow_count to 0 for users with stale workflows in both
    free and premium tier tables.

    A workflow is considered stale if:
    - active_workflow_count > 0
    - last_workflow_start is older than threshold
    - This indicates a process crash without proper cleanup

    Args:
        stale_threshold_minutes: Minutes after which a workflow is considered stale

    Returns:
        Tuple of (number of users recovered, list of recovered user IDs)
    """
    if MODE.IS_STANDALONE:
        logger.info("Standalone mode - skipping workflow count recovery")
        return 0, []

    try:
        from sqlalchemy import update

        stale_cutoff = get_current_datetime() - timedelta(
            minutes=stale_threshold_minutes
        )
        recovered_users = []

        with session_scope() as session:
            # Recover free tier users
            free_stmt = select(FreeUserAssignment).where(
                FreeUserAssignment.active_workflow_count > 0,
                FreeUserAssignment.last_workflow_start < stale_cutoff,
            )
            stale_free_assignments = session.execute(free_stmt).all()

            for row in stale_free_assignments:
                assignment = row[0]
                update_stmt = (
                    update(FreeUserAssignment)
                    .where(FreeUserAssignment.user_id == assignment.user_id)
                    .values(active_workflow_count=0)
                )
                session.execute(update_stmt)
                recovered_users.append(assignment.user_id)

                logger.warning(
                    f"Recovered stale workflow count for FREE user "
                    f"{assignment.user_id}: count={assignment.active_workflow_count}, "
                    f"last_start={assignment.last_workflow_start}"
                )

            # Recover premium tier users
            premium_stmt = select(PremiumUserAssignment).where(
                PremiumUserAssignment.active_workflow_count > 0,
                PremiumUserAssignment.last_workflow_start < stale_cutoff,
                PremiumUserAssignment.is_standby == False,  # noqa: E712
            )
            stale_premium_assignments = session.execute(premium_stmt).all()

            for row in stale_premium_assignments:
                assignment = row[0]
                update_stmt = (
                    update(PremiumUserAssignment)
                    .where(PremiumUserAssignment.user_id == assignment.user_id)
                    .values(active_workflow_count=0)
                )
                session.execute(update_stmt)
                # Don't double-count if user is in both tables (shouldn't happen)
                if assignment.user_id not in recovered_users:
                    recovered_users.append(assignment.user_id)

                logger.warning(
                    f"Recovered stale workflow count for PREMIUM user "
                    f"{assignment.user_id}: count={assignment.active_workflow_count}, "
                    f"last_start={assignment.last_workflow_start}"
                )

            session.commit()

            if not recovered_users:
                logger.info("No stale workflow counts found")
            else:
                logger.info(
                    f"Recovered {len(recovered_users)} users with stale workflow counts"
                )

            return len(recovered_users), recovered_users

    except Exception as e:
        logger.error(f"Failed to recover stale workflow counts: {e}", exc_info=True)
        return 0, []
