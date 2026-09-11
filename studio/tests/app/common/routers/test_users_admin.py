"""The admin Account Manager: who may reach it, and what its writes leave behind.

Two harness notes:

- The routers run against a real SQLite session rather than a `Mock(spec=Session)`,
  so "the role row was replaced, not added" and "nothing was written" are
  observable.
- The session conftest stubs `get_current_user` and `get_admin_user` out for the
  whole run, so a request would succeed even with the gate removed. The gating
  tests restore the real dependency and put a non-admin behind it.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from firebase_admin.auth import EmailAlreadyExistsError
from pydantic import ValidationError

from studio.__main_unit__ import app
from studio.app.common.core.auth.auth_dependencies import (
    get_admin_user,
    get_current_user,
)
from studio.app.common.core.subscription.constants import SubscriptionPlanIds
from studio.app.common.core.users import crud_users
from studio.app.common.db.database import get_db
from studio.app.common.models import Organization as OrganizationModel
from studio.app.common.models import Role as RoleModel
from studio.app.common.models import User as UserModel
from studio.app.common.models import UserRole as UserRoleModel
from studio.app.common.models.subscription import (
    SubscriptionPlans,
    SubscriptionUserAccount,
    SubscriptionUserPurchase,
    UserStorageUsage,
    UserSubscription,
)
from studio.app.common.routers import users_admin, users_me
from studio.app.common.schemas.users import User, UserCreate, UserRole, UserUpdate
from studio.tests.app.common.sqlite_harness import sqlite_session

GB = 1024 * 1024 * 1024
ORGANIZATION_ID = 1
STRONG_PASSWORD = "e2ePass!1"

# Hand-copied from the password rule's own error message. Deriving it from
# `password_regex` would make the test track the rule instead of checking it.
ALLOWED_SPECIALS = "!#$%&()*+,-./@_|"


def plan(plan_id, name, price):
    return SubscriptionPlans(
        id=plan_id,
        name=name,
        price=price,
        billing_cycle=1,
        features={},
        status=True,
        currency=1,
    )


@pytest.fixture()
def db():
    """In-memory SQLite session holding the tables the admin routers touch.

    The plan rows are seeded because the list query joins them for the plan
    name; the roles because `set_role` writes a `user_roles` link to them.
    """
    with sqlite_session(
        [
            OrganizationModel.__table__,
            RoleModel.__table__,
            UserModel.__table__,
            UserRoleModel.__table__,
            SubscriptionPlans.__table__,
            UserSubscription.__table__,
            UserStorageUsage.__table__,
            SubscriptionUserAccount.__table__,
            SubscriptionUserPurchase.__table__,
        ]
    ) as session:
        session.add(OrganizationModel(id=ORGANIZATION_ID, name="Test Org"))
        session.add(RoleModel(id=UserRole.admin.value, role="admin"))
        session.add(RoleModel(id=UserRole.operator.value, role="operator"))
        session.add(plan(SubscriptionPlanIds.FREE, "Free", 0))
        session.add(plan(SubscriptionPlanIds.PREMIUM, "Premium", 5000))
        session.commit()
        yield session


def seed_user(
    db,
    uid,
    email,
    role_id=UserRole.operator.value,
    name=None,
    active=True,
    bucket=None,
):
    """A `users` row plus its `user_roles` link. Returns the id."""
    user = UserModel(
        organization_id=ORGANIZATION_ID,
        uid=uid,
        name=name or uid,
        email=email,
        attributes={"remote_bucket_name": bucket} if bucket else {},
        active=active,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    db.add(UserRoleModel(user_id=user.id, role_id=role_id))
    db.commit()
    return user.id


def seed_subscription(db, user_id, plan_id, expires_in_days, usage=0, quota=5 * GB):
    db.add(
        UserSubscription(
            user_id=user_id,
            plan_id=plan_id,
            expiration=datetime.now(timezone.utc) + timedelta(days=expires_in_days),
        )
    )
    db.add(
        UserStorageUsage(
            user_id=user_id,
            storage_usage_bytes=usage,
            storage_quota_bytes=quota,
        )
    )
    db.commit()


def user_schema(db, user_id) -> User:
    """The `User` the auth dependencies hand a router, built the same way
    `crud_users.get_user` builds it: role_id read from `user_roles`."""
    return asyncio.run(crud_users.get_user(db, user_id, ORGANIZATION_ID))


def _has_router_level_gate(route) -> bool:
    """Whether the admin gate is declared on the router rather than as a route
    parameter. FastAPI gives a parameter dependency the parameter's name, and a
    dependency from `include_router(dependencies=...)` a name of None, so the two
    are distinguishable - and only the router-level one survives a route dropping
    its `current_admin` argument.
    """
    return any(
        sub.call is get_admin_user and sub.name is None
        for sub in route.dependant.dependencies
    )


def role_ids_of(db, user_id):
    return [
        row.role_id
        for row in db.query(UserRoleModel)
        .filter(UserRoleModel.user_id == user_id)
        .all()
    ]


@pytest.fixture()
def admin_id(db):
    return seed_user(db, "uid-admin", "admin@example.com", UserRole.admin.value)


@pytest.fixture()
def admin(db, admin_id) -> User:
    return user_schema(db, admin_id)


@pytest.fixture(autouse=True)
def restore_overrides():
    """`dependency_overrides` is state on the shared `app`."""
    original = app.dependency_overrides.copy()
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(original)


@pytest.fixture()
def client(db, admin):
    """A client whose requests arrive as the seeded admin, against `db`.

    Entered as a context manager, not merely constructed: the list route returns
    a `LimitOffsetPage`, and without the app's lifespan the pagination params are
    never resolved (`RuntimeError: Use params or add_pagination`).
    """
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_admin_user] = lambda: admin
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def no_firebase():
    """Stub the Firebase Admin SDK at the boundary `crud_users` calls it."""
    with patch.object(crud_users, "firebase_auth") as firebase, patch.object(
        crud_users, "RemoteStorageController"
    ) as storage:
        storage.is_available.return_value = False
        firebase.create_user.side_effect = lambda **kwargs: MagicMock(
            uid=f"fb-{kwargs['email']}", email=kwargs["email"]
        )
        yield firebase


def create(db, no_firebase, email, role_id, name="New User", verified=True):
    return asyncio.run(
        crud_users.create_user(
            db,
            UserCreate(
                email=email, password=STRONG_PASSWORD, name=name, role_id=role_id
            ),
            organization_id=ORGANIZATION_ID,
            verified=verified,
        )
    )


class TestOnlyAnAdminReachesTheAccountManager:
    """The gate is not where it looks like it is.

    Each route takes `current_admin: User = Depends(get_admin_user)`, but that
    parameter is how the route learns who is calling; the enforcement is the
    `dependencies=[Depends(get_admin_user)]` on the router's `include_router`.
    Either one alone answers 403, so deleting one route's parameter changes no
    behaviour, and a per-route assertion cannot catch it. The requests below
    therefore assert the behaviour on every path, and the declaration test
    asserts the router-level dependency specifically.
    """

    # `{user_id}` resolves to an id that does not exist, so every write stops at
    # its own "user not found" rather than mutating anything. Bodies are good
    # enough to get past FastAPI's validation, so a 403 is the gate answering
    # rather than a 422.
    ABSENT_USER_ID = 999_999
    REQUESTS = [
        ("GET", "/admin/users", None),
        (
            "POST",
            "/admin/users",
            {
                "email": "gated@example.com",
                "password": STRONG_PASSWORD,
                "name": "Gated",
                "role_id": UserRole.operator.value,
            },
        ),
        ("GET", "/admin/users/{user_id}", None),
        (
            "PUT",
            "/admin/users/{user_id}",
            {
                "email": "gated@example.com",
                "name": "Gated",
                "role_id": UserRole.operator.value,
            },
        ),
        (
            "PUT",
            "/admin/users/{user_id}/subscription",
            {"plan_id": 1, "storage_quota_bytes": GB, "reason": "test"},
        ),
        ("DELETE", "/admin/users/{user_id}", None),
    ]

    def test_every_route_on_the_router_is_covered_here(self):
        """Otherwise a route added later is unexercised and unnoticed."""
        declared = {
            (method, route.path)
            for route in users_admin.router.routes
            for method in route.methods
        }
        assert declared == {(method, path) for method, path, _ in self.REQUESTS}

    def test_every_mounted_admin_route_resolves_the_admin_gate(self):
        """Asserted against the mounted app rather than one router, so an
        `/admin` route mounted from somewhere else - or this router re-included
        without its `dependencies` - is caught. A request could not show either:
        the session conftest stubs the auth dependencies out for the whole run,
        and the per-route `current_admin` parameter answers 403 by itself.
        """
        admin_routes = [
            route
            for route in app.routes
            if getattr(route, "path", "").startswith("/admin")
        ]
        assert admin_routes, "no /admin routes are mounted; this test proves nothing"

        for route in admin_routes:
            assert _has_router_level_gate(
                route
            ), f"{route.path} has no router-level admin gate"

    @pytest.mark.parametrize("method,path,body", REQUESTS)
    def test_a_non_admin_is_refused(self, db, admin_id, method, path, body):
        operator_id = seed_user(db, "uid-op", "op@example.com")
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides.pop(get_admin_user, None)
        app.dependency_overrides[get_current_user] = lambda: user_schema(
            db, operator_id
        )

        with TestClient(app) as client:
            response = client.request(
                method, path.format(user_id=self.ABSENT_USER_ID), json=body
            )

        assert response.status_code == 403
        assert response.json()["detail"] == "Insufficient privileges"

    @pytest.mark.parametrize("method,path,body", REQUESTS)
    def test_the_same_request_as_an_admin_is_not_refused(
        self, db, admin_id, no_firebase, method, path, body
    ):
        """The positive control. Without it every 403 above could be a route
        that is unreachable for some other reason, and the parametrisation would
        keep passing after the gate was removed."""
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides.pop(get_admin_user, None)
        app.dependency_overrides[get_current_user] = lambda: user_schema(db, admin_id)

        with TestClient(app) as client:
            response = client.request(
                method, path.format(user_id=self.ABSENT_USER_ID), json=body
            )

        # 404 for the absent user is fine here; 403 is not.
        assert response.status_code != 403


class TestADemotedAdminLosesAccess:
    """The role is not carried in the token: it is re-read per request, which is
    what makes a demotion take effect on the next one."""

    def test_the_role_is_read_from_the_database_each_time(self, db, admin_id):
        assert user_schema(db, admin_id).is_admin is True

        asyncio.run(crud_users.set_role(db, admin_id, UserRole.operator.value))

        assert user_schema(db, admin_id).is_admin is False

    def test_the_gate_refuses_the_demoted_user(self, db, admin_id):
        asyncio.run(crud_users.set_role(db, admin_id, UserRole.operator.value))
        demoted = user_schema(db, admin_id)

        with pytest.raises(HTTPException) as raised:
            asyncio.run(get_admin_user(current_user=demoted))

        assert raised.value.status_code == 403

    def test_the_gate_admits_an_admin(self, db, admin_id):
        admitted = asyncio.run(get_admin_user(current_user=user_schema(db, admin_id)))

        assert admitted.id == admin_id


class TestUserListColumns:
    """Every column the Account Manager grid renders, and the derived values
    behind them.

    The subscription-status ladder and the grace window itself are pinned against
    `get_user_with_context`; what is asserted here is that the list query wires
    the same derivation up, over a query shape (SQL aggregates) the single-user
    path does not use.
    """

    def test_the_list_carries_every_column_the_grid_renders(self, db, client, admin_id):
        listed_id = seed_user(
            db,
            "uid-listed",
            "listed@example.com",
            UserRole.operator.value,
            name="Listed User",
            bucket="bucket-listed",
        )
        seed_subscription(
            db, listed_id, SubscriptionPlanIds.FREE, 30, usage=2 * GB, quota=5 * GB
        )

        response = client.get("/admin/users?limit=50&offset=0")

        assert response.status_code == 200
        row = next(item for item in response.json()["items"] if item["id"] == listed_id)
        assert row["name"] == "Listed User"
        assert row["email"] == "listed@example.com"
        assert row["role_id"] == UserRole.operator.value
        assert row["data_usage"] == 2 * GB
        assert row["subscription_status"] == "Free"
        assert row["storage_usage_bytes"] == 2 * GB
        assert row["storage_quota_bytes"] == 5 * GB
        assert row["attributes"]["remote_bucket_name"] == "bucket-listed"

    @pytest.mark.parametrize(
        "plan_id,expires_in_days,expected_status",
        [
            (SubscriptionPlanIds.FREE, 30, "Free"),
            (SubscriptionPlanIds.PREMIUM, 12, "Premium"),
            # A plan_id read without its expiration would answer "Premium" here
            (SubscriptionPlanIds.PREMIUM, -1, "Limit Grace"),
        ],
    )
    def test_the_subscription_column_is_derived_from_plan_and_expiration(
        self, db, client, admin_id, plan_id, expires_in_days, expected_status
    ):
        user_id = seed_user(db, "uid-sub", "sub@example.com")
        seed_subscription(db, user_id, plan_id, expires_in_days)

        response = client.get("/admin/users?limit=50&offset=0")

        row = next(item for item in response.json()["items"] if item["id"] == user_id)
        assert row["subscription_status"] == expected_status

    def test_premium_reports_the_days_remaining(self, db, client, admin_id):
        user_id = seed_user(db, "uid-days", "days@example.com")
        seed_subscription(db, user_id, SubscriptionPlanIds.PREMIUM, 12)

        response = client.get("/admin/users?limit=50&offset=0")

        row = next(item for item in response.json()["items"] if item["id"] == user_id)
        # Days remaining rounds up, so an expiration 12 days out reads as 12.
        # It used to truncate to 11, which also made "expires later today" read
        # as 0 and tipped a paying user into the grace branch.
        assert row["subscription_days_remaining"] == 12

    @pytest.mark.parametrize(
        "usage,quota,expected_percent",
        [
            # Quotas other than the 5GB free default, so a divisor hardcoded to
            # any one plan's quota cannot satisfy both cases
            (1 * GB, 4 * GB, 25.0),
            (30 * GB, 200 * GB, 15.0),
        ],
    )
    def test_the_storage_percentage_is_usage_over_that_users_quota(
        self, db, client, admin_id, usage, quota, expected_percent
    ):
        user_id = seed_user(db, "uid-storage", "storage@example.com")
        seed_subscription(
            db, user_id, SubscriptionPlanIds.FREE, 30, usage=usage, quota=quota
        )

        response = client.get("/admin/users?limit=50&offset=0")

        row = next(item for item in response.json()["items"] if item["id"] == user_id)
        assert row["storage_usage_percent"] == expected_percent

    def test_a_user_with_no_storage_row_reports_zero_rather_than_failing(
        self, db, client, admin_id
    ):
        """Every admin-created user gets a storage row, but a user predating the
        feature does not, and the list must still render."""
        user_id = seed_user(db, "uid-bare", "bare@example.com")

        response = client.get("/admin/users?limit=50&offset=0")

        row = next(item for item in response.json()["items"] if item["id"] == user_id)
        assert row["storage_usage_bytes"] == 0
        assert row["storage_usage_percent"] == 0.0
        assert row["subscription_status"] == "Free"

    def test_a_deleted_user_is_not_listed(self, db, client, admin_id):
        inactive_id = seed_user(db, "uid-gone", "gone@example.com", active=False)

        response = client.get("/admin/users?limit=50&offset=0")

        assert inactive_id not in [item["id"] for item in response.json()["items"]]


class TestUserListSearch:
    """The grid's own name/email filter boxes, which reach the route as query
    parameters."""

    @pytest.fixture()
    def seeded(self, db, admin_id):
        seed_user(db, "uid-alice", "alice@example.com", name="Alice Smith")
        seed_user(db, "uid-bob", "bob@other.com", name="Bob Jones")
        return db

    def names_from(self, response):
        return {item["name"] for item in response.json()["items"]}

    def test_a_name_fragment_narrows_the_list(self, seeded, client):
        response = client.get("/admin/users?name=Alice&limit=50&offset=0")

        assert self.names_from(response) == {"Alice Smith"}

    def test_an_email_fragment_narrows_the_list(self, seeded, client):
        response = client.get("/admin/users?email=other.com&limit=50&offset=0")

        assert self.names_from(response) == {"Bob Jones"}

    def test_the_two_filters_intersect_rather_than_union(self, seeded, client):
        response = client.get(
            "/admin/users?name=Alice&email=other.com&limit=50&offset=0"
        )

        assert response.json()["items"] == []

    def test_a_fragment_matching_nothing_is_empty_rather_than_everything(
        self, seeded, client
    ):
        """An unset filter matches every row, so a filter that fell through to the
        default would answer with the whole list."""
        response = client.get("/admin/users?name=Nobody&limit=50&offset=0")

        assert response.json()["items"] == []

    def test_an_unset_filter_still_lists_everyone(self, seeded, client):
        """Both filters are applied on every request, defaulting to the empty
        string, so escaping them must not turn "no filter" into "no rows"."""
        response = client.get("/admin/users?limit=50&offset=0")

        assert {"Alice Smith", "Bob Jones"} <= self.names_from(response)

    @pytest.mark.parametrize("column", ["name", "email"])
    def test_an_underscore_is_matched_literally(self, db, client, admin_id, column):
        """`_` is a single-character wildcard in SQL `LIKE`. Unescaped, a search
        for `a_b` also returns `axb`, so the admin gets rows that do not contain
        what they typed."""
        seed_user(db, "uid-lit", "a_b@example.com", name="a_b")
        seed_user(db, "uid-wild", "axb@example.com", name="axb")

        response = client.get(f"/admin/users?{column}=a_b&limit=50&offset=0")

        assert self.names_from(response) == {"a_b"}

    def test_a_percent_matches_nothing_rather_than_everyone(self, seeded, client):
        """The inverse mistake: `%` unescaped is "match everything", which reads as
        a filter that silently did not apply."""
        response = client.get("/admin/users?name=%25&limit=50&offset=0")

        assert response.json()["items"] == []


class TestUserListSorting:
    """The grid's column headers. `sort` arrives as repeated query parameters
    consumed in (column, direction) pairs; `role` and `role_id` are the
    interesting columns, because they live on joined tables rather than `users`.
    """

    def names_from(self, response):
        return [item["name"] for item in response.json()["items"]]

    @pytest.mark.parametrize(
        "direction,expected",
        [("asc", ["Alice", "Carol", "Dave"]), ("desc", ["Dave", "Carol", "Alice"])],
    )
    def test_a_name_sort_orders_the_page(
        self, db, client, admin_id, direction, expected
    ):
        db.query(UserModel).filter(UserModel.id == admin_id).one().name = "Alice"
        db.commit()
        seed_user(db, "uid-carol", "carol@example.com", name="Carol")
        seed_user(db, "uid-dave", "dave@example.com", name="Dave")

        response = client.get(
            f"/admin/users?sort=name&sort={direction}&limit=50&offset=0"
        )

        assert self.names_from(response) == expected

    def test_the_default_order_is_by_id_ascending(self, db, client, admin_id):
        """No sort parameter at all, which is how the grid first loads."""
        seed_user(db, "uid-second", "second@example.com", name="Second")
        seed_user(db, "uid-third", "third@example.com", name="Third")

        response = client.get("/admin/users?limit=50&offset=0")

        ids = [item["id"] for item in response.json()["items"]]
        assert ids == sorted(ids)

    @pytest.mark.parametrize("column", ["role", "role_id"])
    def test_a_role_sort_orders_by_an_aggregate(self, column):
        """The list query groups by `users.id`, so a role column on a joined table
        has to be ordered by an aggregate. SQLite accepts a bare joined column and
        MySQL rejects it (only_full_group_by, 1055), which makes clicking the Role
        header a 400 rather than a sort - so this asserts the mapping the route
        passes rather than trusting the harness to notice.
        """
        assert "min" in str(crud_users.USER_LIST_SORT_MAPPING[column]).lower()

    @pytest.mark.parametrize("column", ["role", "role_id"])
    def test_a_role_sort_is_mapped_onto_the_joined_table(
        self, db, client, admin_id, column
    ):
        """Neither column exists on `users`; the route maps them to
        `RoleModel.role` and `UserRoleModel.role_id`. Left unmapped, the lookup
        raises `AttributeError` and the route answers 400 instead of sorting."""
        seed_user(db, "uid-op-sort", "opsort@example.com", UserRole.operator.value)

        ascending = client.get(f"/admin/users?sort={column}&sort=asc&limit=50&offset=0")
        descending = client.get(
            f"/admin/users?sort={column}&sort=desc&limit=50&offset=0"
        )

        assert ascending.status_code == 200
        assert descending.status_code == 200
        ascending_roles = [item["role_id"] for item in ascending.json()["items"]]
        descending_roles = [item["role_id"] for item in descending.json()["items"]]
        # An admin and an operator are seeded, so the two orders differ
        assert ascending_roles == sorted(ascending_roles)
        assert descending_roles == sorted(descending_roles, reverse=True)
        assert ascending_roles != descending_roles


class TestGetOneUser:
    """`GET /admin/users/{user_id}`, which the grid uses to refresh a single row
    after an edit."""

    def test_the_user_is_returned_with_the_role_read_from_user_roles(
        self, db, client, admin_id
    ):
        """`role_id` is not a column on `users`; it is joined in and grafted onto
        the row, so a dropped join answers with the user and a null role."""
        user_id = seed_user(
            db, "uid-one", "one@example.com", UserRole.admin.value, name="Just One"
        )

        response = client.get(f"/admin/users/{user_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == user_id
        assert body["name"] == "Just One"
        assert body["email"] == "one@example.com"
        assert body["role_id"] == UserRole.admin.value

    def test_a_soft_deleted_user_is_a_404(self, db, client, admin_id):
        """The list filters on `active`; this route has to as well, or a deleted
        id fetched directly still resolves."""
        user_id = seed_user(db, "uid-inactive", "inactive@example.com", active=False)

        response = client.get(f"/admin/users/{user_id}")

        assert response.status_code == 404

    def test_an_absent_user_is_a_404(self, db, client, admin_id):
        response = client.get("/admin/users/999999")

        assert response.status_code == 404

    def test_a_user_from_another_organization_is_a_404(self, db, client, admin_id):
        """The route passes the caller's own organization down, so an id guessed
        from another tenant must not resolve."""
        db.add(OrganizationModel(id=2, name="Other Org"))
        db.commit()
        outsider = UserModel(
            organization_id=2,
            uid="uid-outsider-get",
            name="Outsider",
            email="outsider-get@example.com",
            attributes={},
            active=True,
        )
        db.add(outsider)
        db.commit()
        db.refresh(outsider)

        response = client.get(f"/admin/users/{outsider.id}")

        assert response.status_code == 404


class TestUpdateUserOverHttp:
    """The update tests above call `crud_users.update_user` directly, which skips
    the route's own wiring: the caller's organization, and the id from the path."""

    def test_the_edit_is_persisted_through_the_route(
        self, db, client, no_firebase, admin_id
    ):
        user_id = seed_user(db, "uid-http", "http@example.com", name="Before")

        response = client.put(
            f"/admin/users/{user_id}",
            json={
                "name": "After",
                "email": "http@example.com",
                "role_id": UserRole.admin.value,
            },
        )

        assert response.status_code == 200
        row = db.query(UserModel).filter(UserModel.id == user_id).one()
        assert row.name == "After"
        assert role_ids_of(db, user_id) == [UserRole.admin.value]

    def test_an_absent_user_is_a_404(self, db, client, no_firebase, admin_id):
        response = client.put(
            "/admin/users/999999",
            json={
                "name": "Nobody",
                "email": "nobody@example.com",
                "role_id": UserRole.operator.value,
            },
        )

        assert response.status_code == 404

    def test_an_incomplete_body_is_a_422(self, db, client, admin_id):
        user_id = seed_user(db, "uid-partial", "partial@example.com")

        response = client.put(f"/admin/users/{user_id}", json={"name": "Only A Name"})

        assert response.status_code == 422


class TestUserListPagination:
    def test_limit_and_offset_walk_the_list_without_repeating(
        self, db, client, admin_id
    ):
        for index in range(3):
            seed_user(db, f"uid-page-{index}", f"page{index}@example.com")

        first = client.get("/admin/users?limit=2&offset=0").json()
        second = client.get("/admin/users?limit=2&offset=2").json()

        # 3 seeded plus the admin
        assert first["total"] == 4
        assert len(first["items"]) == 2
        assert len(second["items"]) == 2
        first_ids = {item["id"] for item in first["items"]}
        # A dropped offset would serve page 1 again
        assert first_ids.isdisjoint({item["id"] for item in second["items"]})

    def test_an_offset_past_the_end_is_empty_rather_than_an_error(
        self, db, client, admin_id
    ):
        response = client.get("/admin/users?limit=50&offset=500")

        assert response.status_code == 200
        assert response.json()["items"] == []


class TestCreateUser:
    """The row set an admin-created account starts life with."""

    @pytest.mark.parametrize("role_id", [UserRole.admin.value, UserRole.operator.value])
    def test_the_requested_role_is_honoured(self, db, no_firebase, admin_id, role_id):
        created = create(db, no_firebase, "created@example.com", role_id)

        new_id = created["user"].id
        assert role_ids_of(db, new_id) == [role_id]

    def test_the_new_user_is_active_and_free_with_a_quota(
        self, db, no_firebase, admin_id
    ):
        created = create(db, no_firebase, "fresh@example.com", UserRole.operator.value)
        new_id = created["user"].id

        row = db.query(UserModel).filter(UserModel.id == new_id).one()
        assert row.active is True
        subscription = (
            db.query(UserSubscription).filter(UserSubscription.user_id == new_id).one()
        )
        assert subscription.plan_id == SubscriptionPlanIds.FREE
        storage = (
            db.query(UserStorageUsage).filter(UserStorageUsage.user_id == new_id).one()
        )
        assert storage.storage_quota_bytes == 5 * GB
        assert storage.storage_usage_bytes == 0

    @pytest.mark.parametrize("role_id", [UserRole.admin.value, UserRole.operator.value])
    def test_no_stripe_row_is_written_for_an_admin_created_account(
        self, db, no_firebase, admin_id, role_id
    ):
        """An admin-created account has never been near
        Stripe, so a customer or purchase row at this point would either bill
        somebody who has not bought anything or mis-key the first real purchase.
        """
        created = create(db, no_firebase, "nostripe@example.com", role_id)
        new_id = created["user"].id

        # The counts below are only meaningful if the creation really ran
        assert (
            db.query(UserSubscription)
            .filter(UserSubscription.user_id == new_id)
            .count()
            == 1
        )
        assert (
            db.query(SubscriptionUserAccount)
            .filter(SubscriptionUserAccount.user_id == new_id)
            .count()
            == 0
        )
        assert (
            db.query(SubscriptionUserPurchase)
            .filter(SubscriptionUserPurchase.user_id == new_id)
            .count()
            == 0
        )

    def test_the_firebase_account_is_created_pre_verified(
        self, db, no_firebase, admin_id
    ):
        """An admin-created user must be able to log in without clicking a link
        in an inbox the admin does not own."""
        create(db, no_firebase, "verified@example.com", UserRole.operator.value)

        assert no_firebase.create_user.call_args.kwargs["email_verified"] is True

    def test_self_registration_cannot_ask_for_a_role(self, db, no_firebase, admin_id):
        """The unverified path is `/register`, which is unauthenticated, so an
        honoured `role_id` there would let anyone mint themselves an admin."""
        with patch.object(crud_users.AuthEmailService, "send_verification_email"):
            created = create(
                db,
                no_firebase,
                "selfmade@example.com",
                UserRole.admin.value,
                verified=False,
            )

        assert role_ids_of(db, created["user"].id) == [UserRole.operator.value]


class TestCreateUserValidation:
    @pytest.mark.parametrize("missing", ["email", "password", "name", "role_id"])
    def test_every_field_is_required(self, missing):
        payload = {
            "email": "who@example.com",
            "password": STRONG_PASSWORD,
            "name": "Who",
            "role_id": UserRole.operator.value,
        }
        payload.pop(missing)

        with pytest.raises(ValidationError) as raised:
            UserCreate(**payload)

        assert missing in str(raised.value)

    def test_the_route_answers_422_for_an_incomplete_body(self, client, admin_id):
        response = client.post("/admin/users", json={"name": "Who"})

        assert response.status_code == 422

    @pytest.mark.parametrize(
        "email", ["not-an-email", "missing@tld", "@example.com", "spaces in@x.com"]
    )
    def test_an_invalid_email_is_rejected(self, email):
        with pytest.raises(ValidationError):
            UserCreate(
                email=email,
                password=STRONG_PASSWORD,
                name="Who",
                role_id=UserRole.operator.value,
            )

    @pytest.mark.parametrize(
        "password",
        [
            "aB!1",  # shorter than six characters
            "abcdef!",  # no digit
            "abcdef1",  # no special character
            "123456!",  # no letter
            "a" * 256 + "1!",  # longer than 255 characters
        ],
    )
    def test_a_weak_password_is_rejected(self, password):
        with pytest.raises(ValidationError):
            UserCreate(
                email="who@example.com",
                password=password,
                name="Who",
                role_id=UserRole.operator.value,
            )

    def test_a_password_meeting_every_rule_is_accepted(self):
        assert (
            UserCreate(
                email="who@example.com",
                password=STRONG_PASSWORD,
                name="Who",
                role_id=UserRole.operator.value,
            ).password
            == STRONG_PASSWORD
        )

    @pytest.mark.parametrize("special", ALLOWED_SPECIALS)
    def test_each_documented_special_character_satisfies_the_rule(self, special):
        """The set the error message advertises has to be the set the rule
        accepts. Reading it out of `password_regex` instead would stop testing a
        character the moment the rule dropped it."""
        UserCreate(
            email="who@example.com",
            password=f"abcd1{special}",
            name="Who",
            role_id=UserRole.operator.value,
        )

    def test_a_duplicate_email_is_reported_and_writes_nothing(
        self, db, no_firebase, admin_id
    ):
        # The real SDK error: typed, code ALREADY_EXISTS. Faking a bespoke code
        # here is how a 500 on real duplicates went unnoticed.
        no_firebase.create_user.side_effect = EmailAlreadyExistsError(
            "The user with the provided email already exists (EMAIL_EXISTS).",
            None,
            None,
        )
        before = db.query(UserModel).count()

        with pytest.raises(HTTPException) as raised:
            create(db, no_firebase, "dupe@example.com", UserRole.operator.value)

        assert raised.value.status_code == 400
        assert "already registered" in raised.value.detail
        assert db.query(UserModel).count() == before

    def test_a_failure_after_the_firebase_account_exists_leaves_no_orphan(
        self, db, no_firebase, admin_id
    ):
        """The partial state that actually matters: a Firebase login with no
        application user behind it. The DB rows are rolled back, so the Firebase
        account has to go too."""
        before = db.query(UserModel).count()
        with patch.object(
            crud_users, "RemoteStorageController"
        ) as storage, patch.object(
            crud_users, "ensure_user_bucket_exists", return_value=None
        ):
            storage.is_available.return_value = True
            with pytest.raises(HTTPException) as raised:
                create(db, no_firebase, "orphan@example.com", UserRole.operator.value)

        assert raised.value.status_code == 500
        assert db.query(UserModel).count() == before
        no_firebase.delete_user.assert_called_once_with("fb-orphan@example.com")


class TestUpdateUser:
    def test_the_name_is_persisted(self, db, no_firebase, admin_id):
        user_id = seed_user(db, "uid-rename", "rename@example.com", name="Old Name")

        asyncio.run(
            crud_users.update_user(
                db,
                user_id,
                UserUpdate(
                    name="New Name",
                    email="rename@example.com",
                    role_id=UserRole.operator.value,
                ),
                ORGANIZATION_ID,
            )
        )

        assert db.query(UserModel).filter(UserModel.id == user_id).one().name == (
            "New Name"
        )

    def test_the_role_row_is_replaced_rather_than_added_to(
        self, db, no_firebase, admin_id
    ):
        """`set_role` deletes before inserting. A second row would make
        `func.min(role_id)` in the list query decide the role, so an admin
        promoted from operator would still read as an operator."""
        user_id = seed_user(db, "uid-promote", "promote@example.com")

        asyncio.run(
            crud_users.update_user(
                db,
                user_id,
                UserUpdate(
                    name="Promote Me",
                    email="promote@example.com",
                    role_id=UserRole.admin.value,
                ),
                ORGANIZATION_ID,
            )
        )

        assert role_ids_of(db, user_id) == [UserRole.admin.value]

    def test_the_email_is_persisted_and_pushed_to_firebase(
        self, db, no_firebase, admin_id
    ):
        """The DB address and the login address are two stores. Updating only
        the first leaves the user unable to log in with what the admin sees."""
        user_id = seed_user(db, "uid-remail", "before@example.com")

        asyncio.run(
            crud_users.update_user(
                db,
                user_id,
                UserUpdate(
                    name="Remail",
                    email="after@example.com",
                    role_id=UserRole.operator.value,
                ),
                ORGANIZATION_ID,
            )
        )

        assert db.query(UserModel).filter(UserModel.id == user_id).one().email == (
            "after@example.com"
        )
        no_firebase.update_user.assert_called_once_with(
            "uid-remail", email="after@example.com"
        )

    def test_a_user_from_another_organization_is_not_found(
        self, db, no_firebase, admin_id
    ):
        db.add(OrganizationModel(id=2, name="Other Org"))
        db.commit()
        outsider = UserModel(
            organization_id=2,
            uid="uid-outsider",
            name="Outsider",
            email="outsider@example.com",
            attributes={},
            active=True,
        )
        db.add(outsider)
        db.commit()
        db.refresh(outsider)

        with pytest.raises(HTTPException) as raised:
            asyncio.run(
                crud_users.update_user(
                    db,
                    outsider.id,
                    UserUpdate(
                        name="Hijacked",
                        email="outsider@example.com",
                        role_id=UserRole.admin.value,
                    ),
                    ORGANIZATION_ID,
                )
            )

        assert raised.value.status_code == 404
        assert db.query(UserModel).filter(UserModel.id == outsider.id).one().name == (
            "Outsider"
        )


class TestSelfDeleteIsRejectedServerSide:
    """The DataGrid hides the button for the signed-in admin's own row, which is
    not a guard: `crud_users.delete_user` deletes the Firebase account first and
    by design, so a request that reaches it destroys the caller's own login
    before anything else runs."""

    def test_an_admin_deleting_themselves_is_refused(self, db, admin):
        with patch.object(crud_users, "delete_user") as delete:
            with pytest.raises(HTTPException) as raised:
                asyncio.run(
                    users_admin.delete_user(
                        user_id=admin.id, db=db, current_admin=admin
                    )
                )

        assert raised.value.status_code == 403
        # The callee is where the damage is, so the refusal has to happen before it
        delete.assert_not_called()

    def test_the_refusal_is_a_403_over_http(self, db, client, admin):
        """The gating parametrisation only ever asks for an absent id, so the
        status and message a caller actually receives are unasserted without
        this."""
        with patch.object(crud_users, "delete_user") as delete:
            response = client.delete(f"/admin/users/{admin.id}")

        assert response.status_code == 403
        assert response.json()["detail"] == (
            "An admin cannot delete their own account here. "
            "Use the account settings page instead."
        )
        delete.assert_not_called()

    def test_deleting_somebody_else_still_reaches_the_deletion_pipeline(
        self, db, admin
    ):
        other_id = seed_user(db, "uid-other", "other@example.com")

        with patch.object(crud_users, "delete_user", return_value=True) as delete:
            result = asyncio.run(
                users_admin.delete_user(user_id=other_id, db=db, current_admin=admin)
            )

        assert result is True
        assert delete.call_args.args[1] == other_id

    def test_the_self_service_deletion_path_is_untouched(self, db, admin):
        """The guard is "not through the Account Manager grid", not "an admin
        cannot leave": `/users/me` deletes the caller's own account by design,
        and a guard placed in `crud_users.delete_user` instead would break it."""
        with patch.object(users_me.crud_users, "delete_user", return_value=True):
            result = asyncio.run(users_me.delete_me(db=db, current_user=admin))

        assert result is True


class TestReRegisteringADeletedAddress:
    """Deletion is a soft delete, so the old row keeps the address.

    The *self-registration* form reaches
    `create_user(verified=False)`; the admin path is `verified=True`. Both are
    exercised because the unverified branch rewrites `role_id` and sends a
    verification email before the row set is settled.
    """

    @pytest.mark.parametrize("verified", [True, False])
    def test_the_address_can_be_registered_again_as_a_new_row(
        self, db, no_firebase, admin_id, verified
    ):
        email = "returning@example.com"
        old_id = seed_user(db, "uid-old", email)
        db.query(UserModel).filter(UserModel.id == old_id).one().active = False
        db.commit()

        no_firebase.create_user.side_effect = None
        no_firebase.create_user.return_value = MagicMock(uid="uid-new", email=email)
        with patch.object(
            crud_users.AuthEmailService, "send_verification_email"
        ) as send_email:
            created = create(
                db, no_firebase, email, UserRole.operator.value, verified=verified
            )

        assert send_email.call_count == (0 if verified else 1)

        rows = (
            db.query(UserModel)
            .filter(UserModel.email == email)
            .order_by(UserModel.id)
            .all()
        )
        assert [(row.id, row.active) for row in rows] == [
            (old_id, False),
            (created["user"].id, True),
        ]
        # The row carries the uid Firebase minted, not the soft-deleted row's
        # stale one and not something the application derived from the address
        assert rows[1].uid == "uid-new"
