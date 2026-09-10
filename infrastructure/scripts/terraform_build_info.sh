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

# Resolve to the infrastructure/ directory so git info reflects the IaC repo,
# regardless of the caller's working directory.
cd "$(dirname "$0")/.."

git_commit=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
git_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
# `|| true` keeps the dirty check non-fatal under `set -e`
git_status=$(git status --porcelain 2>/dev/null || true)
if [ -n "$git_status" ]; then
  git_dirty="true"
else
  git_dirty="false"
fi

# Encode as JSON via python3 (available in the toolchain; avoids a jq dependency).
python3 -c "import json,sys; json.dump({'git_commit':sys.argv[1],'git_branch':sys.argv[2],'git_dirty':sys.argv[3]}, sys.stdout)" \
  "$git_commit" "$git_branch" "$git_dirty"
