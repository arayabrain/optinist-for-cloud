"""Tests for the e2e Firebase sweep script.

The sweep deletes accounts, so the things worth asserting are which addresses it
selects, which it leaves alone, and that a partial delete is never reported as a
clean one.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "scripts"
    / "sweep_e2e_firebase_users.py"
)

_spec = importlib.util.spec_from_file_location("sweep_e2e_firebase_users", MODULE_PATH)
sweeper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweeper)

# Every fixture address below is stamped well before this, so the grace period
# only spares accounts a test stamps deliberately close to it.
NOW_MS = 1787000000000


def user(email):
    return SimpleNamespace(uid=f"uid-{email}", email=email)


def stale_uids(users):
    return sweeper.stale_uids(users, NOW_MS)


SWEPT = [
    "e2e_unverified_1786520283944@test.com",
    "e2e_admin_created_operator_1786930000000@test.com",
    "e2e_admin_mutable_1786930000000@test.com",
]

SPARED = [
    # Fixed accounts the suite logs in as: no Date.now() suffix
    "e2e_ci_free@test.com",
    "e2e_ci_admin@test.com",
    "e2e_ci_lifecycle@test.com",
    "e2e_local_admin@test.com",
    # A real address
    "someone@araya.org",
    # Near misses on the timestamp
    "e2e_x_178652028394@test.com",
    "e2e_x_17865202839441@test.com",
    # Non-ASCII digits: \d would match these, [0-9] does not
    "e2e_x_١٢٣٤٥٦٧٨٩٠١٢٣@test.com",
    # A real address that merely contains a throwaway-shaped substring
    "victim+e2e_x_1786520283944@test.com",
    # Right shape, wrong domain
    "e2e_x_1786520283944@example.com",
    None,
]


@pytest.mark.parametrize("email", SWEPT)
def test_throwaway_accounts_are_swept(email):
    assert stale_uids([user(email)]) == [f"uid-{email}"]


@pytest.mark.parametrize("email", SPARED)
def test_every_other_account_is_spared(email):
    assert stale_uids([user(email)]) == []


def test_mixed_list_selects_only_the_throwaways():
    users = [user(email) for email in SWEPT + SPARED]
    assert stale_uids(users) == [f"uid-{email}" for email in SWEPT]


def stamped(age_ms):
    return f"e2e_admin_mutable_{NOW_MS - age_ms}@test.com"


# Concrete ages rather than offsets from GRACE_MS: expressed against the
# constant, these still pass when the grace period is mutated to zero. Three
# hours is a suite still inside playwright's 165-minute globalTimeout.
@pytest.mark.parametrize("age_ms", [0, 10 * 60 * 1000, 3 * 60 * 60 * 1000])
def test_a_concurrent_runs_accounts_are_left_alone(age_ms):
    assert stale_uids([user(stamped(age_ms))]) == []


def test_an_account_from_an_earlier_run_is_swept():
    email = stamped(6 * 60 * 60 * 1000)
    assert stale_uids([user(email)]) == [f"uid-{email}"]


class FakeAuth:
    """delete_users reports per-uid failures in its result rather than raising."""

    def __init__(self, failures_per_batch=0):
        self.batches = []
        self.failures_per_batch = failures_per_batch

    def delete_users(self, uids):
        self.batches.append(len(uids))
        errors = [
            SimpleNamespace(index=i, reason="QUOTA_EXCEEDED")
            for i in range(min(self.failures_per_batch, len(uids)))
        ]
        return SimpleNamespace(success_count=len(uids) - len(errors), errors=errors)


def batch_of(count):
    return [user(f"e2e_batch_{1786520283944 + i}@test.com") for i in range(count)]


def test_deletes_are_batched_under_the_api_cap():
    auth = FakeAuth()
    assert sweeper.sweep(auth, batch_of(2500), NOW_MS) == 2500
    assert auth.batches == [1000, 1000, 500]


def test_a_partial_delete_is_not_reported_as_a_clean_sweep():
    auth = FakeAuth(failures_per_batch=3)
    with pytest.raises(RuntimeError, match="deleted 1494, 6 failed"):
        sweeper.sweep(auth, batch_of(1500), NOW_MS)


def test_nothing_to_delete_makes_no_api_call():
    auth = FakeAuth()
    assert sweeper.sweep(auth, [user("e2e_ci_free@test.com")], NOW_MS) == 0
    assert auth.batches == []
