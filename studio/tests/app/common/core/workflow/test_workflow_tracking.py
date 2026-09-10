"""
Unit tests for workflow tracking functionality.

Tests increment/decrement of active_workflow_count for free and premium tier users.
"""

from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.dialects import mysql

from studio.app.common.core.workflow.workflow_tracking import (
    TIER_FREE,
    TIER_PREMIUM,
    decrement_workflow_count,
    get_active_workflow_count,
    increment_workflow_count,
)
from studio.app.common.models import FreeUserAssignment, PremiumUserAssignment


@pytest.fixture
def mock_session():
    """Mock database session with tier detection for free tier user"""
    # Patch MODE.IS_STANDALONE to False so functions don't return early
    with patch("studio.app.common.core.workflow.workflow_tracking.MODE") as mock_mode:
        mock_mode.IS_STANDALONE = False
        # Mock _get_user_tier to return free tier with free record
        with patch(
            "studio.app.common.core.workflow.workflow_tracking._get_user_tier"
        ) as mock_tier:
            mock_tier.return_value = (
                TIER_FREE,
                True,
                False,
            )  # free tier, has free record
            with patch(
                "studio.app.common.core.workflow.workflow_tracking.session_scope"
            ) as mock:
                session = MagicMock()
                mock.return_value.__enter__.return_value = session
                yield session


@pytest.fixture
def mock_session_premium():
    """Mock database session with tier detection for premium tier user"""
    with patch("studio.app.common.core.workflow.workflow_tracking.MODE") as mock_mode:
        mock_mode.IS_STANDALONE = False
        with patch(
            "studio.app.common.core.workflow.workflow_tracking._get_user_tier"
        ) as mock_tier:
            mock_tier.return_value = (
                TIER_PREMIUM,
                False,
                True,
            )  # premium tier, has premium record
            with patch(
                "studio.app.common.core.workflow.workflow_tracking.session_scope"
            ) as mock:
                session = MagicMock()
                mock.return_value.__enter__.return_value = session
                yield session


@pytest.fixture
def mock_session_no_records():
    """Mock database session with tier detection but no records"""
    with patch("studio.app.common.core.workflow.workflow_tracking.MODE") as mock_mode:
        mock_mode.IS_STANDALONE = False
        with patch(
            "studio.app.common.core.workflow.workflow_tracking._get_user_tier"
        ) as mock_tier:
            mock_tier.return_value = (TIER_FREE, False, False)  # free tier, no records
            with patch(
                "studio.app.common.core.workflow.workflow_tracking.session_scope"
            ) as mock:
                session = MagicMock()
                mock.return_value.__enter__.return_value = session
                yield session


def _compiled(session):
    """Return ``[(table_name, sql, params), ...]`` for every executed statement."""
    out = []
    for call in session.execute.call_args_list:
        statement = call.args[0]
        compiled = statement.compile(dialect=mysql.dialect())
        out.append(
            (
                statement.table.name,
                " ".join(str(compiled).split()),
                compiled.params,
            )
        )
    return out


class TestIncrementWorkflowCount:
    """Test workflow count increment"""

    def test_increment_workflow_count_success_free_tier(self, mock_session):
        """Test successful increment of workflow count for free tier user"""
        # Setup mock result for execute()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute.return_value = mock_result

        increment_workflow_count(user_id=123)

        mock_session.commit.assert_called_once()
        table, sql, params = _compiled(mock_session)[0]
        assert table == FreeUserAssignment.__tablename__
        assert "active_workflow_count=(free_user_assignments" in sql
        assert ".active_workflow_count + " in sql
        assert "last_workflow_start=now()" in sql
        assert "WHERE free_user_assignments.user_id = " in sql
        assert params["user_id_1"] == 123

    def test_increment_workflow_count_success_premium_tier(self, mock_session_premium):
        """Test successful increment of workflow count for premium tier user"""
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session_premium.execute.return_value = mock_result

        increment_workflow_count(user_id=123)

        mock_session_premium.commit.assert_called_once()
        table, sql, params = _compiled(mock_session_premium)[0]
        assert table == PremiumUserAssignment.__tablename__
        assert "last_workflow_start=now()" in sql
        assert params["user_id_1"] == 123

    def test_increment_workflow_count_no_assignment(self, mock_session_no_records):
        """Test increment when user has no assignment records"""
        # Setup mock result with rowcount=0 (no rows updated)
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session_no_records.execute.return_value = mock_result

        # Should not raise exception
        increment_workflow_count(user_id=123)

        # execute() should not be called since no records exist
        assert not mock_session_no_records.execute.called

    def test_increment_workflow_count_none_user_id(self, mock_session):
        """Test increment with None user_id.

        The previous assertion was on ``session.exec``, which production never
        calls, so the ``user_id is None`` guard could be deleted and this still
        passed.
        """
        increment_workflow_count(user_id=None)

        mock_session.execute.assert_not_called()
        mock_session.commit.assert_not_called()

    def test_increment_workflow_count_multiple_times(self, mock_session):
        """Test multiple increments"""
        # Setup mock result for execute()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute.return_value = mock_result

        increment_workflow_count(user_id=123)
        increment_workflow_count(user_id=123)

        # Verify execute() was called twice (once per increment)
        assert mock_session.execute.call_count == 2
        assert mock_session.commit.call_count == 2


class TestDecrementWorkflowCount:
    """Test workflow count decrement"""

    def test_decrement_workflow_count_success_free_tier(self, mock_session):
        """Test successful decrement of workflow count for free tier user"""
        # Setup mock result for execute()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute.return_value = mock_result

        decrement_workflow_count(user_id=123)

        mock_session.commit.assert_called_once()
        table, sql, params = _compiled(mock_session)[0]
        assert table == FreeUserAssignment.__tablename__
        assert "last_workflow_end=now()" in sql
        assert "WHERE free_user_assignments.user_id = " in sql
        assert params["user_id_1"] == 123

    def test_decrement_workflow_count_success_premium_tier(self, mock_session_premium):
        """Test successful decrement of workflow count for premium tier user"""
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session_premium.execute.return_value = mock_result

        decrement_workflow_count(user_id=123)

        mock_session_premium.commit.assert_called_once()
        table, sql, params = _compiled(mock_session_premium)[0]
        assert table == PremiumUserAssignment.__tablename__
        assert "last_workflow_end=now()" in sql
        assert params["user_id_1"] == 123

    def test_decrement_workflow_count_never_negative(self, mock_session):
        """Test that count never goes below 0"""
        # Setup mock result for execute()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        mock_session.execute.return_value = mock_result

        decrement_workflow_count(user_id=123)

        mock_session.commit.assert_called_once()
        _, sql, params = _compiled(mock_session)[0]
        assert (
            "active_workflow_count=greatest("
            "%s, free_user_assignments.active_workflow_count - %s)"
        ) in sql
        assert params["greatest_1"] == 0

    def test_decrement_workflow_count_no_assignment(self, mock_session_no_records):
        """Test decrement when user has no assignment records"""
        # Setup mock result with rowcount=0 (no rows updated)
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_session_no_records.execute.return_value = mock_result

        # Should not raise exception
        decrement_workflow_count(user_id=123)

        # execute() should not be called since no records exist
        assert not mock_session_no_records.execute.called

    def test_decrement_workflow_count_none_user_id(self, mock_session):
        """Test decrement with None user_id.

        As above: ``session.exec`` is an auto-created mock production never
        touches, so it could not fail.
        """
        decrement_workflow_count(user_id=None)

        mock_session.execute.assert_not_called()
        mock_session.commit.assert_not_called()


class TestGetActiveWorkflowCount:
    """Test getting active workflow count"""

    def test_get_active_workflow_count_success(self, mock_session):
        """Test successful retrieval of workflow count"""
        mock_assignment = MagicMock()
        mock_assignment.active_workflow_count = 3
        # Mock execute() to return a row-like tuple
        mock_session.execute.return_value.first.return_value = (mock_assignment,)

        count = get_active_workflow_count(user_id=123)

        assert count == 3
        sql = " ".join(
            str(
                mock_session.execute.call_args.args[0].compile(dialect=mysql.dialect())
            ).split()
        )
        assert f"FROM {FreeUserAssignment.__tablename__}" in sql

    def test_get_active_workflow_count_reads_the_premium_table_for_a_premium_user(
        self, mock_session_premium
    ):
        """The premium baseline. Every other case in this class runs on the free
        fixture, so the tier branch inside ``get_active_workflow_count`` was
        unexercised and a premium user's count could have been read from
        ``free_user_assignments`` (always 0) with the suite green."""
        mock_assignment = MagicMock()
        mock_assignment.active_workflow_count = 2
        mock_session_premium.execute.return_value.first.return_value = (
            mock_assignment,
        )

        count = get_active_workflow_count(user_id=123)

        assert count == 2
        sql = " ".join(
            str(
                mock_session_premium.execute.call_args.args[0].compile(
                    dialect=mysql.dialect()
                )
            ).split()
        )
        assert f"FROM {PremiumUserAssignment.__tablename__}" in sql

    def test_get_active_workflow_count_no_assignment(self, mock_session):
        """Test retrieval when user has no assignment"""
        # Mock execute() to return None
        mock_session.execute.return_value.first.return_value = None

        count = get_active_workflow_count(user_id=123)

        assert count == 0

    def test_get_active_workflow_count_none_value(self, mock_session):
        """Test retrieval when count is None"""
        mock_assignment = MagicMock()
        mock_assignment.active_workflow_count = None
        # Mock execute() to return a row-like tuple
        mock_session.execute.return_value.first.return_value = (mock_assignment,)

        count = get_active_workflow_count(user_id=123)

        assert count == 0
