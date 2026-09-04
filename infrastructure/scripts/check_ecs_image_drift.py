#!/usr/bin/env python3
"""
check_ecs_image_drift.py — detect ECS services running a STALE image.

Catches the recurring "stale cached :latest" problem: a task keeps running an
old locally-cached image even though ECR :latest has moved on (because the ECS
agent reuses the cached tag instead of re-pulling). The task definition only
references the mutable tag (:latest), but ECS records the real digest each
running container pulled — comparing that to the current ECR digest reveals drift.

Read-only: only ECR/ECS/EC2 describe+list calls. Safe to run anytime.

Usage:
  python3 check_ecs_image_drift.py \
      --cluster development-optinist-cloud-cluster \
      --repo    development-optinist-for-cloud \
      [--tag latest] [--region ap-northeast-1] [--services svcA svcB ...]

Exit code 0 = every service runs the target digest. 1 = drift or a down service.
Run it before AND after a deploy/test to confirm the fleet is on the real latest.
"""
import argparse
import json
import subprocess
import sys

# Tags that move between builds, so they name no particular one. ecr_build_push.sh
# pushes exactly these plus one immutable version tag per build.
MUTABLE_TAGS = {"latest"}


def aws(region, *args):
    """Run an aws CLI command, return parsed JSON (None on empty output)."""
    cmd = ["aws", "--region", region, "--output", "json", *args]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:3])}…: {p.stderr.strip()}")
    out = p.stdout.strip()
    return json.loads(out) if out else None


def aws_optional(region, *args):
    """Like aws(), but returns None instead of raising when the call fails.

    Used only for presentational lookups, so a missing/expired image can never
    turn a drift report into a crash.
    """
    try:
        return aws(region, *args)
    except (RuntimeError, ValueError):  # non-zero exit, or non-JSON stdout
        return None


def short(digest):
    return (digest or "—").replace("sha256:", "")[:12]


def label_for(tags, fallback):
    """Name a build from the tags on its digest: one name, the rest as an aside.

    A bare ", ".join competes with the "A != B" sentence it lands in, and a
    mutable tag names no build — so prefer an immutable one and park the others
    in parentheses.
    """
    if not tags:
        return fallback
    preferred = next((t for t in tags if t not in MUTABLE_TAGS), tags[0])
    others = [t for t in tags if t != preferred]
    return f"{preferred} (also {', '.join(others)})" if others else preferred


def target_label_for(tag, alias_tags):
    """Name the build a target tag refers to.

    A mutable tag names no build, so it defers to its immutable alias. An
    explicit tag already names the build asked for and is kept as given —
    substituting there can only lose the information the caller supplied.
    """
    if tag in MUTABLE_TAGS:
        return label_for(alias_tags, tag)
    return tag


def resolve_version(region, repo, digest, cache):
    """Best-effort ECR tag(s) for a digest, so a stale row can name its build.

    Purely presentational — it never feeds the OK/STALE decision. A digest no
    longer present in ECR yields None and the row falls back to the digest
    alone, so a report can never fail on the lookup. Cached per digest, so
    tasks sharing an image cost one call, failures included.
    """
    if digest in cache:
        return cache[digest]
    tags = (
        aws_optional(
            region,
            "ecr",
            "describe-images",
            "--repository-name",
            repo,
            "--image-ids",
            f"imageDigest={digest}",
            "--query",
            "imageDetails[0].imageTags",
        )
        or []
    )
    # A None in the cache marks a digest nothing could be resolved for; main()
    # reports those once, after the table.
    cache[digest] = label_for(tags, None)
    return cache[digest]


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


def main():
    ap = argparse.ArgumentParser(description="Detect stale ECS image digests vs ECR.")
    ap.add_argument("--cluster", required=True)
    ap.add_argument("--repo", required=True, help="ECR repository name")
    ap.add_argument(
        "--tag",
        default="latest",
        help="ECR tag treated as the target (default: latest)",
    )
    ap.add_argument("--region", default="ap-northeast-1")
    ap.add_argument(
        "--services", nargs="*", help="Service names (default: all in cluster)"
    )
    args = ap.parse_args()
    region = args.region

    # 1. Authoritative target digest = what REPO:TAG points to in ECR right now.
    img = aws(
        region,
        "ecr",
        "describe-images",
        "--repository-name",
        args.repo,
        "--image-ids",
        f"imageTag={args.tag}",
        "--query",
        "imageDetails[0]",
    )
    if not img:
        # Kept on aws(), not aws_optional(): the raw error distinguishes a
        # missing image from bad credentials, a wrong region or a throttle.
        # Routing this through the fail-soft wrapper would report all of them
        # as "not found", which is why this branch stays hard to reach.
        print(f"ERROR: {args.repo}:{args.tag} not found in ECR ({region}).")
        return 2
    target = img["imageDigest"]
    alias_tags = [t for t in img.get("imageTags", []) if t != args.tag]
    print(f"Target  {args.repo}:{args.tag}")
    print(
        f"  digest  {short(target)}   pushed {img.get('imagePushedAt','?')}"
        f"   aka {', '.join(alias_tags) or '—'}"
    )
    target_label = target_label_for(args.tag, alias_tags)
    print()

    # 2. Services to inspect.
    if args.services:
        services = args.services
    else:
        arns = (
            aws(region, "ecs", "list-services", "--cluster", args.cluster) or {}
        ).get("serviceArns", [])
        services = [a.rsplit("/", 1)[-1] for a in arns]
    if not services:
        print("No services found.")
        return 2

    rows = []  # (service, desired, running, tag, digest, status, note)
    ci_to_ec2 = {}  # container-instance ARN -> ec2 instance id (resolved lazily)
    digest_to_version = {}  # image digest -> ECR tag(s), for naming stale builds

    for batch in chunks(services, 10):
        descs = (
            aws(
                region,
                "ecs",
                "describe-services",
                "--cluster",
                args.cluster,
                "--services",
                *batch,
            )
            or {}
        ).get("services", [])
        for s in descs:
            name = s["serviceName"]
            desired, running = s["desiredCount"], s["runningCount"]

            # Intentionally scaled to 0 -> not drift; report and skip.
            if desired == 0:
                rows.append((name, desired, running, "—", "—", "IDLE", "scaled to 0"))
                continue

            # tag the task def points at (context only)
            td = aws(
                region,
                "ecs",
                "describe-task-definition",
                "--task-definition",
                s["taskDefinition"],
                "--query",
                "taskDefinition.containerDefinitions[0].image",
            )
            ref_tag = (td or "").rsplit(":", 1)[-1] if td else "?"

            # All RUNNING tasks (a row each); if none, the newest STOPPED
            # task (one row) to surface a crash-loop image.
            run_arns = (
                aws(
                    region,
                    "ecs",
                    "list-tasks",
                    "--cluster",
                    args.cluster,
                    "--service-name",
                    name,
                    "--desired-status",
                    "RUNNING",
                )
                or {}
            ).get("taskArns", [])
            status_kind = "RUNNING"
            task_arns = run_arns
            if not task_arns:
                stp = (
                    aws(
                        region,
                        "ecs",
                        "list-tasks",
                        "--cluster",
                        args.cluster,
                        "--service-name",
                        name,
                        "--desired-status",
                        "STOPPED",
                    )
                    or {}
                ).get("taskArns", [])
                task_arns = stp[:1]
                status_kind = "STOPPED"

            if not task_arns:
                rows.append(
                    (name, desired, running, ref_tag, "—", "DOWN", "no tasks at all")
                )
                continue

            tasks = (
                aws(
                    region,
                    "ecs",
                    "describe-tasks",
                    "--cluster",
                    args.cluster,
                    "--tasks",
                    *task_arns,
                )
                or {}
            ).get("tasks", [])
            for t in tasks:
                ci_to_ec2.setdefault(t.get("containerInstanceArn"), None)
                for c in t.get("containers", []):
                    digest = c.get("imageDigest")
                    if status_kind == "STOPPED":
                        status = "DOWN"
                        note = (t.get("stoppedReason") or "")[:60]
                        stopped_ver = (
                            resolve_version(
                                region, args.repo, digest, digest_to_version
                            )
                            if digest
                            else None
                        )
                        if stopped_ver:
                            note = f"{stopped_ver}: {note}" if note else stopped_ver
                    elif digest == target:
                        status, note = "OK", ""
                    elif digest is None:
                        status, note = "UNKNOWN", "no imageDigest reported"
                    else:
                        status = "STALE"
                        running_ver = resolve_version(
                            region, args.repo, digest, digest_to_version
                        )
                        note = (
                            f"running {running_ver} != target {target_label}"
                            if running_ver
                            else "running != target digest"
                        )
                    rows.append(
                        (
                            name,
                            desired,
                            running,
                            ref_tag,
                            short(digest),
                            status,
                            # A marker with no content is not a note. Every
                            # status that has something to say sets `note`.
                            f"[{status_kind} task] {note}" if note else "",
                        )
                    )

    # Resolve container-instance ARNs -> EC2 ids (location hint).
    arns = [a for a in ci_to_ec2 if a]
    for batch in chunks(arns, 100):
        for ci in (
            aws(
                region,
                "ecs",
                "describe-container-instances",
                "--cluster",
                args.cluster,
                "--container-instances",
                *batch,
            )
            or {}
        ).get("containerInstances", []):
            ci_to_ec2[ci["containerInstanceArn"]] = ci.get("ec2InstanceId")

    # 3. Report.
    print(f"{'SERVICE':<46} {'DES':>3} {'RUN':>3} {'TAG':<26} {'DIGEST':<14} STATUS")
    print("-" * 110)
    for name, des, run, tag, digest, status, note in rows:
        mark = {"OK": "  ", "STALE": "▲ ", "DOWN": "✖ ", "UNKNOWN": "? "}.get(
            status, "  "
        )
        line = f"{name:<46} {des:>3} {run:>3} {tag:<26} {digest:<14} {mark}{status}"
        print(line)
        if note:
            print(f"{'':<6}↳ {note}")
    print()

    # Silence on a row is ambiguous — expired, untagged, throttled or denied —
    # and a lost ecr:DescribeImages would quietly unname every row. Reported
    # here rather than mid-build so a redirected stdout keeps the table and its
    # caveats together; the cache has already deduplicated them.
    unresolved = [d for d, v in digest_to_version.items() if v is None]
    if unresolved:
        sys.stdout.flush()
        for d in unresolved:
            print(
                f"WARN: no ECR tags resolved for {short(d)}"
                f" (expired, untagged, or lookup failed)",
                file=sys.stderr,
            )

    stale = [r for r in rows if r[5] == "STALE"]
    down = [r for r in rows if r[5] == "DOWN"]
    if stale or down:
        print("DRIFT DETECTED:")
        for r in stale:
            print(
                f"  ▲ {r[0]} stale — recycle/repull its host to pull"
                f" {target_label} ({short(target)})"
            )
        for r in down:
            # Only point at a note that is actually there: a stopped task
            # need report neither a reason nor a resolvable build.
            print(
                f"  ✖ {r[0]} has no running task"
                f" (desired={r[1]}, running={r[2]})"
                f"{' — see ↳ reason above' if r[6] else ''}"
            )
        print("\nHosts (container instances) in play:")
        for arn, ec2 in ci_to_ec2.items():
            if arn:
                print(f"  {ec2 or '?'}  ({arn.rsplit('/',1)[-1]})")
        return 1

    print("OK: no drift (active services on the target digest).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FileNotFoundError:
        print("ERROR: 'aws' CLI not found on PATH.", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
