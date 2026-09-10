"""Differential concurrency test for the per-user assign lock (case 6204).

Runs the REAL ``assign_premium_user`` for a single user on two threads against
a shared in-memory fake DB and fake ELBv2. The only thing that differs between
the two test cases is the injected ``distributed_lock``:

  * a genuinely serializing per-name lock -> the second assign observes the
    first's stored assignment and short-circuits, so exactly one target group
    is created and none is deleted;
  * a no-op lock -> the two assigns interleave, the second's orphan-cleanup
    deletes the first's live target group, and the first's returned assignment
    is left pointing at a deleted target group (ALB routing corruption).

Pass/fail therefore flips solely on whether the lock serializes: the no-op
case reproduces the corruption, the serializing case does not. Pre-lock code
(assignment without the per-user lock) behaves like the no-op case and fails
the serializing-lock expectation.

Determinism: a barrier gates critical-section entry. When both threads are
concurrently inside the section (no-op lock) the barrier trips and
one-directional events script the exact create/delete interleaving with no
sleeps. When the lock serializes, the second thread cannot enter the section
while the first holds the lock, the barrier breaks, and the ordinary
single-threaded flow runs. The barrier's timeout is consumed only in the
serializing (passing) case, where no partner ever arrives.
"""

import json
import threading
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

from aws_constants import InstanceState
from conftest import setup_db_mock

USER_ID = 77
USER_UID = "uid_77"

# Window for both threads to reach the in-section barrier when they run
# concurrently. Consumed in full only by the serializing case, where the
# second thread is blocked on the lock and never arrives.
ENTRY_BARRIER_TIMEOUT = 3.0
# Generous ceiling for the pre-assign rendezvous; both threads always reach it.
START_GATE_TIMEOUT = 15.0


def _make_serializing_lock():
    """distributed_lock replacement that genuinely serializes same-name holders."""
    locks = {}
    guard = threading.Lock()

    @contextmanager
    def _lock(name, timeout=None):
        with guard:
            lk = locks.setdefault(name, threading.Lock())
        lk.acquire()
        try:
            yield True
        finally:
            lk.release()

    return _lock


def _make_noop_lock():
    """distributed_lock replacement that grants immediately with no exclusion."""

    @contextmanager
    def _lock(name, timeout=None):
        yield True

    return _lock


class _Coordinator:
    """Shared in-memory DB + ELBv2 state plus the interleaving script.

    A thread's role is assigned by the entry barrier: the two threads that
    cross it together become "first" and "second". If the barrier breaks
    (the threads were serialized by the lock) no role is assigned and the
    thread runs the ordinary flow.
    """

    def __init__(self):
        self._guard = threading.Lock()
        self._start_gate = threading.Barrier(2, timeout=START_GATE_TIMEOUT)
        self._entry = threading.Barrier(2, timeout=ENTRY_BARRIER_TIMEOUT)
        self._roles = {}

        self.second_has_read = threading.Event()
        self.first_tg_created = threading.Event()
        self.first_stored = threading.Event()

        self.db = {}  # user_id -> stored assignment row

        self._seq = 0
        self.live = {}  # target group arn -> name
        self.created = []  # arns, in creation order
        self.deleted = []  # arns passed to delete_target_group

    def _role(self):
        return self._roles.get(threading.get_ident())

    def wait_to_start(self):
        """Rendezvous both threads immediately before they call assign."""
        try:
            self._start_gate.wait()
        except threading.BrokenBarrierError:
            pass

    def on_impl_entry(self, user_id=None):
        """Patches restore_pending_release: rendezvous at critical-section start."""
        try:
            idx = self._entry.wait()
        except threading.BrokenBarrierError:
            return None
        with self._guard:
            self._roles[threading.get_ident()] = "first" if idx == 0 else "second"
        return None

    def on_read(self, user_id):
        """Patches get_existing_user_assignment: read the shared DB."""
        with self._guard:
            row = dict(self.db[user_id]) if user_id in self.db else None
        if self._role() == "second":
            self.second_has_read.set()
        return row

    def on_store(
        self,
        user_id,
        instance_id,
        target_group_arn,
        rule_arn,
        instance_state=None,
        is_shared=False,
        is_standby=False,
    ):
        """Patches store_user_assignment: write the shared DB, reject duplicates."""
        role = self._role()
        if role == "first":
            # Hold the write until the concurrent assign has taken its stale
            # read, so the second thread proceeds as if unassigned.
            self.second_has_read.wait()
        elif role == "second":
            # Let the first assign win the row so this one hits the duplicate.
            self.first_stored.wait()

        with self._guard:
            duplicate = user_id in self.db
            if not duplicate:
                self.db[user_id] = {
                    "user_id": user_id,
                    "instance_id": instance_id,
                    "target_group_arn": target_group_arn,
                    "alb_rule_arn": rule_arn,
                    "instance_state": instance_state or InstanceState.RUNNING,
                    "is_shared": 1 if is_shared else 0,
                    "status": "active",
                }

        if role == "first":
            self.first_stored.set()
        if duplicate:
            raise Exception(f"User {user_id} already has a premium assignment")


class _CoordElbv2:
    """Fake ELBv2 sharing the coordinator's target-group state."""

    def __init__(self, coord):
        self.c = coord

    def create_target_group(self, Name, **kwargs):
        c = self.c
        with c._guard:
            c._seq += 1
            arn = f"arn:tg/{Name}/{c._seq}"
            c.live[arn] = Name
            c.created.append(arn)
        if c._role() == "first":
            c.first_tg_created.set()
        return {"TargetGroups": [{"TargetGroupArn": arn}]}

    def describe_target_groups(self, Names=None, **kwargs):
        c = self.c
        if c._role() == "second":
            # The orphan-cleanup describe must run after the first assign has
            # created its target group, so it observes (and deletes) it.
            c.first_tg_created.wait()
        with c._guard:
            if Names:
                arns = [a for a, n in c.live.items() if n in Names]
            else:
                arns = list(c.live)
        return {"TargetGroups": [{"TargetGroupArn": a} for a in arns]}

    def delete_target_group(self, TargetGroupArn=None, **kwargs):
        with self.c._guard:
            self.c.deleted.append(TargetGroupArn)
            self.c.live.pop(TargetGroupArn, None)
        return {}

    def register_targets(self, **kwargs):
        return {}

    def delete_rule(self, **kwargs):
        return {}


def _run_variant(mock_env, lock_factory):
    """Run two concurrent assign_premium_user calls for one user.

    Returns (coordinator, results) where results maps thread tag -> the
    assign_premium_user return value, or the raised exception.
    """
    import premium_manager

    coord = _Coordinator()
    elbv2 = _CoordElbv2(coord)
    ec2 = MagicMock()
    ec2.describe_instances.return_value = {
        "Reservations": [{"Instances": [{"State": {"Name": InstanceState.RUNNING}}]}]
    }

    def boto3_client(service):
        if service == "elbv2":
            return elbv2
        if service == "ec2":
            return ec2
        return MagicMock()

    results = {}

    def worker(tag):
        coord.wait_to_start()
        try:
            results[tag] = premium_manager.assign_premium_user(
                USER_ID, {"tier": "premium"}, USER_UID
            )
        except Exception as exc:  # noqa: BLE001 - surface any failure to the test
            results[tag] = exc

    with ExitStack() as stack:
        stack.enter_context(patch.dict("os.environ", mock_env))

        def stub(name, **kwargs):
            stack.enter_context(patch.object(premium_manager, name, **kwargs))

        # Coordination hooks on the real assignment path.
        stub("restore_pending_release", side_effect=coord.on_impl_entry)
        stub("get_existing_user_assignment", side_effect=coord.on_read)
        stub("store_user_assignment", side_effect=coord.on_store)

        # Leaf DB/AWS helpers faked so the real impl reaches target-group work.
        stub("register_orphaned_stopped_instances")
        stub(
            "get_all_premium_instances_with_states",
            return_value=[{"instance_id": "i-run", "state": InstanceState.RUNNING}],
        )
        stub("count_active_premium_users", return_value=0)
        stub("get_available_standby_instances", return_value=[])
        stub("check_instance_readiness_with_retry", return_value=True)
        stub("get_assigned_users_for_instance", return_value=[])
        stub("try_reserve_instance", return_value=True)
        stub("target_group_exists", return_value=True)
        stub("_enable_sticky_sessions")
        stub("_ensure_premium_tg_unhealthy_alarm")
        stub("_delete_premium_tg_unhealthy_alarm")
        stub("cleanup_duplicate_rules_for_routing_id", return_value=0)
        stub("create_alb_rule", return_value={"Rules": [{"RuleArn": "arn:rule"}]})
        stub("update_user_activity", return_value=True)
        stub("invoke_migration_async")
        stub("scale_premium_instances_if_needed", return_value=False)

        stack.enter_context(
            patch(
                "premium_manager.pymysql.connect",
                side_effect=lambda *a, **k: setup_db_mock(),
            )
        )
        stack.enter_context(
            patch("premium_manager.distributed_lock", new=lock_factory())
        )
        stack.enter_context(patch("boto3.client", side_effect=boto3_client))

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

    assert not t1.is_alive() and not t2.is_alive(), "assign threads did not finish"
    return coord, results


def test_serializing_lock_yields_single_target_group(mock_env_vars_premium):
    """With a genuinely serializing lock the second assign short-circuits, so
    exactly one target group survives and nothing is orphaned."""
    coord, results = _run_variant(mock_env_vars_premium, _make_serializing_lock)

    assert all(isinstance(r, dict) for r in results.values()), results
    statuses = sorted(r["statusCode"] for r in results.values())
    assert statuses == [200, 200], results
    sources = sorted(
        json.loads(r["body"])["assignment_source"] for r in results.values()
    )
    assert sources == ["dedicated", "existing"], sources

    assert len(coord.created) == 1, coord.created
    assert coord.deleted == [], coord.deleted
    assert list(coord.live) == coord.created


def test_noop_lock_orphans_first_target_group(mock_env_vars_premium):
    """With a no-op lock the two assigns race: both create a target group and
    the second's orphan-cleanup deletes the first's live one, leaving the first
    assignment pointing at a deleted target group."""
    coord, results = _run_variant(mock_env_vars_premium, _make_noop_lock)

    # Both assigns took the fresh path and created a target group.
    assert len(coord.created) == 2, coord.created
    first_tg = coord.created[0]

    # The first assign's target group was deleted by the second's cleanup.
    assert first_tg in coord.deleted, (first_tg, coord.deleted)
    assert first_tg not in coord.live

    # One assign returned success referencing the now-deleted target group;
    # the other collided on the shared row.
    dicts = [r for r in results.values() if isinstance(r, dict)]
    excs = [r for r in results.values() if isinstance(r, Exception)]
    assert len(dicts) == 1 and len(excs) == 1, results
    winner = dicts[0]
    assert winner["statusCode"] == 200, winner
    assert json.loads(winner["body"])["target_group_arn"] == first_tg
    assert first_tg in coord.deleted
