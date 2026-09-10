"""The SPA shell catch-all and the health endpoint.

Both are served by ``__main_unit__`` on every tier, including public, where the
ALB health check and the browser's deep links are the only two things reaching
the instance before any API call happens.

The ALB routing that puts these requests on the public target group needs a
deployed environment; what is asserted here is that the application answers them
correctly once they arrive.

Two mechanisms can serve a deep link, and the assertions below deliberately do
not care which: ``SPARoutingMiddleware`` intercepts anything with
``Accept: text/html`` before routing, and the ``/{_:path}`` catch-all handles
whatever reaches the router. The contract is the response, not the mechanism.

``root()`` has two branches, and which one runs depends on whether the runner
happens to have a frontend build: CI checks out and runs pytest with no build, so
it serves ``no-built-pages.html``, while a developer machine with a local build
serves the real ``index.html``. Asserting only "200 and text/html" therefore
proves nothing about the shell in CI - a "frontend not built" placeholder
satisfies it. ``TestSpaCatchAllServesTheShell`` pins both branches explicitly
instead, so the built-index path is covered wherever it runs.
"""

from unittest.mock import patch

import pytest
from fastapi.templating import Jinja2Templates

from studio.__main_unit__ import app
from studio.app.common.core.middleware.spa_routing_middleware import (
    INDEX_HTML_CACHE_HEADERS,
)

MODULE = "studio.__main_unit__"

# Paths React Router owns that match no API router, so they reach the
# ``/{_:path}`` catch-all. Deliberately not ``/workspaces`` or ``/dataview``,
# which are real router prefixes; those are covered separately by the
# colliding-deep-link case, which needs the middleware rather than the catch-all.
CLIENT_SIDE_ONLY_PATHS = [
    "/dashboard",
    "/account",
    "/login",
    "/subscription/thanks",
    "/subscription/failed",
]


class TestSpaCatchAllServesTheShell:
    """A client-side route returns the SPA shell, not a 404.

    These paths exist only in React Router. A cold browser load or a refresh on
    any of them arrives as a plain GET matching no router, so ``any_pages`` must
    hand back ``index.html`` and let the client route. If it 404s, every deep
    link and every refresh is a broken page.
    """

    def test_client_side_routes_are_not_api_routes(self):
        """Guards the premise of every case below. If one of these paths ever
        becomes a real router prefix, the catch-all is no longer what answers
        it and the assertions stop meaning anything."""
        api_paths = {route.path for route in app.routes if hasattr(route, "path")}

        for path in CLIENT_SIDE_ONLY_PATHS:
            assert path not in api_paths, (
                f"{path} is now a mounted API route; pick a different "
                f"client-side-only path for the catch-all assertions"
            )

    @pytest.mark.parametrize("path", CLIENT_SIDE_ONLY_PATHS)
    def test_a_client_side_route_returns_the_shell(self, client, path):
        response = client.get(path)

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_an_unknown_api_path_404s_for_a_json_client(self, client):
        """The other half of the same handler: an XHR asking for JSON must get a
        404 rather than a page of HTML, or the frontend parses the shell as a
        failed response body instead of surfacing the error.
        """
        response = client.get(
            "/api/does-not-exist", headers={"accept": "application/json"}
        )

        assert response.status_code == 404

    def test_the_same_path_branches_on_the_accept_header(self, client):
        """The branch is ``"application/json" in accept``, so one path must go
        both ways depending only on that header."""
        as_browser = client.get("/dashboard", headers={"accept": "text/html"})
        as_xhr = client.get("/dashboard", headers={"accept": "application/json"})

        assert as_browser.status_code == 200
        assert as_xhr.status_code == 404

    @pytest.mark.parametrize("path", CLIENT_SIDE_ONLY_PATHS[:2])
    def test_the_built_index_is_served_when_a_build_exists(
        self, client, path, tmp_path
    ):
        """The branch CI never reaches on its own.

        Without this, every assertion in this class is satisfied by
        ``no-built-pages.html`` - a placeholder that says the frontend is not
        built, which is the opposite of the shell being served.
        """
        marker = '<!--built-index-marker--><div id="root"></div>'
        (tmp_path / "index.html").write_text(marker)

        with patch(f"{MODULE}.os.path.exists", return_value=True), patch(
            f"{MODULE}.build_templates", Jinja2Templates(directory=str(tmp_path))
        ):
            response = client.get(path)

        assert response.status_code == 200
        assert "built-index-marker" in response.text
        for header, value in INDEX_HTML_CACHE_HEADERS.items():
            assert response.headers.get(header) == value

    def test_a_deep_link_that_collides_with_a_real_api_route_gets_html(self, client):
        """``/workspaces`` is both a React route and a real authenticated
        ``GET``, which is the one case the catch-all cannot handle.

        Browser navigation carries no Authorization header, so the request must be
        intercepted by ``SPARoutingMiddleware`` on ``Accept: text/html`` before
        routing. Reaching the API instead is what produced "Could not validate
        credentials" on a refresh. Deliberately the bare path: ``/workspaces/123``
        matches no route, so it falls through to the catch-all and would pass with
        the middleware disabled.
        """
        response = client.get("/workspaces", headers={"accept": "text/html"})

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_the_shell_is_not_cached(self, client):
        """``index.html`` names hashed asset bundles. A cached shell after a
        deploy points at bundles that no longer exist, which is the chunk-load
        failure the client-side chunk-load reload exists to absorb.

        Asserted against the constant the app sends rather than a duplicated
        header string, so the two cannot drift apart.
        """
        response = client.get("/dashboard")

        for header, value in INDEX_HTML_CACHE_HEADERS.items():
            assert response.headers.get(header) == value, (
                f"{header} was {response.headers.get(header)!r}, " f"expected {value!r}"
            )
        assert (
            "no-store" in INDEX_HTML_CACHE_HEADERS["Cache-Control"]
        ), "the constant itself stopped forbidding storage"


class TestHealthEndpoint:
    """``/health`` answers 200.

    The public ASG uses ``health_check_type = "ELB"``, so this response is what
    keeps instances in the target group. A non-200 rolls the whole public tier.
    """

    def test_health_returns_200_and_healthy(self, client):
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_health_needs_no_authentication(self):
        """The ALB health check sends no Authorization header.

        Asserted against the route's declaration, not a request.
        ``dependency_overrides`` is state on the shared ``app``, and the session
        fixture stubs the auth dependencies out for the whole run - so a request
        would 200 even if someone added ``Depends(get_current_user)`` here, and
        the ASG would then drop every instance in production.
        """
        health = next(
            route for route in app.routes if getattr(route, "path", None) == "/health"
        )

        # ``route.dependencies`` only holds the decorator's ``dependencies=[...]``.
        # A ``Depends`` in the handler signature lands in ``dependant`` instead,
        # which is the shape that actually gets written by accident.
        resolved = [
            getattr(dep.call, "__name__", dep.call)
            for dep in health.dependant.dependencies
        ]
        assert not health.dependencies, (
            f"/health gained dependencies {health.dependencies}; the ALB health "
            f"check cannot satisfy any of them"
        )
        assert not resolved, (
            f"/health gained signature-level dependencies {resolved}; the ALB "
            f"health check cannot satisfy any of them"
        )
        assert not health.dependant.security_requirements
