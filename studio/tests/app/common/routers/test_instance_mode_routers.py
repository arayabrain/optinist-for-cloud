"""
Tests for INSTANCE_MODE=public router gating.

Verifies that `_register_routers(app, "public")` mounts the public-safe
routers (auth.router, users_me.router + beacon_router, dataview.public_router,
internal.router, outputs.router — these are on every tier so login + SPA
bootstrap survive a free-tier outage) and skips the workflow/optinist routers,
while default mode registers the full set.
"""

from fastapi import FastAPI

from studio.__main_unit__ import _register_routers


def _registered_paths(instance_mode: str) -> set[str]:
    """Build a fresh FastAPI app, register routers for the mode, return its paths."""
    app = FastAPI()
    _register_routers(app, instance_mode)
    return {route.path for route in app.routes if hasattr(route, "path")}


class TestInstanceModePublic:
    """In INSTANCE_MODE=public, only the public subset is registered."""

    def test_public_router_is_registered(self):
        paths = _registered_paths("public")
        assert any(p.startswith("/api/public/dataview") for p in paths)

    def test_outputs_router_is_registered(self):
        # Public carve-out lives in the auth dependency, not in registration.
        paths = _registered_paths("public")
        assert any(p.startswith("/api/visualizations") for p in paths)

    def test_internal_router_is_registered(self):
        paths = _registered_paths("public")
        assert any(p.startswith("/system-internal") for p in paths)

    def test_auth_router_is_registered_on_public(self):
        # Mounted on public so authentication survives a free-tier outage.
        paths = _registered_paths("public")
        assert any(p.startswith("/auth") for p in paths)

    def test_users_me_router_is_registered_on_public(self):
        # The SPA hits GET /users/me + the premium-assign endpoints right
        # after /auth/login; both must survive a free outage.
        paths = _registered_paths("public")
        assert "/users/me" in paths
        assert "/users/me/routing-info" in paths
        assert "/users/me/premium/assign" in paths
        assert "/users/me/premium/heartbeat" in paths
        assert "/users/me/premium/status" in paths

    def test_users_me_beacon_router_is_registered_on_public(self):
        # navigator.sendBeacon target; needs to survive free outage so the
        # 120s pending_release grace window starts even when free is down.
        paths = _registered_paths("public")
        assert "/users/me/premium/release-beacon" in paths

    def test_log_report_router_is_registered_on_public(self):
        # POST /log-report/frontend-errors survives free outage so client-side
        # errors still reach CloudWatch.
        paths = _registered_paths("public")
        assert "/log-report/frontend-errors" in paths

    def test_logs_router_is_not_registered_on_public(self):
        # /logs viewer stays on its owning tier — premium via p100-199
        # routing-id, free via p320 Bearer catch-all. Users should never see
        # public-task logs mixed into their own.
        paths = _registered_paths("public")
        assert not any(p.startswith("/logs") for p in paths)

    def test_workflow_router_is_not_registered(self):
        paths = _registered_paths("public")
        assert not any(p.startswith("/workflow") for p in paths)

    def test_run_router_is_not_registered(self):
        paths = _registered_paths("public")
        assert not any(p.startswith("/run") for p in paths)

    def test_authenticated_dataview_router_is_not_registered(self):
        paths = _registered_paths("public")
        non_public_dataview = [
            p
            for p in paths
            if p.startswith("/api/dataview") and not p.startswith("/api/public")
        ]
        assert non_public_dataview == []

    def test_subscriptions_router_is_not_registered(self):
        paths = _registered_paths("public")
        assert not any(p.startswith("/subscriptions") for p in paths)

    def test_experiments_router_is_not_registered(self):
        """A public task has no access to a user's experiment records, and
        ``/experiments`` was the one prefix left unchecked while every other
        one was covered."""
        paths = _registered_paths("public")
        assert not any(p.startswith("/experiments") for p in paths)

    def test_optinist_routers_are_not_registered(self):
        paths = _registered_paths("public")
        for prefix in ("/hdf5", "/mat", "/nwb"):
            assert not any(
                p.startswith(prefix) for p in paths
            ), f"{prefix} should not be registered in INSTANCE_MODE=public"

    def test_roi_edit_endpoints_are_not_registered(self):
        # roi.router shares prefix "/api/visualizations"; assert each explicitly.
        paths = _registered_paths("public")
        roi_endpoints = (
            "/api/visualizations/image/{filepath:path}/status",
            "/api/visualizations/image/{filepath:path}/add_roi",
            "/api/visualizations/image/{filepath:path}/merge_roi",
            "/api/visualizations/image/{filepath:path}/delete_roi",
            "/api/visualizations/image/{filepath:path}/commit_edit",
            "/api/visualizations/image/{filepath:path}/cancel_edit",
        )
        for endpoint in roi_endpoints:
            assert endpoint not in paths, (
                f"{endpoint} (roi.router) should not be registered "
                "in INSTANCE_MODE=public"
            )


class TestInstanceModeDefault:
    """In default / non-public mode, all routers are registered (regression check)."""

    def test_workflow_router_is_registered(self):
        paths = _registered_paths("default")
        assert any(p.startswith("/workflow") for p in paths)

    def test_auth_router_is_registered(self):
        paths = _registered_paths("default")
        assert any(p.startswith("/auth") for p in paths)

    def test_public_router_is_still_registered(self):
        paths = _registered_paths("default")
        assert any(p.startswith("/api/public/dataview") for p in paths)

    def test_authenticated_dataview_router_is_registered(self):
        paths = _registered_paths("default")
        non_public = [
            p
            for p in paths
            if p.startswith("/api/dataview") and not p.startswith("/api/public")
        ]
        assert (
            non_public
        ), "authenticated /api/dataview/* should be registered in default mode"
