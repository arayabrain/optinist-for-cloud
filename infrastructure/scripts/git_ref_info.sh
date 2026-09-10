#!/usr/bin/env bash
# ============================================================================
# git_ref_info.sh
# ============================================================================
# Resolves the git provenance of the current worktree into three variables:
#
#   GIT_INFO_COMMIT  full commit hash, or "unknown" outside a git worktree
#   GIT_INFO_BRANCH  branch name, or "" when HEAD is detached
#   GIT_INFO_TAG     tag pointing exactly at HEAD, or "" when there is none
#
# Meant to be sourced, not executed:
#
#   # shellcheck source=infrastructure/scripts/git_ref_info.sh
#   . "$(dirname "$0")/git_ref_info.sh"
#   resolve_git_ref_info            # optionally: resolve_git_ref_info <dir>
#
# Not executable on purpose — running it directly does nothing.
#
# Only facts are resolved here. The "which ref was this deployed from?"
# summary is derived where it is read (see studio/app/version.py BuildInfo),
# so the derivation rule lives in one place and stays changeable — a value
# baked into BUILD_INFO or an ECS tag could never be corrected afterwards.
#
# Why not `git rev-parse --abbrev-ref HEAD`: it reports the literal string
# "HEAD" for a detached HEAD, which is what a tag checkout produces. That
# made tag-based deployments record `git_branch: "HEAD"` and lose the tag
# entirely. `git symbolic-ref` fails cleanly instead, leaving the branch
# empty so the tag is what identifies the build.
# ============================================================================

resolve_git_ref_info() {
    local repo_dir="${1:-.}"

    GIT_INFO_COMMIT=$(git -C "$repo_dir" rev-parse HEAD 2>/dev/null || echo "unknown")

    # Empty (not "HEAD") when detached, e.g. when a tag was checked out.
    GIT_INFO_BRANCH=$(git -C "$repo_dir" symbolic-ref --short -q HEAD 2>/dev/null || echo "")

    # Tags whose target is HEAD itself, highest release version first.
    #
    # `--sort=-v:refname` alone is not enough, for two reasons measured on a
    # commit carrying v1.1.10, v1.1.10-rc1 and v1.1.10-beta:
    #
    #   - With no configuration it ranks v1.1.10-rc1 *above* v1.1.10, so the
    #     release build would have recorded the release candidate.
    #   - Its ordering of suffixed tags follows the operator's
    #     `versionsort.suffix` setting, which is normally global and personal.
    #     The same commit produced v1.1.10-rc1, v1.1.10-beta or v1.1.10
    #     depending on whose machine ran the build.
    #
    # So the policy is stated here rather than delegated to git config: a
    # release tag (v1.2.3 or 1.2.3, no suffix) always wins, and among those the
    # ordering is pure numeric comparison, which no setting affects.
    #
    # `|| true` keeps the pipeline non-fatal for callers running under
    # `set -e -o pipefail` (head closing the pipe early, or grep matching
    # nothing, is not an error here).
    #
    # No `git describe --tags --exact-match` fallback: it reads the same
    # refs/tags/* this does, so it cannot succeed where this returns nothing —
    # including a shallow `git clone --depth 1 -b <tag>`, which creates the ref
    # for the requested tag even under `--no-tags`. It would also answer
    # *differently* on a commit carrying several tags, preferring the annotated
    # one over the highest version and breaking the ordering above.
    GIT_INFO_TAG=$(git -C "$repo_dir" tag --points-at HEAD --sort=-v:refname 2>/dev/null \
        | grep -E '^v?[0-9]+(\.[0-9]+)*$' | head -n1 || true)

    # No release tag on this commit: a prerelease, or a name like `nightly`, is
    # still worth recording. `-refname` is a plain reverse-lexical sort, which
    # `versionsort.suffix` does not touch, so this stays deterministic too.
    if [ -z "$GIT_INFO_TAG" ]; then
        GIT_INFO_TAG=$(git -C "$repo_dir" tag --points-at HEAD --sort=-refname 2>/dev/null | head -n1 || true)
    fi
}
