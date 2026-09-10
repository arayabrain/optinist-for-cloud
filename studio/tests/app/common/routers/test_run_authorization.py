"""Every /run endpoint must declare a workspace guard.

These assert the route *declarations* rather than calling through TestClient:
the session fixture in conftest.py replaces both workspace dependencies with
skip_dependencies, so a request-level test would pass even with the guard
removed. Inspecting the router is what actually pins the invariant down.

`apply_filter` shipped without any workspace dependency -- authenticated by
the router-level get_current_user in __main_unit__.py, but with nothing
checking the workspace -- so any logged-in user could rewrite another
workspace's node output. CodeQL does not model authorization, so nothing
caught it automatically.
"""
import pytest

from studio.app.common.core.workspace.workspace_dependencies import (
    is_workspace_available,
    is_workspace_owner,
)
from studio.app.common.routers.run import router

WORKSPACE_GUARDS = {is_workspace_owner, is_workspace_available}

# Endpoints that write experiment data. Reads may be shared; writes may not.
WRITE_ENDPOINTS = {"run", "run_id", "cancel_run", "apply_filter"}


def guards_of(route):
    return {d.dependency for d in getattr(route, "dependencies", [])}


def routes_taking_a_workspace():
    return [r for r in router.routes if "{workspace_id}" in r.path]


def test_every_workspace_route_is_guarded():
    unguarded = [
        r.name
        for r in routes_taking_a_workspace()
        if not guards_of(r) & WORKSPACE_GUARDS
    ]
    assert unguarded == [], (
        f"{unguarded} take a workspace_id but declare no workspace dependency; "
        "authentication alone does not check who owns the workspace"
    )


@pytest.mark.parametrize("name", sorted(WRITE_ENDPOINTS))
def test_write_endpoints_require_ownership(name):
    route = next(r for r in router.routes if r.name == name)
    assert is_workspace_owner in guards_of(route), (
        f"{name} writes experiment data, so it must require ownership rather "
        "than mere access to a shared workspace"
    )


def test_run_result_only_requires_access():
    """The one read endpoint: a user a workspace is shared with may poll it."""
    route = next(r for r in router.routes if r.name == "run_result")
    assert guards_of(route) == {is_workspace_available}


def test_the_write_endpoint_list_is_complete():
    """Fails when a new /run endpoint appears, so the guard question is asked
    for it rather than silently defaulting to unguarded."""
    known = WRITE_ENDPOINTS | {"run_result"}
    actual = {r.name for r in routes_taking_a_workspace()}
    assert actual == known, (
        f"unclassified /run endpoints: {sorted(actual - known)}; "
        "add each to WRITE_ENDPOINTS or document it as a read"
    )
