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
#   . "$(dirname "$0")/git_ref_info.sh"
#   resolve_git_ref_info            # optionally: resolve_git_ref_info <dir>
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

    # Tags whose target is HEAD itself. `--sort=-v:refname` makes the choice
    # deterministic when several tags share a commit, preferring the highest
    # version (v1.1.10 over v1.1.9).
    # `|| true` keeps the pipeline non-fatal for callers running under
    # `set -e -o pipefail` (head closing the pipe early is not an error here).
    GIT_INFO_TAG=$(git -C "$repo_dir" tag --points-at HEAD --sort=-v:refname 2>/dev/null | head -n1 || true)

    # Fallback for worktrees without local tag refs (e.g. a shallow clone made
    # with `git clone --depth 1 -b <tag>`). `--exact-match` is deliberate: the
    # default `git describe` reports the *nearest* tag, which would record
    # v1.1.10 for a commit 16 revisions past it.
    if [ -z "$GIT_INFO_TAG" ]; then
        GIT_INFO_TAG=$(git -C "$repo_dir" describe --tags --exact-match HEAD 2>/dev/null || echo "")
    fi
}
