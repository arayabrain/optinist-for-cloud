#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# terraform_build_info.sh
# ============================================================================
# Emits terraform-apply provenance as JSON on stdout, for consumption by the
# Terraform `external` data source (data.external.tf_build_info).
#
# This records "which git revision of infrastructure/ was applied", mirroring
# the Docker /app/BUILD_INFO concept (which records image provenance) at the
# infrastructure layer. The values are stamped onto the ECS cluster as tags so
# a deployment can be traced back to its source revision from the running env.
#
# `external` requires a flat JSON object of string values on stdout.
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Resolve against the infrastructure/ directory so git info reflects the IaC
# repo, regardless of the caller's working directory.
# shellcheck source=infrastructure/scripts/git_ref_info.sh
. "$SCRIPT_DIR/git_ref_info.sh"
resolve_git_ref_info "$SCRIPT_DIR/.."

# `|| true` keeps the dirty check non-fatal under `set -e`
git_status=$(git -C "$SCRIPT_DIR/.." status --porcelain 2>/dev/null || true)
if [ -n "$git_status" ]; then
  git_dirty="true"
else
  git_dirty="false"
fi

# Encode as JSON via python3 (available in the toolchain; avoids a jq dependency).
# Indexed explicitly rather than zipped: a missing argument then raises
# IndexError here, instead of silently emitting a JSON object without the
# field, which terraform would only report much later as "object does not have
# an attribute named git_tag" at plan time.
python3 -c "import json,sys; json.dump({'git_commit':sys.argv[1],'git_branch':sys.argv[2],'git_tag':sys.argv[3],'git_dirty':sys.argv[4]}, sys.stdout)" \
  "$GIT_INFO_COMMIT" "$GIT_INFO_BRANCH" "$GIT_INFO_TAG" "$git_dirty"
