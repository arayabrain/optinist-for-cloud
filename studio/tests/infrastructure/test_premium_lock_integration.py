"""Real-MySQL GET_LOCK integration test for distributed_lock.

Opt-in. The three lock tests are skipped unless
RUN_PREMIUM_LOCK_IT=1 with RDS_* env pointing at a reachable MySQL (see
docker-compose.premium-lock-it.yml). The per-PR lambda lane collects this
module but skips them (no DB there), so it never hangs CI.

Scope: this proves the REAL distributed_lock (MySQL GET_LOCK) serializes
concurrent SESSIONS - mutual exclusion, serialization under contention, and
per-name isolation. The mocked TestConcurrentAssignLock tests always grant the
lock, so they model the corruption the lock prevents but cannot prove the lock
serializes; this does. What this does NOT do is run the real assign_premium_user
against a real DB - that the critical section actually executes INSIDE the lock
is guarded structurally by TestConcurrentAssignLock::
test_assign_impl_runs_inside_the_lock (deterministic, per-PR). A full concurrent
assign_premium_user race against a reconstructed schema stays deferred.
"""

import os
import threading
import time

import pytest

_opt_in = pytest.mark.skipif(
    os.environ.get("RUN_PREMIUM_LOCK_IT") != "1",
    reason="opt-in L3: set RUN_PREMIUM_LOCK_IT=1 with a real MySQL (RDS_* env)",
)


@pytest.fixture
def pm():
    # Imported lazily so collection (per-PR lambda lane) never needs a DB.
    # distributed_lock reads RDS_* / port at call time, not import time.
    import premium_manager

    return premium_manager


def _lock_name(suffix):
    # Suffix so the three tests in this process use distinct names.
    return f"ws5_{os.getpid()}_{suffix}"


def _lock_owner(pm, name):
    """Return the connection id currently holding GET_LOCK(name), or None if
    free, via an independent probe session (mirrors distributed_lock's connect).
    Positively distinguishes 'held by another session' from a NULL/error."""
    import pymysql

    host = os.environ["RDS_HOST"].split(":")[0]
    conn = pymysql.connect(
        host=host,
        port=pm.DatabaseConfig.DEFAULT_PORT,
        user=os.environ["RDS_USER"],
        password=os.environ["RDS_PASSWORD"],
        database=os.environ["RDS_DATABASE"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        ssl={"check_hostname": False},
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT IS_USED_LOCK(%s) AS owner", (name,))
            return cursor.fetchone()["owner"]
    finally:
        conn.close()


def test_opt_in_env_is_consistent():
    """Not skipped: if a DB is wired (RDS_HOST set) the opt-in flag must be set
    too, so a mis-propagated flag ERRORS here instead of the whole suite
    silently skipping and the harness reporting a hollow green."""
    if os.environ.get("RDS_HOST"):
        assert os.environ.get("RUN_PREMIUM_LOCK_IT") == "1", (
            "RDS_HOST is set but RUN_PREMIUM_LOCK_IT is not; the lock "
            "integration tests would silently skip"
        )


@_opt_in
def test_second_session_cannot_acquire_held_lock(pm):
    """A held GET_LOCK blocks a second (different-connection) session, and the
    name becomes acquirable again after release."""
    name = _lock_name("mutex")
    with pm.distributed_lock(name, timeout=5) as first:
        assert first is True
        # Positively confirm the lock is held by a session (rules out a NULL /
        # error result masquerading as contention below).
        assert _lock_owner(pm, name) is not None
        # distributed_lock opens a fresh connection per call, so this is a
        # distinct session; timeout=0 => immediate 0 while the first holds.
        with pm.distributed_lock(name, timeout=0) as second:
            assert second is False
    # First released on block exit; the name is free and acquirable again.
    assert _lock_owner(pm, name) is None
    with pm.distributed_lock(name, timeout=5) as third:
        assert third is True


@_opt_in
def test_concurrent_holders_are_serialized(pm):
    """Four real threads contending for one lock never hold it simultaneously,
    and all four complete their critical section (so the proof is not vacuous)."""
    name = _lock_name("serial")
    guard = threading.Lock()
    live = {"n": 0}
    max_live = {"n": 0}
    completed = {"n": 0}
    errors = []

    def worker():
        try:
            with pm.distributed_lock(name, timeout=30) as acquired:
                assert acquired is True
                with guard:
                    live["n"] += 1
                    max_live["n"] = max(max_live["n"], live["n"])
                time.sleep(0.2)  # hold the critical section so overlap would show
                with guard:
                    live["n"] -= 1
                    completed["n"] += 1
        except Exception as e:  # noqa: BLE001 - surface any thread failure
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors, errors
    assert not any(t.is_alive() for t in threads)
    assert completed["n"] == 4  # every thread actually entered the lock
    assert max_live["n"] == 1  # but never two at once


@_opt_in
def test_distinct_lock_names_do_not_block(pm):
    """Different lock names are independent, so different users' per-user locks
    (assign_user_<id>) do not serialize each other. Meaningful together with
    the mutual-exclusion test above (which rejects an always-grant lock)."""
    held = _lock_name("iso_a")
    other = _lock_name("iso_b")
    with pm.distributed_lock(held, timeout=5) as a:
        assert a is True
        with pm.distributed_lock(other, timeout=0) as b:
            assert b is True
