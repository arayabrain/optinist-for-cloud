#!/usr/bin/env python3
"""Read-only audit: would the router path guard reject any stored path?

The guard added in the #842 series validates request paths before they reach
the filesystem. One case can turn a previously-working request into a 400, and
neither the unit tests nor the E2E suite can see it, because it depends on
data written by an earlier deployment:

  outputPaths in a stored experiment.yaml / workflow.yaml may hold an
  ABSOLUTE path. normalize_output_path() only strips the OUTPUT_DIR of the
  *running* container, so a record written under a different OUTPUT_DIR (say
  /app/studio_data/output while this container uses /tmp/studio/output) keeps
  its foreign prefix, and secure_output_relpath() then rejects it as an
  escape.

Before the guard the same request built a nonsensical path and 404'd; after
it, it is a 400. Either way the file was unreachable, so this is a
diagnostic, not necessarily a blocker -- but it should be a conscious call,
not a surprise in production.

Directory names are NOT checked: secure_component() rejects only "", ".",
".." and values containing a separator, none of which os.listdir can return,
so no on-disk experiment directory can fail it.

Usage:
  python3 audit_path_guard_compatibility.py                    # $OPTINIST_DIR
  python3 audit_path_guard_compatibility.py --data-dir /path
  python3 audit_path_guard_compatibility.py --output-dir /app/studio_data/output
  python3 audit_path_guard_compatibility.py --json

Exit status: 0 when every stored path resolves, 1 when any would be rejected.
"""
import argparse
import json
import os
import sys

try:
    import yaml
except ImportError:  # pragma: no cover - the audit is useless without it
    sys.exit("PyYAML is required: pip install pyyaml")

CONFIG_NAMES = ("experiment.yaml", "workflow.yaml")


def would_be_rejected(output_dir: str, path: str):
    """Mirror normalize_output_path + secure_output_relpath.

    Returns None when the path resolves, or a reason string when it does not.
    """
    if not path:
        return None
    if path.startswith(output_dir):
        path = path[len(output_dir) :].lstrip("/")
    prefix = os.path.normpath(output_dir) + os.sep
    normalized = os.path.normpath(os.path.join(output_dir, path))
    if not normalized.startswith(prefix):
        return f"resolves to {normalized!r}, outside {prefix!r}"
    return None


def stored_paths(doc):
    """Yield (where, path) for every outputPaths entry in a config document."""
    if not isinstance(doc, dict):
        return
    for node_id, node in (doc.get("nodeDict") or {}).items():
        if isinstance(node, dict):
            yield f"nodeDict.{node_id}", None  # visited for id reporting only
    for fn_id, fn in (doc.get("function") or {}).items():
        if not isinstance(fn, dict):
            continue
        for key, out in (fn.get("outputPaths") or {}).items():
            if isinstance(out, dict) and isinstance(out.get("path"), str):
                yield f"function.{fn_id}.outputPaths.{key}", out["path"]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--data-dir",
        default=os.environ.get("OPTINIST_DIR"),
        help="DATA_DIR root; defaults to $OPTINIST_DIR",
    )
    ap.add_argument(
        "--output-dir",
        help="OUTPUT_DIR the app will run with; defaults to <data-dir>/output",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    if not args.data_dir:
        ap.error("set OPTINIST_DIR or pass --data-dir")
    output_dir = args.output_dir or os.path.join(args.data_dir, "output")

    findings, configs = [], 0
    for dirpath, _dirnames, filenames in os.walk(output_dir):
        for name in filenames:
            if name not in CONFIG_NAMES:
                continue
            full = os.path.join(dirpath, name)
            configs += 1
            try:
                doc = yaml.safe_load(open(full))
            except Exception as e:  # noqa: BLE001 - report and continue
                findings.append({"kind": "unreadable", "file": full, "error": str(e)})
                continue
            for where, path in stored_paths(doc):
                if path is None:
                    continue
                reason = would_be_rejected(output_dir, path)
                if reason:
                    findings.append(
                        {
                            "kind": "rejected",
                            "file": full,
                            "where": where,
                            "path": path,
                            "reason": reason,
                        }
                    )

    rejected = [f for f in findings if f["kind"] == "rejected"]

    if args.json:
        json.dump(
            {
                "output_dir": output_dir,
                "configs_scanned": configs,
                "findings": findings,
            },
            sys.stdout,
            indent=2,
        )
        print()
    else:
        print(f"OUTPUT_DIR: {output_dir}")
        print(f"configs scanned: {configs}")
        for f in findings:
            if f["kind"] == "rejected":
                print(f"  REJECTED  {f['file']}")
                print(f"            {f['where']} = {f['path']!r}")
                print(f"            {f['reason']}")
            else:
                print(f"  WARN      {f['file']}: {f['error']}")
        if not configs:
            print("  SKIP      no experiment.yaml / workflow.yaml found")
        elif not rejected:
            print("  OK        every stored path resolves under OUTPUT_DIR")

    return 1 if rejected else 0


if __name__ == "__main__":
    sys.exit(main())
