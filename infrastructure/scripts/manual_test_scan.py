#!/usr/bin/env python3
"""Read-only scan of a deployed environment for manual-test sheets 09 and 20.

Automates the rows that are pure observations: the sheet-20 DB integrity
audits (2008-2024), the sheet-09 Stripe catalog / customer / subscription
reads (901-936), the CloudWatch background-job metrics (2027) and the public
no-auth reproduce API (2028). Writes a markdown report with one
PASS / FAIL / INFO / SKIP line per sheet row.

Access paths (all read-only):
- SQL runs on an in-VPC instance via SSM Run Command; the instance fetches
  the DB credentials from Secrets Manager itself, so nothing secret transits
  this machine or the SSM history.
- Stripe is read with the environment's own key from Secrets Manager, GET
  requests only. A live-mode key is refused unless --allow-live is passed.

Usage:
  python3 manual_test_scan.py                        # development check
  python3 manual_test_scan.py --check production     # strict catalog, live key
  python3 manual_test_scan.py --cases release        # report keyed by the
                                                     # release-sheet BT rows
  python3 manual_test_scan.py --verbose -o report.md # per-row evidence blocks
                                                     # (query/API call + its
                                                     # output) for the sheet's
                                                     # Evidence column

The development check accepts the deliberate dev catalog (the DB Premium plan
links to Stripe product "Premium Plan Test" with a day-interval price so
renewals can be exercised daily); the production check requires the monthly
catalog and fails on any drift.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

REGION = "ap-northeast-1"
# The 5-minute sync job publishes these on every run, so their absence is a
# real fault.
EXPECTED_METRICS = [
    "ExperimentsSynced",
    "SyncErrors",
    "SyncErrorRate",
]
# The cleanup job returns early when no user is eligible, without publishing, so
# on a quiet environment these fall outside list-metrics' lookback and their
# absence means nothing. Reported, never failed on.
OPTIONAL_METRICS = [
    "DataCleanupCount",
    "CleanupErrors",
    "CleanupKept",
]
CURRENCY = {1: "usd", 2: "jpy"}

# Release-sheet rows this scan evidences, each built from the system-sheet
# checks named on its Scripted note. --cases release relabels the report with
# these BT numbers; everything else on the release sheets stays manual / e2e.
RELEASE_MAP = [
    ("09 Subscription Registration", "BT-909", ["2018", "2009"]),
    ("09 Subscription Registration", "BT-910", ["2016"]),
    ("09 Subscription Registration", "BT-911", ["930", "931"]),
    ("09 Subscription Registration", "BT-912", ["2013", "2016"]),
    ("09 Subscription Registration", "BT-913", ["932"]),
    ("09 Subscription Registration", "BT-914", ["934", "923"]),
    ("09 Subscription Registration", "BT-923", ["920", "921"]),
    ("11 AWS Monitoring", "BT-1108", ["2027"]),
    ("11 AWS Monitoring", "BT-1110", ["2028"]),
]


def aws(*args):
    out = subprocess.run(
        ["aws", *args, "--region", REGION, "--output", "json"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(
            f"aws {args[0]} {args[1]} failed: {out.stderr.strip()[:300]}"
        )
    return json.loads(out.stdout) if out.stdout.strip() else {}


def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            body = {}
        return e.code, body
    except Exception as e:
        return 0, {"_error": str(e)[:200]}


def stripe_get(key, path, **params):
    url = "https://api.stripe.com" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    status, body = http_get(url, {"Authorization": "Bearer " + key})
    if status != 200:
        body["_status"] = status
    return body


def taskdef_env(env):
    td = aws(
        "ecs", "describe-task-definition", "--task-definition", f"{env}-cloud-taskdef"
    )
    pairs = td["taskDefinition"]["containerDefinitions"][0]["environment"]
    return {p["name"]: p["value"] for p in pairs}


def find_ssm_instance(env):
    res = aws(
        "ec2",
        "describe-instances",
        "--filters",
        f"Name=tag:Name,Values={env}-asg-instance",
        "Name=instance-state-name,Values=running",
    )
    ids = [i["InstanceId"] for r in res["Reservations"] for i in r["Instances"]]
    if not ids:
        raise RuntimeError(f"no running instance tagged Name={env}-asg-instance")
    info = aws(
        "ssm",
        "describe-instance-information",
        "--filters",
        f"Key=InstanceIds,Values={','.join(ids)}",
    )
    online = [
        i["InstanceId"]
        for i in info["InstanceInformationList"]
        if i["PingStatus"] == "Online"
    ]
    if not online:
        raise RuntimeError(f"instances {ids} are not Online in SSM")
    return online[0]


def audit_queries(user_id, user_email=None):
    if user_id:
        uid = str(user_id)
    elif user_email:
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", user_email):
            raise SystemExit(f"--user-email is not a plain address: {user_email}")
        # Pinned regardless of plan or status: an audit that retargets when the
        # named account's subscription breaks reports on a different account
        # instead of the failure.
        uid = (
            "(SELECT su.user_id FROM subscription_users su"
            " JOIN users u ON u.id=su.user_id"
            f" WHERE u.active=1 AND u.email='{user_email}'"
            " ORDER BY su.updated_at DESC LIMIT 1)"
        )
    else:
        uid = (
            "(SELECT su.user_id FROM subscription_users su"
            " JOIN users u ON u.id=su.user_id"
            " JOIN subscription_user_accounts sua ON sua.user_id=su.user_id"
            # An e2e checkout probe leaves the freshest subscription of all, so
            # without this the audit retargets onto a throwaway account.
            " WHERE su.plan_id=2 AND u.active=1 AND u.email NOT LIKE 'e2e%'"
            " ORDER BY su.updated_at DESC LIMIT 1)"
        )
    return [
        (
            "plans",
            "SELECT id,name,price,billing_cycle,currency,status,"
            "IFNULL(stripe_product_id,''),IFNULL(stripe_price_id,'')"
            " FROM subscription_plans ORDER BY id",
        ),
        (
            "orphan_su",
            "SELECT COUNT(*) FROM subscription_users su"
            " LEFT JOIN users u ON su.user_id=u.id WHERE u.id IS NULL",
        ),
        (
            "orphan_sua",
            "SELECT COUNT(*) FROM subscription_user_accounts sua"
            " LEFT JOIN users u ON sua.user_id=u.id WHERE u.id IS NULL",
        ),
        (
            "dup_premium",
            "SELECT COUNT(*) FROM (SELECT user_id FROM subscription_users"
            " WHERE plan_id=2 GROUP BY user_id HAVING COUNT(*)>1) d",
        ),
        (
            "null_su",
            "SELECT COUNT(*) FROM subscription_users WHERE plan_id IS NULL"
            " OR user_id IS NULL OR expiration IS NULL",
        ),
        (
            "null_sua",
            "SELECT COUNT(*) FROM subscription_user_accounts"
            " WHERE user_id IS NULL OR provider_id IS NULL"
            " OR provider_customer_id IS NULL OR provider_customer_id=''",
        ),
        (
            "plan_ids",
            "SELECT plan_id, COUNT(*) FROM subscription_users"
            " GROUP BY plan_id ORDER BY plan_id",
        ),
        (
            "bad_ts",
            "SELECT COUNT(*) FROM subscription_users"
            " WHERE created_at > updated_at OR created_at > NOW()",
        ),
        (
            "exp_before_created",
            "SELECT COUNT(*) FROM subscription_users WHERE expiration < created_at",
        ),
        ("_uid", f"SET @uid := {uid}"),
        (
            "user",
            "SELECT u.id, u.email, IFNULL(u.name,''), su.plan_id, su.expiration,"
            " IFNULL(su.sync_status,''), su.scheduled_downgrade, su.created_at,"
            " su.updated_at, IFNULL(sua.provider_customer_id,'')"
            " FROM users u JOIN subscription_users su ON su.user_id=u.id"
            " LEFT JOIN subscription_user_accounts sua ON sua.user_id=u.id"
            " WHERE u.id=@uid",
        ),
        (
            "purchases",
            "SELECT id, plan_id, created_at FROM subscription_user_purchases"
            " WHERE user_id=@uid ORDER BY created_at DESC LIMIT 3",
        ),
        (
            "pub_exp",
            "SELECT workspace_id, uid FROM experiment_records"
            " WHERE publish_status=1 ORDER BY updated_at DESC LIMIT 1",
        ),
        (
            "priv_exp",
            "SELECT workspace_id, uid FROM experiment_records"
            " WHERE publish_status=0 ORDER BY updated_at DESC LIMIT 1",
        ),
    ]


def build_sql(queries):
    parts = []
    for tag, sql in queries:
        if not tag.startswith("_"):
            parts.append(f"SELECT '@@{tag}';")
        parts.append(f"{sql};")
    return "\n".join(parts) + "\n"


def ssm_sql(env, instance_id, db_host, sql):
    def jq(field):
        return f"\"import json,sys; print(json.load(sys.stdin)['{field}'])\""

    remote_lines = [
        "set -e",
        f"CFG=$(aws secretsmanager get-secret-value --region {REGION}"
        f" --secret-id {env}/database/config"
        " --query SecretString --output text)",
        f'DBUSER=$(printf %s "$CFG" | python3 -c {jq("username")})',
        f'DBNAME=$(printf %s "$CFG" | python3 -c {jq("database")})',
        f'export MYSQL_PWD=$(printf %s "$CFG" | python3 -c {jq("password")})',
        "cat > /tmp/mts_scan.sql <<'MTSEOF'",
        *sql.splitlines(),
        "MTSEOF",
        f'mariadb --ssl -h {db_host} -u "$DBUSER" --connect-timeout=10'
        ' -N -B "$DBNAME" < /tmp/mts_scan.sql',
        "rm -f /tmp/mts_scan.sql",
    ]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"commands": remote_lines}, f)
        params_path = f.name
    try:
        cmd = aws(
            "ssm",
            "send-command",
            "--instance-ids",
            instance_id,
            "--document-name",
            "AWS-RunShellScript",
            "--comment",
            "manual-test read-only scan (sheets 09/20)",
            "--parameters",
            f"file://{params_path}",
        )["Command"]["CommandId"]
    finally:
        os.unlink(params_path)
    inv = {}
    for _ in range(60):
        time.sleep(3)
        inv = aws(
            "ssm",
            "get-command-invocation",
            "--command-id",
            cmd,
            "--instance-id",
            instance_id,
        )
        if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
            break
    if inv.get("Status") != "Success":
        raise RuntimeError(
            f"SSM SQL command {inv.get('Status')}:"
            f" {inv.get('StandardErrorContent', '')[:400]}"
        )
    return inv["StandardOutputContent"]


def parse_sections(text):
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith("@@"):
            current = line[2:]
            sections[current] = []
        elif current is not None and line.strip():
            sections[current].append(line.split("\t"))
    return sections


def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def inv_tax(i):
    # invoice.tax/total_tax_amounts became total_taxes in newer Stripe APIs
    return i.get("tax") or sum(
        t.get("amount", 0)
        for t in i.get("total_taxes") or i.get("total_tax_amounts") or []
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--check",
        choices=("development", "production"),
        default="development",
        help="environment profile: resource prefix, catalog strictness,"
        " Stripe key policy and default base URL",
    )
    ap.add_argument(
        "--cases",
        choices=("system", "release"),
        default="system",
        help="which sheet's row numbers the report uses: system (sheets 09/20)"
        " or release (the BT rows this scan evidences)",
    )
    ap.add_argument("--env", help="resource name prefix (default: from --check)")
    ap.add_argument(
        "--user-id",
        type=int,
        help="target user id (default: newest active premium user"
        " with a Stripe account)",
    )
    ap.add_argument(
        "--user-email",
        help="target user by address, so the audit stays on one account"
        " instead of following whichever subscription was updated last"
        " (--user-id wins)",
    )
    ap.add_argument(
        "--base-url",
        help="deployed app base URL for the 2028 probe (default: from --check)",
    )
    ap.add_argument(
        "--allow-live",
        action="store_true",
        help="permit a live-mode Stripe key (still read-only;"
        " implied by --check production)",
    )
    ap.add_argument(
        "--allow-test-interval",
        action="store_true",
        help="accept a non-month billing interval on the Stripe price"
        " (implied by --check development)",
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="add a per-row evidence block to the report: the query or API"
        " call that ran and its output, ready to paste into the sheet's"
        " Evidence column",
    )
    ap.add_argument(
        "-o", "--out", default="manual_test_scan_report.md", help="report path"
    )
    ap.add_argument(
        "--json",
        metavar="PATH",
        help="also write {row: {status, evidence}} as JSON, for a caller that"
        " asserts per row instead of reading the report",
    )
    args = ap.parse_args()
    prod = args.check == "production"
    env = args.env or ("subscr-optinist" if prod else "development-optinist")
    allow_test_interval = args.allow_test_interval or not prod
    allow_live = args.allow_live or prod
    # Production's newest premium account is a paying customer, and auditing one
    # reports their billing history as release results. Keyed on the environment
    # actually read rather than on --check, since --env alone can point every
    # query at production. `is None` because --user-id 0 is falsy and would
    # otherwise fall through to the unpinned query.
    if (
        (prod or env.startswith("subscr"))
        and args.user_id is None
        and not args.user_email
    ):
        raise SystemExit(
            f"auditing {env} needs --user-email or --user-id: name the release"
            " test account rather than auditing a real customer"
        )

    results = []

    def add(sheet, row, status, evidence, detail=None):
        results.append((sheet, row, status, evidence, detail or []))
        print(f"  [{status:4}] {row}: {evidence[:110]}")

    print(f"check={args.check} env={env} region={REGION}")
    tenv = taskdef_env(env)
    db_host = tenv.get("DB_HOST", "").split(":")[0]
    env_prefix = tenv.get("ENV_PREFIX", "default")
    namespace = f"OptiNiSt/BackgroundJobs/{env_prefix}"

    print("running SQL audits over SSM ...")
    instance = find_ssm_instance(env)
    queries = audit_queries(args.user_id, args.user_email)
    qtext = dict(queries)
    db = parse_sections(ssm_sql(env, instance, db_host, build_sql(queries)))

    def sqld(*tags):
        lines = []
        for t in tags:
            lines.append(f"mysql> {qtext[t]};")
            lines += ["  " + " | ".join(r) for r in db.get(t, [])] or ["  (0 rows)"]
        return lines

    def count(tag):
        return int(db[tag][0][0]) if db.get(tag) else -1

    add(
        "20",
        "2026",
        "PASS",
        f"DB reachable read-only (mariadb --ssl via SSM on {instance});"
        " all audit queries answered",
        [
            f"# mariadb --ssl over SSM Run Command on {instance} (host {db_host});",
            f"# creds fetched on-instance from Secrets Manager"
            f" {env}/database/config",
            f"-> {len(db)} audit result sections returned",
        ],
    )
    add(
        "20",
        "2008",
        "PASS" if count("orphan_su") == 0 else "FAIL",
        f"orphaned subscription_users rows: {count('orphan_su')}",
        sqld("orphan_su"),
    )
    add(
        "20",
        "2009",
        "PASS" if count("dup_premium") == 0 else "FAIL",
        f"users with >1 Premium row: {count('dup_premium')}",
        sqld("dup_premium"),
    )
    nulls = (count("null_su"), count("null_sua"))
    add(
        "20",
        "2010",
        "PASS" if nulls == (0, 0) else "FAIL",
        f"NULL required fields: subscription_users={nulls[0]},"
        f" subscription_user_accounts={nulls[1]}",
        sqld("null_su", "null_sua"),
    )
    integ = (
        count("orphan_su"),
        count("orphan_sua"),
        count("dup_premium"),
        nulls[0],
        nulls[1],
    )
    add(
        "20",
        "2012",
        "PASS" if set(integ) == {0} else "FAIL",
        f"orphans(su,sua)/dups/nulls(su,sua) = {integ}",
        sqld("orphan_su", "orphan_sua", "dup_premium", "null_su", "null_sua"),
    )
    plan_ids = {int(r[0]): int(r[1]) for r in db.get("plan_ids", [])}
    add(
        "20",
        "2024",
        "PASS" if set(plan_ids) <= {1, 2} else "FAIL",
        f"plan_id distribution: {plan_ids}",
        sqld("plan_ids"),
    )
    add(
        "20",
        "2023",
        "PASS" if count("bad_ts") == 0 else "FAIL",
        f"created_at>updated_at or in future: {count('bad_ts')};"
        f" expiration<created_at: {count('exp_before_created')}"
        " (sheet allows test accounts here)",
        sqld("bad_ts", "exp_before_created"),
    )

    if not db.get("user"):
        add(
            "20",
            "2018",
            "SKIP",
            "no active premium user with a Stripe account found; pass --user-id",
        )
        user = None
    else:
        u = db["user"][0]
        user = {
            "id": int(u[0]),
            "email": u[1],
            "name": u[2],
            "plan_id": int(u[3]),
            "expiration": u[4],
            "sync_status": u[5],
            "scheduled_downgrade": u[6],
            "created_at": u[7],
            "updated_at": u[8],
            "cus_id": u[9],
        }
        print(f"target user: id={user['id']} ({user['email']})")
        exp = parse_dt(user["expiration"])
        problems = []
        if user["plan_id"] not in (1, 2):
            problems.append(f"plan_id={user['plan_id']}")
        if user["plan_id"] == 2 and exp < datetime.now(timezone.utc):
            problems.append(f"expiration in the past: {user['expiration']}")
        if user["sync_status"] != "synced":
            problems.append(f"sync_status={user['sync_status']}")
        if parse_dt(user["created_at"]) > parse_dt(user["updated_at"]):
            problems.append("created_at > updated_at")
        add(
            "20",
            "2018",
            "PASS" if not problems else "FAIL",
            f"user {user['id']}: plan_id={user['plan_id']},"
            f" expiration={user['expiration']},"
            f" sync_status={user['sync_status']},"
            f" scheduled_downgrade={user['scheduled_downgrade']}"
            + (f"; problems: {problems}" if problems else ""),
            sqld("user"),
        )
        purchases = db.get("purchases", [])
        add(
            "09",
            "917",
            "PASS" if purchases else "FAIL",
            f"latest subscription_user_purchases for user {user['id']}: "
            + (
                f"id={purchases[0][0]} plan_id={purchases[0][1]}"
                f" created={purchases[0][2]}"
                if purchases
                else "none"
            ),
            sqld("purchases"),
        )

    print("reading Stripe (GET only) ...")
    secret = json.loads(
        aws(
            "secretsmanager", "get-secret-value", "--secret-id", f"{env}/stripe/config"
        )["SecretString"]
    )
    key = secret["secret_key"]
    if key.startswith("sk_live") and not allow_live:
        add(
            "09",
            "901-936",
            "SKIP",
            "live-mode Stripe key detected; re-run with --allow-live"
            " or --check production to scan read-only",
        )
        key = None

    if key:
        plans = [
            {
                "id": int(r[0]),
                "name": r[1],
                "price": int(r[2]),
                "cycle": int(r[3]),
                "currency": CURRENCY.get(int(r[4]), r[4]),
                "product": r[6],
                "price_id": r[7],
            }
            for r in db.get("plans", [])
        ]
        products = stripe_get(key, "/v1/products", limit=100, active="true").get(
            "data", []
        )
        by_prod = {p["id"]: p for p in products}
        linked_ok = [
            (
                f"{p['name']} -> {by_prod[p['product']]['name']}"
                if p["product"] in by_prod
                else f"{p['name']} -> {p['product']} NOT ACTIVE"
            )
            for p in plans
            if p["product"]
        ]
        add(
            "09",
            "901",
            (
                "PASS"
                if linked_ok and all("NOT ACTIVE" not in e for e in linked_ok)
                else "FAIL"
            ),
            "DB plans resolve to active products:"
            f" {'; '.join(linked_ok) or 'no linked plans'}"
            f" (all active products: {sorted(p['name'] for p in products)})",
            ["GET https://api.stripe.com/v1/products?active=true&limit=100"]
            + [f"  active products: {sorted(p['name'] for p in products)}"]
            + sqld("plans")
            + [f"  {e}" for e in linked_ok],
        )

        prices = stripe_get(key, "/v1/prices", limit=100).get("data", [])
        by_id = {p["id"]: p for p in prices}
        linked = [plan for plan in plans if plan["price_id"]]
        p902, p903, p904, p908, price_calls = [], [], [], [], []
        for plan in linked:
            sp = by_id.get(plan["price_id"]) or stripe_get(
                key, f"/v1/prices/{plan['price_id']}"
            )
            sprod = (
                stripe_get(key, f"/v1/products/{plan['product']}")
                if plan["product"]
                else {}
            )
            price_calls.append(
                f"GET /v1/prices/{plan['price_id']} ;"
                f" GET /v1/products/{plan['product']}  ({plan['name']})"
            )
            p904.append(
                f"{plan['name']}: product"
                f" {'active' if sprod.get('active') else 'MISSING/inactive'},"
                f" price {'active' if sp.get('active') else 'MISSING/inactive'}"
            )
            interval = (sp.get("recurring") or {}).get("interval")
            interval_ok = interval == "month" or allow_test_interval
            p902.append(
                f"{plan['name']}: {sp.get('unit_amount')} {sp.get('currency')}"
                f"/{interval}"
                + (
                    ""
                    if interval == "month"
                    else (
                        " (non-month interval accepted)"
                        if interval_ok
                        else " (not monthly)"
                    )
                )
            )
            mismatches = []
            if plan["name"].lower() not in (sprod.get("name") or "").lower():
                mismatches.append(
                    f"name {sprod.get('name')!r} does not contain {plan['name']!r}"
                )
            if sp.get("unit_amount") != plan["price"]:
                mismatches.append(f"amount {sp.get('unit_amount')} != {plan['price']}")
            if sp.get("currency") != plan["currency"]:
                mismatches.append(
                    f"currency {sp.get('currency')} != {plan['currency']}"
                )
            if plan["cycle"] == 1 and not interval_ok:
                mismatches.append(f"interval {interval} != month")
            p903.append(
                f"{plan['name']}:"
                f" {'match' if not mismatches else '; '.join(mismatches)}"
            )
            p908.append(f"{plan['name']}: tax_behavior={sp.get('tax_behavior')}")
        if linked:

            def pdetail(outputs):
                return price_calls + [f"  {e}" for e in outputs]

            add(
                "09",
                "902",
                (
                    "PASS"
                    if all("None" not in e and "not monthly" not in e for e in p902)
                    else "FAIL"
                ),
                "; ".join(p902),
                pdetail(p902),
            )
            add(
                "09",
                "903",
                "PASS" if all(e.endswith("match") for e in p903) else "FAIL",
                "; ".join(p903),
                pdetail(p903) + sqld("plans"),
            )
            add(
                "09",
                "904",
                "PASS" if "MISSING" not in " ".join(p904) else "FAIL",
                "; ".join(p904),
                pdetail(p904),
            )
            add(
                "09",
                "908",
                "PASS" if all(e.endswith("unspecified") for e in p908) else "INFO",
                "; ".join(p908) + " (unspecified = dashboard 'Default')",
                pdetail(p908),
            )
        else:
            add(
                "09",
                "902-904/908",
                "SKIP",
                "no subscription_plans row carries a stripe_price_id",
            )

        regs = stripe_get(key, "/v1/tax/registrations", status="active").get("data", [])
        countries = [r.get("country") for r in regs]
        add(
            "09",
            "907",
            "PASS" if "JP" in countries else "FAIL",
            f"active tax registrations: {countries or 'none'}",
            ["GET https://api.stripe.com/v1/tax/registrations?status=active"]
            + [
                f"  {r.get('country')}: status={r.get('status')} active_from="
                f"{iso(r['active_from']) if r.get('active_from') else '?'}"
                for r in regs
            ],
        )

        hooks = stripe_get(key, "/v1/webhook_endpoints").get("data", [])
        ok_hook = [
            h
            for h in hooks
            if h.get("status") == "enabled"
            and (
                "checkout.session.completed" in h.get("enabled_events", [])
                or "*" in h.get("enabled_events", [])
            )
        ]
        add(
            "09",
            "920",
            "PASS" if ok_hook else "FAIL",
            "enabled endpoints listening to checkout.session.completed:"
            f" {[h['url'] for h in ok_hook] or 'none'}"
            " (delivery log stays a dashboard check)",
            ["GET https://api.stripe.com/v1/webhook_endpoints"]
            + [
                f"  {h['url']}: status={h.get('status')}"
                f" enabled_events={len(h.get('enabled_events', []))}"
                " (checkout.session.completed"
                f" {'included' if h in ok_hook else 'NOT included'})"
                for h in hooks
            ],
        )

        ck_events = stripe_get(
            key, "/v1/events", type="checkout.session.completed", limit=3
        ).get("data", [])
        ck_lines = [
            "GET https://api.stripe.com/v1/events"
            "?type=checkout.session.completed&limit=3"
        ] + [
            f"  {e['id']} at {iso(e['created'])}:"
            f" amount_total={e['data']['object'].get('amount_total')}"
            " amount_tax="
            f"{(e['data']['object'].get('total_details') or {}).get('amount_tax')}"
            for e in ck_events
        ]
        if ck_events:
            sess = ck_events[0]["data"]["object"]
            tax = (sess.get("total_details") or {}).get("amount_tax")
            add(
                "09",
                "921",
                "PASS" if tax is not None else "FAIL",
                f"latest checkout.session.completed: total_details.amount_tax={tax}",
                ck_lines,
            )
            inv_id = sess.get("invoice")
            if inv_id and tax is not None:
                inv = stripe_get(key, f"/v1/invoices/{inv_id}")
                add(
                    "09",
                    "924",
                    "PASS" if inv_tax(inv) == (tax or 0) else "FAIL",
                    f"session amount_tax={tax} vs invoice tax={inv_tax(inv)}"
                    f" ({inv_id})",
                    ck_lines
                    + [
                        f"GET https://api.stripe.com/v1/invoices/{inv_id}",
                        f"  invoice {inv.get('number')}: total={inv.get('total')}"
                        f" tax={inv_tax(inv)} created="
                        f"{iso(inv['created']) if inv.get('created') else '?'}",
                    ],
                )
            else:
                add(
                    "09",
                    "924",
                    "INFO",
                    "latest session has no invoice or no tax to compare",
                    ck_lines,
                )
        else:
            add(
                "09",
                "921/924",
                "INFO",
                "no checkout.session.completed events in Stripe's 30-day window",
                ck_lines,
            )

        if user and user["cus_id"]:
            cus_id = user["cus_id"]
            found = stripe_get(key, "/v1/customers", email=user["email"], limit=10).get(
                "data", []
            )
            found_lines = [
                "GET https://api.stripe.com/v1/customers"
                f"?email={user['email']}&limit=10"
            ] + [f"  {c['id']} created {iso(c['created'])}" for c in found]
            add(
                "09",
                "927",
                "PASS" if len(found) == 1 else "FAIL",
                f"customers with email {user['email']}: {len(found)}"
                f" ({[c['id'] for c in found]})",
                found_lines,
            )
            add(
                "20",
                "2013",
                "PASS" if len(found) == 1 and found[0]["id"] == cus_id else "FAIL",
                f"Stripe search count={len(found)},"
                f" DB provider_customer_id={cus_id}",
                found_lines
                + [f"DB subscription_user_accounts.provider_customer_id = {cus_id}"],
            )

            cus = stripe_get(key, f"/v1/customers/{cus_id}")
            meta_uid = (cus.get("metadata") or {}).get("user_id")
            default_pm = (cus.get("invoice_settings") or {}).get(
                "default_payment_method"
            )
            cus_lines = [
                f"GET https://api.stripe.com/v1/customers/{cus_id}",
                f"  id={cus.get('id')} email={cus.get('email')}"
                f" name={cus.get('name')} metadata.user_id={meta_uid}"
                f" default_pm={'set' if default_pm else 'none'}"
                f" deleted={bool(cus.get('deleted'))}",
            ]
            match_id = cus.get("id") == cus_id and not cus.get("deleted")
            add(
                "09",
                "929",
                "PASS" if match_id else "FAIL",
                f"GET /v1/customers/{cus_id} resolves: {match_id}",
                cus_lines,
            )
            add(
                "20",
                "2016",
                "PASS" if match_id else "FAIL",
                f"provider_customer_id {cus_id} == Stripe customer id: {match_id}",
                cus_lines
                + [f"DB subscription_user_accounts.provider_customer_id = {cus_id}"],
            )
            add(
                "09",
                "928",
                (
                    "PASS"
                    if cus.get("email") == user["email"]
                    and str(meta_uid) == str(user["id"])
                    else "FAIL"
                ),
                f"email={cus.get('email')}, name={cus.get('name')},"
                f" metadata.user_id={meta_uid},"
                f" default_pm={'set' if default_pm else 'none'}",
                cus_lines,
            )

            # Rows 914 / 919: the billing address Stripe collected on the
            # session (billing_address_collection=required, row 910) has to end
            # up on the customer, or the tax it charged rests on nothing.
            addr = cus.get("address") or {}
            addr_fields = [f for f in ("country", "postal_code") if addr.get(f)]
            add(
                "09",
                "914",
                "PASS" if addr.get("country") else "FAIL",
                f"customer address: {addr_fields or 'absent'}"
                + (f", country={addr.get('country')}" if addr.get("country") else ""),
                cus_lines + [f"  address={ {k: v for k, v in addr.items() if v} }"],
            )
            add(
                "09",
                "919",
                "PASS" if addr.get("country") else "FAIL",
                f"address readable via the API: {bool(addr.get('country'))}"
                " (its rendering in the dashboard stays manual)",
                cus_lines,
            )

            subs = stripe_get(
                key, "/v1/subscriptions", customer=cus_id, status="all", limit=10
            ).get("data", [])
            active = [s for s in subs if s["status"] in ("active", "trialing")]
            want = 1 if user["plan_id"] == 2 else 0
            price_ids = [i["price"]["id"] for s in active for i in s["items"]["data"]]
            sub_lines = [
                "GET https://api.stripe.com/v1/subscriptions"
                f"?customer={cus_id}&status=all&limit=10"
            ] + [
                f"  {s['id']}: status={s['status']}"
                f" cancel_at_period_end={s.get('cancel_at_period_end')}"
                f" prices={[i['price']['id'] for i in s['items']['data']]}"
                for s in subs
            ]
            add(
                "09",
                "930",
                "PASS" if len(active) == want else "FAIL",
                f"subscriptions: {[(s['id'], s['status']) for s in subs]};"
                f" active price ids: {price_ids}",
                sub_lines,
            )
            add(
                "20",
                "2014",
                "PASS" if len(active) <= 1 else "FAIL",
                f"active/trialing subscriptions: {len(active)} of {len(subs)} total",
                sub_lines,
            )

            if active:
                sub = active[0]
                items = sub["items"]["data"]
                # newer Stripe API versions carry current_period_end on the item
                cpe = (
                    sub.get("trial_end")
                    if sub["status"] == "trialing"
                    else sub.get("current_period_end")
                    or (items and items[0].get("current_period_end"))
                )
                cpe_dt = datetime.fromtimestamp(cpe, tz=timezone.utc)
                add(
                    "09",
                    "931",
                    (
                        "PASS"
                        if not sub.get("cancel_at_period_end")
                        and cpe_dt > datetime.now(timezone.utc)
                        else "FAIL"
                    ),
                    f"cancel_at_period_end={sub.get('cancel_at_period_end')},"
                    f" period_end={cpe_dt:%Y-%m-%d %H:%M:%S}Z,"
                    f" status={sub['status']}",
                    sub_lines
                    + [f"  {sub['id']} period/trial end = {cpe_dt:%Y-%m-%d %H:%M:%S}Z"],
                )
                drift = abs((cpe_dt - parse_dt(user["expiration"])).total_seconds())
                if drift <= 90:
                    drift_status = "PASS"
                elif not prod and drift <= 3 * 86400 + 90:
                    drift_status = "INFO"
                else:
                    drift_status = "FAIL"
                drift_lines = (
                    [
                        f"Stripe {sub['id']} period/trial end ="
                        f" {cpe_dt:%Y-%m-%d %H:%M:%S}Z (GET /v1/subscriptions)",
                        "DB subscription_users.expiration  =" f" {user['expiration']}Z",
                    ]
                    + sqld("user")
                    + [f"-> drift {drift:.0f}s"]
                )
                if drift_status == "INFO":
                    drift_lines.append(
                        "-> INFO: the nightly stop loses webhook deliveries and"
                        " Stripe redelivers within 72h, so development lags"
                        " Stripe by up to 3 days between deliveries"
                    )
                add(
                    "09",
                    "932",
                    drift_status,
                    f"Stripe period/trial end vs DB expiration drift: {drift:.0f}s",
                    drift_lines,
                )
                add(
                    "20",
                    "2017",
                    drift_status,
                    f"expiration={user['expiration']}Z vs current_period_end="
                    f"{cpe_dt:%Y-%m-%d %H:%M:%S}Z (drift {drift:.0f}s)",
                    drift_lines,
                )
            else:
                for row in ("931", "932"):
                    add("09", row, "SKIP", "no active subscription in Stripe")
                add("20", "2017", "SKIP", "no active subscription in Stripe")

            pms = stripe_get(
                key, "/v1/payment_methods", customer=cus_id, type="card", limit=10
            ).get("data", [])
            pm_lines = [
                "GET https://api.stripe.com/v1/payment_methods"
                f"?customer={cus_id}&type=card&limit=10"
            ] + [
                f"  {p['id']}: {p['card']['brand']} ****{p['card']['last4']}"
                f" exp {p['card']['exp_month']}/{p['card']['exp_year']}"
                f"{' (default)' if p['id'] == default_pm else ''}"
                for p in pms
            ]
            if pms:
                card = pms[0]["card"]
                future = (card["exp_year"], card["exp_month"]) >= (
                    datetime.now().year,
                    datetime.now().month,
                )
                add(
                    "09",
                    "933",
                    "PASS" if future else "FAIL",
                    f"{card['brand']} ****{card['last4']}"
                    f" exp {card['exp_month']}/{card['exp_year']},"
                    f" default={'yes' if pms[0]['id'] == default_pm else 'no'}",
                    pm_lines,
                )
            else:
                add(
                    "09",
                    "933",
                    "FAIL" if user["plan_id"] == 2 else "SKIP",
                    "no card payment method attached",
                    pm_lines,
                )

            invs = stripe_get(key, "/v1/invoices", customer=cus_id, limit=10).get(
                "data", []
            )
            inv_lines = [
                f"GET https://api.stripe.com/v1/invoices?customer={cus_id}&limit=10"
            ] + [
                f"  {i.get('number')}: {i.get('billing_reason')}"
                f" status={i['status']} total={i['total']} {i['currency']}"
                f" tax={inv_tax(i)} created={iso(i['created'])}"
                for i in invs
            ]
            paid = [i for i in invs if i["status"] == "paid"]
            if paid:
                inv = paid[0]
                created_day = datetime.fromtimestamp(inv["created"], tz=timezone.utc)
                add(
                    "09",
                    "934",
                    "PASS",
                    f"latest paid invoice {inv.get('number')}:"
                    f" total={inv['total']} {inv['currency']},"
                    f" created={created_day:%Y-%m-%d}",
                    inv_lines,
                )
                add(
                    "09",
                    "926",
                    "PASS" if inv.get("invoice_pdf") else "FAIL",
                    f"invoice_pdf URL present: {bool(inv.get('invoice_pdf'))}"
                    " (layout check stays manual)",
                    [
                        inv_lines[0],
                        f"  {inv.get('number')}: invoice_pdf"
                        f" {'present' if inv.get('invoice_pdf') else 'MISSING'}",
                    ],
                )
                taxed = [i for i in invs if inv_tax(i)]
                add(
                    "09",
                    "923",
                    "PASS" if taxed else "FAIL",
                    f"invoices carrying a tax line: {len(taxed)} of {len(invs)};"
                    f" latest tax={inv_tax(taxed[0]) if taxed else None}",
                    inv_lines,
                )
            else:
                for row in ("934", "926", "923"):
                    add("09", row, "SKIP", "no paid invoice on the customer", inv_lines)

            cycles = [
                i for i in invs if i.get("billing_reason") == "subscription_cycle"
            ]
            initial = [
                i for i in invs if i.get("billing_reason") == "subscription_create"
            ]
            if cycles:
                taxes = sorted({inv_tax(i) for i in cycles})
                base = inv_tax(initial[0]) if initial else None
                ok = all(t > 0 for t in taxes) and (base is None or base in taxes)
                add(
                    "09",
                    "925",
                    "PASS" if ok else "FAIL",
                    f"recurring (subscription_cycle) invoices: {len(cycles)},"
                    f" tax amounts {taxes}"
                    + (
                        f"; initial invoice tax {base}"
                        if base is not None
                        else " (initial invoice older than the fetched window)"
                    ),
                    inv_lines,
                )
            else:
                add(
                    "09",
                    "925",
                    "SKIP",
                    "no subscription_cycle invoice yet (young subscription)",
                    inv_lines,
                )

            pis = stripe_get(key, "/v1/payment_intents", customer=cus_id, limit=3).get(
                "data", []
            )
            add(
                "09",
                "935",
                (
                    "PASS"
                    if pis and pis[0]["status"] == "succeeded"
                    else ("SKIP" if not pis else "FAIL")
                ),
                "latest payment intents:"
                f" {[(p['id'], p['status'], p['amount']) for p in pis] or 'none'}",
                [
                    "GET https://api.stripe.com/v1/payment_intents"
                    f"?customer={cus_id}&limit=3"
                ]
                + [
                    f"  {p['id']}: status={p['status']} amount={p['amount']}"
                    f" created={iso(p['created'])}"
                    for p in pis
                ],
            )

            events = stripe_get(key, "/v1/events", limit=100).get("data", [])
            mine = [
                e
                for e in events
                if cus_id in json.dumps(e.get("data", {}).get("object", {}))
            ]
            types = [
                (
                    e["type"],
                    datetime.fromtimestamp(e["created"], tz=timezone.utc).strftime(
                        "%m-%d %H:%M"
                    ),
                )
                for e in reversed(mine)
            ]
            failures = [t for t, _ in types if "failed" in t]
            add(
                "09",
                "936",
                ("PASS" if not failures else "FAIL") if mine else "INFO",
                f"events for {cus_id} in the 30-day window:"
                f" {types or 'none (older than retention)'}"
                + (f"; failure events: {failures}" if failures else ""),
                [
                    "GET https://api.stripe.com/v1/events?limit=100"
                    f"  (filtered to objects mentioning {cus_id})"
                ]
                + [f"  {e['type']} at {iso(e['created'])}" for e in reversed(mine)],
            )

            add(
                "20",
                "2022",
                "INFO",
                "composite of 2013/2014/2016/2017 + card check above;"
                " see those rows",
            )
            add(
                "20",
                "2019/2020",
                "INFO",
                "dashboard review; the API-visible half is rows 927-936 above",
            )
            add(
                "20",
                "2021",
                "INFO",
                "DB and Stripe legs are 2017/2022 above; the UI leg needs"
                " a browser (LC-02/LC-11/LC-22)",
            )
        else:
            add(
                "09",
                "927-936",
                "SKIP",
                "no target user with a provider_customer_id; pass --user-id",
            )

    print("checking CloudWatch metrics ...")
    metrics = aws("cloudwatch", "list-metrics", "--namespace", namespace).get(
        "Metrics", []
    )
    names = {m["MetricName"] for m in metrics}
    missing = [m for m in EXPECTED_METRICS if m not in names]
    absent_optional = [m for m in OPTIONAL_METRICS if m not in names]
    sync_dims = {
        d["Name"]
        for m in metrics
        if m["MetricName"] == "ExperimentsSynced"
        for d in m.get("Dimensions", [])
    }
    dp = []
    cw_detail = [
        f"$ aws cloudwatch list-metrics --namespace {namespace} --region {REGION}",
        f"  -> {sorted(names) or 'no metrics'}",
    ]
    for name in ("ExperimentsSynced", "DataCleanupCount"):
        if name in names:
            stats = aws(
                "cloudwatch",
                "get-metric-statistics",
                "--namespace",
                namespace,
                "--metric-name",
                name,
                "--statistics",
                "Sum",
                "--period",
                "86400",
                "--start-time",
                datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z"),
                "--end-time",
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ).get("Datapoints", [])
            dp.append(f"{name} today: {sum(d['Sum'] for d in stats):.0f}")
    if args.verbose:
        start7 = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
            "%Y-%m-%dT00:00:00Z"
        )
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cw_detail.append(
            f"$ aws cloudwatch get-metric-statistics --namespace {namespace}"
            " --metric-name <name> --statistics Sum --period 86400"
            f" --start-time {start7} --end-time {now_iso}"
        )
        for name in EXPECTED_METRICS:
            if name not in names:
                cw_detail.append(f"  {name}: metric absent")
                continue
            stats = aws(
                "cloudwatch",
                "get-metric-statistics",
                "--namespace",
                namespace,
                "--metric-name",
                name,
                "--statistics",
                "Sum",
                "--period",
                "86400",
                "--start-time",
                start7,
                "--end-time",
                now_iso,
            ).get("Datapoints", [])
            pts = sorted((d["Timestamp"][:10], d["Sum"]) for d in stats)
            cw_detail.append(
                f"  {name}: "
                + (
                    ", ".join(f"{t}={s:.0f}" for t, s in pts)
                    or "no datapoints in 7 days"
                )
            )
        log_group = f"/ecs/{env_prefix}-background-optinist-cloud-taskdef"
        cw_detail.append(
            f"$ aws logs tail {log_group} --since 2h --format short"
            f" --region {REGION}"
        )
        raw_lines = []
        try:
            # read the tail of the newest stream: this container flushes logs
            # in large chunks whose event timestamps are unreliable, so a
            # time-window filter can miss everything
            streams = aws(
                "logs",
                "describe-log-streams",
                "--log-group-name",
                log_group,
                "--order-by",
                "LastEventTime",
                "--descending",
                "--max-items",
                "1",
            ).get("logStreams", [])
            if streams:
                log_events = aws(
                    "logs",
                    "get-log-events",
                    "--log-group-name",
                    log_group,
                    "--log-stream-name",
                    streams[0]["logStreamName"],
                    "--limit",
                    "100",
                ).get("events", [])
                for ev in log_events:
                    raw_lines += [
                        ln.strip() for ln in ev["message"].splitlines() if ln.strip()
                    ]
        except RuntimeError as e:
            cw_detail.append(f"  log group {log_group}: {e}")
        seen, quoted = set(), []
        for ln in reversed(raw_lines):
            low = ln.lower()
            if not any(
                k in low
                for k in (
                    "starting",
                    "completed",
                    "published cloudwatch metrics",
                    "cleanup",
                    "sync",
                )
            ):
                continue
            # one quote per distinct message kind, newest first, so the
            # 5-minute scheduler line cannot crowd out the rest
            kind = low.rsplit(" - ", 1)[-1][:60]
            if kind in seen:
                continue
            seen.add(kind)
            quoted.append(ln)
            if len(quoted) == 12:
                break
        cw_detail += [f"  {ln[:300]}" for ln in reversed(quoted)]
        if raw_lines and not quoted:
            cw_detail.append(
                f"  {len(raw_lines)} recent log lines, none matching"
                " the job keywords"
            )
    add(
        "20",
        "2027",
        "PASS" if not missing else "FAIL",
        f"namespace {namespace}:"
        f" present={sorted(names & set(EXPECTED_METRICS + OPTIONAL_METRICS))},"
        f" missing={missing or 'none'},"
        f" idle-cleanup-absent={absent_optional or 'none'},"
        f" ExperimentsSynced dims={sorted(sync_dims) or 'none'};"
        f" {'; '.join(dp) or 'no datapoints today'}",
        cw_detail,
    )

    print("probing public no-auth API ...")
    base = args.base_url or ("https://www.araya-optinist.com" if prod else None)
    if not base:
        lbs = aws("elbv2", "describe-load-balancers", "--names", f"{env}-lb")[
            "LoadBalancers"
        ]
        dns = lbs[0]["DNSName"]
        for cand in (f"https://{dns}", f"http://{dns}:8080", f"http://{dns}"):
            status, body = http_get(cand + "/health", timeout=10)
            if status == 200:
                base = cand
                break
    if not base:
        add("20", "2028", "SKIP", "no reachable base URL found; pass --base-url")
    elif not db.get("pub_exp"):
        add(
            "20",
            "2028",
            "SKIP",
            f"{base}: no published experiment (publish_status=1) in the DB",
        )
    else:
        ws, uid = db["pub_exp"][0]
        pub_path = f"/api/public/dataview/workflow/reproduce/{ws}/{uid}"
        pub_status, _ = http_get(f"{base}{pub_path}", timeout=60)
        evid = f"{base}: published {ws}/{uid} -> HTTP {pub_status}"
        detail = [
            f"$ curl -sS -o /dev/null -w 'HTTP %{{http_code}}' {base}{pub_path}",
            f"  -> HTTP {pub_status} (published record, no Authorization header)",
        ]
        priv_ok = True
        if db.get("priv_exp"):
            pws, puid = db["priv_exp"][0]
            priv_path = f"/api/public/dataview/workflow/reproduce/{pws}/{puid}"
            priv_status, _ = http_get(f"{base}{priv_path}", timeout=60)
            priv_ok = priv_status == 404
            evid += f"; private {pws}/{puid} -> HTTP {priv_status} (want 404)"
            detail += [
                "$ curl -sS -o /dev/null -w 'HTTP %{http_code}'" f" {base}{priv_path}",
                f"  -> HTTP {priv_status} (private record; 404 expected)",
            ]
        bad_status, _ = http_get(
            f"{base}/api/dataview?limit=5",
            {"Authorization": "Bearer invalid-token"},
            timeout=30,
        )
        bad_ok = bad_status in (401, 403)
        evid += f"; bad-token /api/dataview -> HTTP {bad_status} (want 401/403)"
        detail += [
            "$ curl -sS -o /dev/null -w 'HTTP %{http_code}'"
            f" -H 'Authorization: Bearer invalid-token' {base}/api/dataview?limit=5",
            f"  -> HTTP {bad_status} (protected endpoint with a bad token;"
            " 401/403 expected)",
        ]
        add(
            "20",
            "2028",
            "PASS" if pub_status == 200 and priv_ok and bad_ok else "FAIL",
            evid,
            detail,
        )

    skipped = [
        (
            "09",
            "905/906",
            "needs a checkout performed mid-test (before/after product-count"
            " and mutation checks); the mutation half is pinned by"
            " test_checkout_session_tax_config.py",
        ),
        (
            "09",
            "909-916",
            "browser behaviour on the Stripe-hosted checkout form; 909/910"
            " are pinned by test_checkout_session_tax_config.py",
        ),
        ("09", "918/919/922", "fresh-checkout log and dashboard observations"),
        (
            "20",
            "2000-2007",
            "browser gestures; covered by the cited e2e tests where one exists",
        ),
        ("20", "2011/2015", "needs a live checkout / a second authenticated session"),
        ("20", "2025", "login e2e (AUTH-01)"),
        (
            "20",
            "2029-2032",
            "operational fault injection (large sync, disk full, S3 tampering)",
        ),
    ]

    def row_key(entry):
        digits = "".join(c if c.isdigit() else " " for c in entry[1]).split()
        return int(digits[0]) if digits else 0

    if args.cases == "release":
        by_row = {
            row: (status, evidence, detail)
            for _, row, status, evidence, detail in results
        }
        rank = {"PASS": 0, "INFO": 1, "SKIP": 2, "FAIL": 3}
        render = []
        for sheet, bt, srcs in RELEASE_MAP:
            hits = [(r, *by_row[r]) for r in srcs if r in by_row]
            if not hits:
                continue
            status = max((h[1] for h in hits), key=lambda s: rank.get(s, 2))
            evidence = " / ".join(f"[{r}] {e}" for r, _, e, _ in hits)
            detail = []
            for r, s, _, d in hits:
                if d:
                    detail += [f"# system check {r}: {s}"] + d
            render.append((sheet, bt, status, evidence, detail))
        sheet_titles = [
            ("09 Subscription Registration", "Release 09 Subscription Registration"),
            ("11 AWS Monitoring", "Release 11 AWS Monitoring"),
        ]
        heading = "release test cases"
        coverage_note = [
            "Only the release rows this scan evidences are listed; the rest of the",
            "release sheets stay manual or e2e (see RELEASE_TEST_COVERAGE.md).",
        ]
    else:
        render = sorted(results, key=row_key)
        sheet_titles = [
            ("09", "Sheet 09: Stripe Prdct Data Sync & Tax"),
            ("20", "Sheet 20: System & Security"),
        ]
        heading = "system sheets 09 and 20"
        coverage_note = []

    generated = (
        f"Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z"
        f" | {args.check} check | env `{env}` | region {REGION}"
    )
    if user:
        generated += f" | target user {user['id']} ({user['email']})"
    lines = (
        [
            f"# Manual-test scan report: {heading}",
            "",
            generated,
            "",
            "All checks are read-only: SQL over SSM Run Command, Stripe GET"
            " requests,",
            "CloudWatch reads, and unauthenticated HTTP probes.",
            "",
        ]
        + coverage_note
        + ([""] if coverage_note else [])
    )
    for sheet, title in sheet_titles:
        lines += [f"## {title}", "", "| Row | Status | Evidence |", "|---|---|---|"]
        for s, row, status, evidence, _ in render:
            if s == sheet:
                lines.append(f"| {row} | {status} | {evidence.replace('|', '/')} |")
        lines.append("")
    if args.cases == "system":
        lines += ["## Not scannable from a script (stay manual / e2e)", ""]
        for sheet, rows, why in skipped:
            lines.append(f"- Sheet {sheet} rows {rows}: {why}")
        lines.append("")
    if args.verbose:
        stamp = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z, {args.check} check"
        lines += [
            "## Per-row evidence (copy the block into the sheet's Evidence column)",
            "",
        ]
        for sheet, title in sheet_titles:
            for s, row, status, evidence, detail in render:
                if s == sheet and detail:
                    lines += [f"### {title} row {row}: {status}", "", "```"]
                    lines += [f"# manual_test_scan.py, {stamp}"]
                    lines += detail
                    lines += ["```", ""]

    with open(args.out, "w") as f:
        f.write("\n".join(lines))
    if args.json:
        with open(args.json, "w") as f:
            # Keyed by row, but a row emitted twice in one pass would silently
            # drop the first verdict, and the caller asserts per row.
            out = {}
            for sheet, row, status, evidence, _ in results:
                if row in out:
                    raise SystemExit(f"row {row} reported twice; JSON would drop one")
                out[row] = {"sheet": sheet, "status": status, "evidence": evidence}
            json.dump(out, f, indent=2)
    fails = sum(1 for r in results if r[2] == "FAIL")
    print(f"\nreport written to {args.out}: {len(results)} rows, {fails} FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
