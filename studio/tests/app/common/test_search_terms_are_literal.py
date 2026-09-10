"""A search term is matched literally, not as a `LIKE` pattern.

`%` and `_` are wildcards in SQL `LIKE`, so an unescaped term returns rows that do
not contain what the user typed: `a_b` also matches `axb`, and a bare `%` matches
everything. The values were always bound parameters, so this was never injectable
- it was wrong results.

Every search filter in the application was the same one-line idiom, so they are
covered together here rather than in each router's own file: the mistake belongs
to the idiom, not to any one caller. The admin user list is the exception - its
filters are asserted through the route in `test_users_admin.py`, where the
pagination harness already exists.
"""

import pathlib
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from studio.app.common.models import Organization as OrganizationModel
from studio.app.common.models import User as UserModel
from studio.app.common.models import Workspace as WorkspaceModel
from studio.app.common.models.experiment import ExperimentRecord
from studio.app.common.routers.dataview import get_records_filtered_query
from studio.app.common.routers.users_search import search_share_users
from studio.app.common.schemas.dataview import DataviewRecordSearchOptions
from studio.tests.app.common.sqlite_harness import sqlite_session

ORGANIZATION_ID = 1

# The pair every case turns on: searching "a_b" must find only the first. With `_`
# left as a wildcard, both come back.
LITERAL = "a_b"
WILDCARD_MATCH = "axb"


@pytest.fixture()
def db():
    with sqlite_session(
        [
            OrganizationModel.__table__,
            UserModel.__table__,
            WorkspaceModel.__table__,
            ExperimentRecord.__table__,
        ]
    ) as session:
        session.add(OrganizationModel(id=ORGANIZATION_ID, name="Test Org"))
        session.commit()
        yield session


def seed_user(db, name, email):
    user = UserModel(
        organization_id=ORGANIZATION_ID,
        uid=f"uid-{email}",
        name=name,
        email=email,
        attributes={},
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user.id


def seed_record(db, user_id, workspace_name, uid, record_name):
    workspace = WorkspaceModel(
        user_id=user_id, name=workspace_name, deleted=False, input_data_usage=0
    )
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    db.add(
        ExperimentRecord(
            workspace_id=workspace.id,
            uid=uid,
            name=record_name,
            data_usage=0,
            success=True,
            analyzed_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


class TestTheShareUserSearch:
    """The route function itself, called directly: it takes its keyword as a plain
    argument, so no client is needed."""

    @pytest.fixture()
    def caller(self, db):
        """A `User` schema standing in for the signed-in user, which the route uses
        only to scope the query to their organization."""
        from types import SimpleNamespace

        seed_user(db, "Caller", "caller@example.com")
        return SimpleNamespace(organization=SimpleNamespace(id=ORGANIZATION_ID))

    def names_found(self, db, caller, keyword):
        found = search_share_users(keyword=keyword, db=db, current_user=caller)
        return sorted(user.name for user in found)

    def test_an_underscore_is_not_a_wildcard(self, db, caller):
        seed_user(db, LITERAL, f"{LITERAL}@example.com")
        seed_user(db, WILDCARD_MATCH, f"{WILDCARD_MATCH}@example.com")

        assert self.names_found(db, caller, LITERAL) == [LITERAL]

    def test_an_underscore_in_the_address_is_not_a_wildcard_either(self, db, caller):
        """One keyword is matched against both columns, so both need escaping."""
        seed_user(db, "Literal", f"{LITERAL}@example.com")
        seed_user(db, "Wildcard", f"{WILDCARD_MATCH}@example.com")

        assert self.names_found(db, caller, LITERAL) == ["Literal"]

    def test_a_percent_does_not_return_the_whole_directory(self, db, caller):
        """This endpoint feeds the workspace-sharing picker, so a stray `%` listing
        every address in the organization is a disclosure, not just noise."""
        seed_user(db, "Alice", "alice@example.com")
        seed_user(db, "Bob", "bob@example.com")

        assert self.names_found(db, caller, "%") == []

    def test_a_plain_fragment_still_matches_as_a_substring(self, db, caller):
        """The positive control: without it, every empty result above could be a
        filter that has stopped matching anything at all."""
        seed_user(db, "Alice", "alice@example.com")

        assert self.names_found(db, caller, "lic") == ["Alice"]


class TestTheDataviewRecordSearch:
    """`get_records_filtered_query`, behind the Dataview search fields, run over
    the router's own joins so the user_name and workspace_name filters resolve the
    way they do in production."""

    def uids_found(self, db, **options):
        query = get_records_filtered_query(
            _base_query(), DataviewRecordSearchOptions(**options)
        )
        return sorted(record.uid for record in db.exec(query).all())

    @pytest.fixture(autouse=True)
    def seeded(self, db):
        literal_user = seed_user(db, LITERAL, f"{LITERAL}@example.com")
        wildcard_user = seed_user(db, WILDCARD_MATCH, f"{WILDCARD_MATCH}@example.com")
        seed_record(db, literal_user, LITERAL, LITERAL, LITERAL)
        seed_record(db, wildcard_user, WILDCARD_MATCH, WILDCARD_MATCH, WILDCARD_MATCH)

    SEARCHABLE_FIELDS = ["uid", "name", "user_name", "workspace_name"]

    @pytest.mark.parametrize("field", SEARCHABLE_FIELDS)
    def test_an_underscore_is_not_a_wildcard(self, db, field):
        assert self.uids_found(db, **{field: LITERAL}) == [LITERAL]

    @pytest.mark.parametrize("field", SEARCHABLE_FIELDS)
    def test_a_percent_matches_nothing_rather_than_every_record(self, db, field):
        assert self.uids_found(db, **{field: "%"}) == []

    @pytest.mark.parametrize("field", SEARCHABLE_FIELDS)
    def test_a_plain_fragment_still_matches_as_a_substring(self, db, field):
        """The positive control for each field, so an empty result above cannot be
        a filter that matches nothing at all."""
        assert self.uids_found(db, **{field: "_b"}) == [LITERAL]

    def test_every_searchable_field_is_covered_here(self):
        """Otherwise a field added to the schema later is unescaped and unnoticed."""
        searchable = {
            name
            for name, field in DataviewRecordSearchOptions.__fields__.items()
            if field.outer_type_ is str
        }

        assert searchable == set(self.SEARCHABLE_FIELDS)


def _base_query():
    """The router's base query without its sort, which needs no escaping."""
    return (
        select(ExperimentRecord)
        .join(
            WorkspaceModel,
            WorkspaceModel.id == ExperimentRecord.workspace_id,
        )
        .join(UserModel, UserModel.id == WorkspaceModel.user_id)
        .filter(
            WorkspaceModel.deleted.is_(False),
            UserModel.active.is_(True),
            ExperimentRecord.success.is_(True),
        )
    )


def test_no_search_filter_builds_a_like_pattern_by_hand():
    """The regression is the idiom rather than any one caller, and a new
    `.like("%" + value + "%")` reintroduces it wherever it is written. Escaping is
    what `contains(..., autoescape=True)` is for."""
    app_root = pathlib.Path(__file__).parents[4] / "studio" / "app"
    offenders = [
        f"{path.relative_to(app_root)}:{number}"
        for path in app_root.rglob("*.py")
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if ".like(" in line and "%" in line
    ]

    assert offenders == []
