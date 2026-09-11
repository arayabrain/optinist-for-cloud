#!/usr/bin/env python3
"""Delete the throwaway Firebase accounts left behind by interrupted e2e runs.

The e2e suite registers accounts named `<prefix>_<Date.now()>@test.com` and
removes them in its teardown. A run that dies first leaves the Firebase user
behind with no DB row, putting it out of reach of any DB-driven cleanup.

Lives here rather than under infrastructure/scripts so it stays out of the
shipped image: the production Dockerfile copies that directory wholesale, and
this deletes by pattern rather than from a list.

Run from the repo root inside the backend container:
    poetry run python .github/scripts/sweep_e2e_firebase_users.py
"""

import re
import time

import firebase_admin
from firebase_admin import auth, credentials

FIREBASE_PRIVATE_PATH = "studio/config/auth/firebase_private.json"

# The 13-digit Date.now() suffix every throwaway carries. The fixed accounts
# (e2e_ci_free, e2e_local_admin) have none, so they can never match.
THROWAWAY = re.compile(r"e2e_[a-z_]+_([0-9]{13})@test\.com")

# One Firebase project is shared by CI and local runs, so a concurrent suite
# still needs whatever it registered. Covers playwright.config's 165-minute
# globalTimeout, the longest a run can hold an account open.
GRACE_MS = 4 * 60 * 60 * 1000

DELETE_BATCH = 1000  # firebase_admin's per-call cap for delete_users


def stale_uids(users, now_ms):
    cutoff = now_ms - GRACE_MS
    return [
        u.uid
        for u in users
        if (m := THROWAWAY.fullmatch(u.email or "")) and int(m.group(1)) < cutoff
    ]


def sweep(auth_module, users, now_ms):
    uids = stale_uids(users, now_ms)
    deleted, errors = 0, []
    for i in range(0, len(uids), DELETE_BATCH):
        # delete_users reports per-uid failures in its result instead of raising
        result = auth_module.delete_users(uids[i : i + DELETE_BATCH])
        deleted += result.success_count
        errors += [e.reason for e in result.errors]
    if errors:
        raise RuntimeError(f"deleted {deleted}, {len(errors)} failed: {errors}")
    return deleted


def main():
    firebase_admin.initialize_app(credentials.Certificate(FIREBASE_PRIVATE_PATH))
    print(sweep(auth, auth.list_users().iterate_all(), time.time() * 1000))


if __name__ == "__main__":
    main()
