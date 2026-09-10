"""Concurrent workflow counting over real connections.

Opt-in: the cases run only with ``RUN_WORKFLOW_COUNT_IT=1`` and the
application's own database reachable. The per-PR lane collects this
module and skips it, so a missing database never fails or hangs it. Every
import that needs a database is inside a test or fixture body for the same
reason.

``test_workflow_tracking_tier_branch.py::TestConcurrentCountsCannotBeLost``
pins the property that makes the race safe - both counters are computed as
``column +/- 1`` inside the UPDATE - against a mock session. What no mock can
show is that concurrent *connections* serialize on the row, which is what the
two rows claim. Losing an increment is the dangerous direction: the count
reaches zero while a workflow is still running, and the sweep reclaims the
instance or the cleanup job deletes the workspace.
"""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

# Each call opens its own sessions, so this stays well inside the engine pool.
CONCURRENCY = 4

_opt_in = pytest.mark.skipif(
    os.environ.get("RUN_WORKFLOW_COUNT_IT") != "1",
    reason="opt-in L3: set RUN_WORKFLOW_COUNT_IT=1 with a reachable database",
)


def _stored_count(user_id):
    from sqlmodel import select

    from studio.app.common.db.database import session_scope
    from studio.app.common.models.free_user import FreeUserAssignment

    with session_scope() as session:
        return session.execute(
            select(FreeUserAssignment.active_workflow_count).where(
                FreeUserAssignment.user_id == user_id
            )
        ).scalar_one()


def _all_at_once(operation, user_id):
    """Run ``operation(user_id)`` on every thread, released together.

    Results are collected rather than joined: a bare ``Thread`` swallows what
    its target raised, so a database error would reach the reader only as a
    count that came out short, with the cause left in stderr.
    """
    # A thread that dies before the barrier has to fail the lane, not hang it.
    released = threading.Barrier(CONCURRENCY, timeout=60)

    def run():
        released.wait()
        operation(user_id)

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        for result in [pool.submit(run) for _ in range(CONCURRENCY)]:
            result.result()


@pytest.fixture()
def free_user():
    """A throwaway user and its free assignment, removed again afterwards.

    ``active`` is False so nothing else sharing the database can mistake the row
    for a live account; the counter path only needs the user row to exist.
    """
    from studio.app.common.core.mode import MODE
    from studio.app.common.db.database import session_scope
    from studio.app.common.models import User as UserModel
    from studio.app.common.models.free_user import FreeUserAssignment

    address = f"workflow-count-it-{uuid.uuid4().hex}@example.invalid"
    with session_scope() as session:
        user = UserModel(
            organization_id=1,
            uid=address,
            name="workflow count IT",
            email=address,
            attributes={},
            active=False,
        )
        session.add(user)
        session.flush()
        user_id = user.id
        session.add(
            FreeUserAssignment(
                user_id=user_id, instance_id="it-local", active_workflow_count=0
            )
        )

    try:
        # The test session runs standalone, where the counter is a no-op by design
        with patch.object(MODE, "IS_STANDALONE", False):
            yield user_id
    finally:
        with session_scope() as session:
            session.execute(
                FreeUserAssignment.__table__.delete().where(
                    FreeUserAssignment.user_id == user_id
                )
            )
            session.execute(UserModel.__table__.delete().where(UserModel.id == user_id))


@_opt_in
class TestConcurrentWorkflowCountsOverRealConnections:
    def test_no_concurrent_start_is_lost(self, free_user):
        from studio.app.common.core.workflow.workflow_tracking import (
            increment_workflow_count,
        )

        _all_at_once(increment_workflow_count, free_user)

        assert _stored_count(free_user) == CONCURRENCY

    def test_the_count_returns_to_zero_when_they_all_finish(self, free_user):
        from studio.app.common.core.workflow.workflow_tracking import (
            decrement_workflow_count,
            increment_workflow_count,
        )

        _all_at_once(increment_workflow_count, free_user)
        assert _stored_count(free_user) == CONCURRENCY

        _all_at_once(decrement_workflow_count, free_user)

        assert _stored_count(free_user) == 0
