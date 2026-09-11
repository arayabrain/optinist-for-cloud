#!/usr/bin/env python3
"""Print the manual-test backlog, derived from the sheets themselves.

The exported CSV sheets are the source of truth for coverage (see their README),
so the list of rows still needing work is derivable rather than worth maintaining
by hand - a parallel to-do file drifts, and the one we kept in a session
scratchpad was lost twice.

Point TEST_SHEET_DIR at a directory holding every sheet exported as CSV from the
test case sheets folder on the internal Drive, or pass --sheets:
https://drive.google.com/drive/folders/1zOfe1Clvg3WF3rtiJVACpOagGnPzxKEO

  MANUAL  = no automated counterpart. Open unless Notes record why it is retired.
  PARTIAL = automated in part; Notes must say which half is left.
  FULL    = done.

A MANUAL or PARTIAL row whose Notes carry an "Adjudicated"/"Re-graded"/
"CONFIRMED-IMPOSSIBLE" line is reported as decided, not as backlog.

  export TEST_SHEET_DIR=<dir of exported sheets>
  python3 infrastructure/scripts/coverage_backlog.py            # summary
  python3 infrastructure/scripts/coverage_backlog.py --rows     # every open row
  python3 infrastructure/scripts/coverage_backlog.py --decided  # retired, and why
"""
import argparse
import csv
import glob
import os
import re

DECIDED = re.compile(r"Adjudicated|Re-graded|CONFIRMED-IMPOSSIBLE", re.I)


def sheets(sheet_dir):
    for path in sorted(glob.glob(os.path.join(sheet_dir, "*.csv"))):
        rows = list(csv.reader(open(path, encoding="utf-8-sig")))
        head = next((i for i, r in enumerate(rows[:5]) if "Coverage" in r), None)
        if head is None:
            continue
        name = os.path.basename(path).split(" - ")[-1][:-4]
        kind = "REL" if "Release Test" in path else "SYS"
        yield kind, name, rows[head], rows[head + 1 :]


def classify(header, row):
    cell = dict(zip(header, row))
    coverage = cell.get("Coverage", "").strip().upper()
    notes = cell.get("Notes") or cell.get("Note") or ""
    if not coverage:
        return None
    if coverage.startswith("FULL"):
        return "full"
    if DECIDED.search(notes):
        return "decided"
    return "open"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", action="store_true", help="list every open row")
    ap.add_argument(
        "--decided", action="store_true", help="list retired rows and the reason"
    )
    ap.add_argument(
        "--sheets",
        default=os.environ.get("TEST_SHEET_DIR"),
        help="directory of exported CSV sheets (default: $TEST_SHEET_DIR)",
    )
    args = ap.parse_args()
    if not args.sheets:
        ap.error("set TEST_SHEET_DIR or pass --sheets <dir of exported sheets>")

    totals = {"full": 0, "decided": 0, "open": 0}
    print(f"{'sheet':46s} {'full':>5s} {'decided':>8s} {'open':>5s}")
    for kind, name, header, rows in sheets(os.path.expanduser(args.sheets)):
        counts = {"full": 0, "decided": 0, "open": 0}
        listed = []
        for row in rows:
            if not row or not row[0].strip():
                continue
            verdict = classify(header, row)
            if verdict is None:
                continue
            counts[verdict] += 1
            totals[verdict] += 1
            cell = dict(zip(header, row))
            if verdict == "open" and args.rows:
                listed.append(
                    f"    {row[0]:8s} {cell.get('Coverage','').strip():8s}"
                    f" {cell.get('Subject','')[:60]}"
                )
            if verdict == "decided" and args.decided:
                reason = next(
                    (
                        line
                        for line in (cell.get("Notes") or cell.get("Note") or "").split(
                            "\n"
                        )
                        if DECIDED.search(line)
                    ),
                    "",
                )
                listed.append(f"    {row[0]:8s} {reason[:150]}")
        print(
            f"{kind} {name:42s} {counts['full']:5d} "
            f"{counts['decided']:8d} {counts['open']:5d}"
        )
        for line in listed:
            print(line)
    print(
        f"\n{'TOTAL':46s} {totals['full']:5d} "
        f"{totals['decided']:8d} {totals['open']:5d}"
    )
    print(
        "automated (FULL + PARTIAL counted by the sheets' own rule): see "
        "SYSTEM_TEST_COVERAGE.md; this script counts what is left to do."
    )


if __name__ == "__main__":
    main()
