"""
Producer-side contract test for the premium/routing response shapes.

Asserts the FastAPI premium/routing response models agree with the shared
fixtures the frontend consumes:
  frontend/src/utils/routing/__fixtures__/premium_routing/premium_contract.json

The frontend reads the SAME file (frontend/src/utils/routing/
premiumRoutingContract.test.ts), so a shape drift on either side breaks one of
the two tests. The backend test container mounts the repo root at /app, so the
fixture is reachable by repo-relative path.

Identifier-omission guards already live in test_premium_api_contract.py; this
file does not duplicate them — it only checks the fixtures declare no
identifier keys and that each fixture is a faithful subset of its model.
"""

import asyncio
import contextlib
import importlib.util
import json
from pathlib import Path
from unittest import mock

import pytest

from studio.app.common.core.middleware import secure_routing_middleware
from studio.app.common.schemas.premium import (
    FreeLogoutResponse,
    PremiumAssignmentDetail,
    PremiumAssignResponse,
    PremiumHeartbeatResponse,
    PremiumReleaseResponse,
    PremiumStatusResponse,
    RoutingInfoResponse,
)

_REPO_ROOT = Path(__file__).resolve().parents[5]
_FIXTURE = (
    _REPO_ROOT
    / "frontend/src/utils/routing/__fixtures__/premium_routing/premium_contract.json"
)
_AWS_CONSTANTS = _REPO_ROOT / "infrastructure" / "aws_constants.py"

# The fixture and the header source of truth live in sibling trees (frontend/,
# infrastructure/). They are present under the repo-root mount (.:/app); skip
# cleanly on a partial checkout rather than erroring at collection.
if not _FIXTURE.exists() or not _AWS_CONSTANTS.exists():
    pytest.skip(
        "premium contract fixture or aws_constants not present in this checkout",
        allow_module_level=True,
    )

CONTRACT = json.loads(_FIXTURE.read_text())


def _backend_routing_headers():
    # Header-name source of truth is infrastructure/aws_constants.py. Load it
    # straight from the file under a unique name: the app test suite stubs
    # sys.modules["aws_constants"] (see test_data_sync.py), so a plain import
    # would pick up that stub instead of the real constants.
    spec = importlib.util.spec_from_file_location(
        "_premium_contract_aws", _AWS_CONSTANTS
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RoutingHeaders


RoutingHeaders = _backend_routing_headers()

# (fixture key, response model) — the six typed endpoints.
TYPED_MODELS = [
    ("routing_info", RoutingInfoResponse),
    ("premium_assign", PremiumAssignResponse),
    ("premium_release", PremiumReleaseResponse),
    ("premium_status", PremiumStatusResponse),
    ("premium_heartbeat", PremiumHeartbeatResponse),
    ("free_logout", FreeLogoutResponse),
]


@pytest.mark.parametrize("key,model_cls", TYPED_MODELS)
def test_fixture_is_valid_for_model(key, model_cls):
    fixture = CONTRACT[key]
    # Constructs => every required field is present with a coercible type.
    model_cls(**fixture)
    # Every fixture key is a declared field => no drift / typo / stale key.
    unknown = set(fixture) - set(model_cls.__fields__)
    assert not unknown, f"{key} has keys absent from {model_cls.__name__}: {unknown}"


def test_status_nested_assignment_shape():
    # PremiumStatusResponse.assignment is Optional[PremiumAssignmentDetail], a
    # typed model. Derive the allowed nested keys from that model so a field
    # rename there is caught here, and confirm the fixture declares no key the
    # model would silently drop.
    a = CONTRACT["premium_status"]["assignment"]
    allowed = set(PremiumAssignmentDetail.__fields__)
    unknown = set(a) - allowed
    assert (
        not unknown
    ), f"nested assignment has keys absent from PremiumAssignmentDetail: {unknown}"
    # Constructs => every fixture value coerces to its declared type.
    PremiumAssignmentDetail(**a)
    assert "uid" not in a and "user_id" not in a


@pytest.mark.parametrize("key,_model_cls", TYPED_MODELS)
def test_fixture_carries_no_identifier(key, _model_cls):
    fixture = CONTRACT[key]
    assert "uid" not in fixture
    assert "user_id" not in fixture


def test_release_beacon_untyped_shape():
    # release-beacon stays response_model=Dict (untyped); pin its shape here.
    f = CONTRACT["release_beacon"]
    assert isinstance(f["success"], bool)
    assert isinstance(f.get("message", ""), str)


def test_header_names_match_backend_source_of_truth():
    h = CONTRACT["headers"]
    assert RoutingHeaders.ROUTING_ID.lower() == h["routing_id"].lower()
    assert RoutingHeaders.USER_TIER.lower() == h["user_tier"].lower()
    assert RoutingHeaders.SERVED_BY_INSTANCE.lower() == h["served_by_instance"].lower()


def _run_secure_routing_middleware():
    """Drive SecureRoutingMiddleware around a header-less ASGI app as a premium
    user and return the lower-cased response header names it appended."""
    captured = []

    async def inner_app(scope, receive, send):
        # Emit no headers here, so the captured start message contains exactly
        # the names the middleware appends.
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def capture_send(message):
        captured.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "path": "/",
        "headers": [(b"authorization", b"Bearer test-token")],
    }
    middleware = secure_routing_middleware.SecureRoutingMiddleware(inner_app)

    srm = secure_routing_middleware
    # The app test conftest forces IS_STANDALONE=True, under which the
    # middleware skips header injection; flip it off so send_wrapper runs.
    # extract_uid / tier / instance-id are mocked so the wrapper reaches the
    # header-append path without a real JWT, DB, or instance-metadata lookup.
    patches = [
        mock.patch.object(srm.MODE, "IS_STANDALONE", False),
        mock.patch.object(
            srm, "extract_uid_from_firebase_jwt", return_value=("test-uid", None)
        ),
        mock.patch.object(srm, "get_user_tier_cached", return_value=srm.TIER_PREMIUM),
        mock.patch.object(srm, "_get_instance_id", return_value="i-0test123"),
    ]
    with contextlib.ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        asyncio.run(middleware(scope, receive, capture_send))

    start = next(m for m in captured if m["type"] == "http.response.start")
    return {name.decode().lower() for name, _ in start["headers"]}


def test_middleware_emits_fixture_header_names():
    # Binds the real header emitter to the fixture: renaming a hardcoded header
    # literal in SecureRoutingMiddleware breaks this test.
    emitted = _run_secure_routing_middleware()
    expected = {v.lower() for v in CONTRACT["headers"].values()}
    assert emitted == expected, (
        "SecureRoutingMiddleware response header names drifted from the shared "
        f"premium_contract.json headers block: emitted={sorted(emitted)} "
        f"expected={sorted(expected)}"
    )
