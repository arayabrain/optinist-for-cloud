# ============================================================================
# Terraform apply provenance
# ============================================================================
# Records which git revision of infrastructure/ was applied, so a running
# deployment can be traced back to its source revision. This mirrors the Docker
# /app/BUILD_INFO concept (image provenance) at the infrastructure layer.
#
# The values are gathered at apply time by scripts/terraform_build_info.sh and
# stamped onto the ECS cluster as tags (see aws_ecs_cluster.main in compute.tf).
# The tags only change when the values they record change — the commit, or the
# checkout state it was applied from — so re-applying from the same checkout
# produces no diff and only the single ECS cluster resource churns on a real
# deploy. Note that the same commit applied first from a branch and then from a
# tag does change TfGitBranch and TfGitTag while TfGitCommit stays put: the
# commit is not the only recorded value.
#
# Inspect from the running environment with:
#   aws ecs describe-clusters --clusters <cluster-name> --include TAGS \
#     --query 'clusters[0].tags' --output table
# ============================================================================

data "external" "tf_build_info" {
  program = ["bash", "${path.module}/../scripts/terraform_build_info.sh"]
}
