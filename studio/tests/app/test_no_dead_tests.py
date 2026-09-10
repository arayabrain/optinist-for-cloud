"""Guard against tests that are counted as coverage but never execute.

A skipped test is indistinguishable from a passing one in a summary line, and
the release sheets read those summary lines. ``test_crud_users_context.py`` sat
at 12 unconditional skips while the coverage map credited it with the
subscription grace-period boundary; the stated reason ("Requires integration
test with real DB") turned out to be false.

Conditional skips are fine - ``skipif`` on an opt-in env var, or a runtime
``pytest.skip`` when a real dependency is genuinely absent, both say something
true about the environment. What this rejects is the unconditional decorator,
which says only that someone stopped maintaining the test.
"""

import re
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent.parent

# ``@pytest.mark.skip`` but not ``@pytest.mark.skipif``. Anchored to the start of
# a line so prose and string literals that name the pattern - including the ones
# in this file - are not mistaken for a decorator.
_UNCONDITIONAL_SKIP = re.compile(r"^[ \t]*(@pytest\.mark\.skip(?!if)\b)", re.MULTILINE)

# An unconditional skip is acceptable only if the reason points at tracked work,
# so it surfaces in an issue triage rather than rotting silently.
_ISSUE_REFERENCE = re.compile(r"#\d+|issues?/\d+", re.IGNORECASE)


def _decorator_source(text: str, start: int) -> str:
    """Return the decorator call starting at ``start``, balanced over parens."""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
        elif char == "\n" and depth == 0:
            return text[start:index]
    return text[start:]


def test_no_unconditional_skip_without_a_linked_issue():
    offenders = []

    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        text = path.read_text()
        for match in _UNCONDITIONAL_SKIP.finditer(text):
            decorator = _decorator_source(text, match.start(1))
            if _ISSUE_REFERENCE.search(decorator):
                continue
            line = text.count("\n", 0, match.start(1)) + 1
            offenders.append(f"{path.relative_to(TESTS_ROOT)}:{line}")

    assert not offenders, (
        "unconditional @pytest.mark.skip with no linked issue - a skip reads as "
        "a pass in the summary line the release sheets are signed off against. "
        "Use @pytest.mark.skipif on a real condition, or cite an issue number in "
        "the reason:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_can_actually_fire():
    """The scan above passes when the tree is clean, which is also what a broken
    regex looks like. Prove the matcher still recognises the pattern it exists
    to reject, and still tolerates the two forms that are allowed."""
    rejected = "\n@pytest.mark.skip(reason='flaky, will look at it later')\n"
    match = _UNCONDITIONAL_SKIP.search(rejected)
    assert match
    assert not _ISSUE_REFERENCE.search(_decorator_source(rejected, match.start(1)))

    with_issue = '\n@pytest.mark.skip(reason="blocked on #664, needs AL2023")\n'
    match = _UNCONDITIONAL_SKIP.search(with_issue)
    assert match
    assert _ISSUE_REFERENCE.search(_decorator_source(with_issue, match.start(1)))

    assert not _UNCONDITIONAL_SKIP.search(
        '\n@pytest.mark.skipif(not os.environ.get("RUN_LOCK_IT"), reason="opt-in")\n'
    )

    indented = "\n    @pytest.mark.skip(reason='no')\n"
    assert _UNCONDITIONAL_SKIP.search(indented), "decorators inside a class count"

    assert not _UNCONDITIONAL_SKIP.search(
        '\nrejected = "@pytest.mark.skip(reason=...)"\n'
    ), "a string literal naming the pattern is not a decorator"


def test_the_guard_reads_multi_line_decorators():
    """The skips this guard was written for spanned two lines, so a matcher that
    stops at the first newline would have missed their reasons entirely."""
    multi_line = (
        "\n@pytest.mark.skip(\n"
        '    reason="Requires integration test with real DB - "\n'
        '    "see #123 for the fixture work"\n'
        ")\n"
    )
    match = _UNCONDITIONAL_SKIP.search(multi_line)
    assert match
    assert _ISSUE_REFERENCE.search(_decorator_source(multi_line, match.start(1)))

    no_issue = (
        "\n@pytest.mark.skip(\n"
        '    reason="Requires integration test with real DB - "\n'
        '    "dynamically added fields not in User schema"\n'
        ")\n"
    )
    match = _UNCONDITIONAL_SKIP.search(no_issue)
    assert match
    assert not _ISSUE_REFERENCE.search(
        _decorator_source(no_issue, match.start(1))
    ), "the exact decorator this guard was written for must still be rejected"
