#!/bin/bash
set -e

# Common Configuration
REGION="ap-northeast-1"
TERRAFORM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../terraform" && pwd)"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# ===========================================
# Parse arguments
# ===========================================
# Usage: ./ecr_build_push.sh [--tag <version-tag>] [--yes] [--deploy]
#   --tag <tag>  : Custom version tag (default: auto-generated YYYYMMDD-HHMMSS-<git-sha>)
#   --yes        : Skip confirmation prompt
#   --deploy     : After push, force-new-deployment on EVERY service in the
#                  cluster so all tiers re-pull the just-pushed :latest
CUSTOM_TAG=""
SKIP_CONFIRM=false
DEPLOY=false

while [[ $# -gt 0 ]]; do
    case $1 in
        --tag)
            CUSTOM_TAG="$2"
            shift 2
            ;;
        --yes|-y)
            SKIP_CONFIRM=true
            shift
            ;;
        --deploy)
            DEPLOY=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: ./ecr_build_push.sh [--tag <version-tag>] [--yes] [--deploy]"
            exit 1
            ;;
    esac
done

# ===========================================
# Guard: reject builds from a dirty worktree
# ===========================================
DIRTY_FILES=$(git -C "$(git rev-parse --show-toplevel)" status --porcelain)
if [ -n "$DIRTY_FILES" ]; then
    echo "ERROR: Working tree is not clean. Commit or stash changes before building."
    echo ""
    echo "$DIRTY_FILES"
    echo ""
    echo "This check ensures builds are reproducible from a clean commit."
    echo "Note: .gitignored secrets (.env, firebase JSONs) are excluded by .dockerignore, not this check."
    exit 1
fi

# ===========================================
# Detect environment and ECR target
# ===========================================
echo "Reading Terraform outputs..."
ENVIRONMENT=$(terraform -chdir="$TERRAFORM_DIR" output -raw environment 2>/dev/null || echo "")
ECR_URI=$(terraform -chdir="$TERRAFORM_DIR" output -raw ecr_repository_url 2>/dev/null || echo "")

if [ -z "$ENVIRONMENT" ]; then
    echo "ERROR: Could not read environment from Terraform output."
    echo "Make sure you have initialized Terraform with the correct backend:"
    echo "  terraform init -backend-config=backends/development.hcl"
    echo "  terraform init -backend-config=backends/production.hcl"
    exit 1
fi

if [ -z "$ECR_URI" ]; then
    echo "ERROR: Could not read ecr_repository_url from Terraform output."
    echo "Make sure you have run 'terraform apply' for the target environment."
    exit 1
fi

REPO_NAME=$(echo "$ECR_URI" | sed 's|.*/||')

# Generate version tag and full commit hash
GIT_SHA=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")
GIT_COMMIT_FULL=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
BUILD_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if [ -n "$CUSTOM_TAG" ]; then
    VERSION_TAG="$CUSTOM_TAG"
else
    VERSION_TAG="$(date +%Y%m%d-%H%M%S)-${GIT_SHA}"
fi

# ===========================================
# Environment confirmation
# ===========================================
echo ""
echo "============================================"
echo "  BUILD AND PUSH CONFIRMATION"
echo "============================================"
echo "  Environment : ${ENVIRONMENT}"
echo "  ECR Repo    : ${REPO_NAME}"
echo "  ECR URI     : ${ECR_URI}"
echo "  Tags        : latest, ${VERSION_TAG}"
echo "  Git commit  : ${GIT_COMMIT_FULL} (${GIT_SHA})"
echo "  Git branch  : ${GIT_BRANCH}"
echo "  Build time  : ${BUILD_TIMESTAMP}"
echo "============================================"
echo ""

# Production environment uses environment="subscr" (see environments/production.tfvars)
PRODUCTION_ENV="subscr"
if [ "$ENVIRONMENT" = "$PRODUCTION_ENV" ]; then
    echo "  *** WARNING: You are pushing to PRODUCTION! ***"
    echo ""
fi

if [ "$SKIP_CONFIRM" = false ]; then
    read -p "Proceed with build and push? (y/N): " CONFIRM
    if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
        echo "Aborted."
        exit 0
    fi
fi

# ===========================================
# Get configuration from Terraform outputs
# ===========================================
AUTOSCALING_HOST=$(terraform -chdir="$TERRAFORM_DIR" output -raw domain_name)
AUTOSCALING_PORT=$(terraform -chdir="$TERRAFORM_DIR" output -raw domain_port)
AUTOSCALING_PROTO=$(terraform -chdir="$TERRAFORM_DIR" output -raw domain_protocol)

echo "Autoscaling Host: $AUTOSCALING_HOST"
echo "Autoscaling Protocol: $AUTOSCALING_PROTO"
echo "Autoscaling Port: $AUTOSCALING_PORT"

# ===========================================
# 1. Build Autoscaling Image with Frontend
# ===========================================
IMAGE_TAG="latest"

echo "Building image for repo: $ECR_URI (repo: $REPO_NAME)"

# Authenticate Docker to ECR (ignore keychain errors on macOS)
ECR_REGISTRY=$(echo "$ECR_URI" | sed 's|/.*||')
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $ECR_REGISTRY 2>&1 | grep -v "error storing credentials" || true

# Verify ECR repository exists (must be created by Terraform, not this script)
if ! aws ecr describe-repositories --repository-names $REPO_NAME --region $REGION >/dev/null 2>&1; then
    echo "ERROR: ECR repository '$REPO_NAME' does not exist."
    echo "The repository must be created by Terraform. Run 'terraform apply' first."
    exit 1
fi

# infrastructure/.env.deploy (gitignored) holds build-time values Terraform does
# not own. Keyed per environment so a development build can never be published
# with the production analytics container. Sourced in a subshell so a stray
# assignment in that file cannot clobber the variables used below.
GTM_ID_VAR="REACT_APP_GTM_ID_$(echo "$ENVIRONMENT" | tr '[:lower:]-' '[:upper:]_')"
REACT_APP_GTM_ID=$(
    set +e
    if [ -f "$REPO_ROOT/infrastructure/.env.deploy" ]; then
        . "$REPO_ROOT/infrastructure/.env.deploy"
    fi
    printf '%s' "${!GTM_ID_VAR:-}"
)

if [ -n "$REACT_APP_GTM_ID" ] && ! [[ "$REACT_APP_GTM_ID" =~ ^GTM-[A-Z0-9]+$ ]]; then
    echo "ERROR: malformed $GTM_ID_VAR ('$REACT_APP_GTM_ID'). Expected GTM-XXXXXXX."
    exit 1
fi

# Build frontend with custom domain for autoscaling
echo "Building frontend for autoscaling with ${AUTOSCALING_PROTO}://${AUTOSCALING_HOST}:${AUTOSCALING_PORT}"
echo "Analytics ($ENVIRONMENT): ${REACT_APP_GTM_ID:-<$GTM_ID_VAR unset, analytics disabled>}"
cd "$REPO_ROOT/frontend"
cat > .env.production << ENV_EOF
REACT_APP_SERVER_HOST=${AUTOSCALING_HOST}
REACT_APP_SERVER_PORT=${AUTOSCALING_PORT}
REACT_APP_SERVER_PROTO=${AUTOSCALING_PROTO}
REACT_APP_EXPDB_METADATA_EDITABLE=true
REACT_APP_GTM_ID=${REACT_APP_GTM_ID:-}
ENV_EOF

yarn install
yarn build
cd "$REPO_ROOT"

# Build the Docker image with embedded build metadata
echo "Building autoscaling Docker image..."
docker build -f studio/config/docker/Dockerfile \
    --build-arg GIT_COMMIT="${GIT_COMMIT_FULL}" \
    --build-arg GIT_BRANCH="${GIT_BRANCH}" \
    --build-arg BUILD_TIMESTAMP="${BUILD_TIMESTAMP}" \
    -t $REPO_NAME:$IMAGE_TAG .

# Tag and push to ECR — both :latest (for ECS) and versioned (for history/rollback)
docker tag $REPO_NAME:$IMAGE_TAG $ECR_URI:latest
docker tag $REPO_NAME:$IMAGE_TAG $ECR_URI:$VERSION_TAG
docker push $ECR_URI:latest
docker push $ECR_URI:$VERSION_TAG

echo ""
echo "============================================"
echo "  PUSH COMPLETE"
echo "============================================"
echo "  Environment : ${ENVIRONMENT}"
echo "  latest      : ${ECR_URI}:latest"
echo "  Version     : ${ECR_URI}:${VERSION_TAG}"
echo "============================================"

# ===========================================
# Optional: force every service to re-pull :latest
# ===========================================
# Runs AFTER the push, so the force always resolves the digest we just pushed.
# Cycles all tiers (main/premium/public/background), not just the main service.
if [ "$DEPLOY" = true ]; then
    CLUSTER=$(terraform -chdir="$TERRAFORM_DIR" output -raw ecs_cluster_name)
    echo ""
    echo "Forcing new deployment on all services in ${CLUSTER}..."
    SERVICES=$(aws ecs list-services --cluster "$CLUSTER" --region "$REGION" \
        --query 'serviceArns[]' --output text)
    for svc in $SERVICES; do
        echo "  -> ${svc##*/}"
        aws ecs update-service --cluster "$CLUSTER" --service "$svc" \
            --force-new-deployment --region "$REGION" >/dev/null
    done
    echo "Force-new-deployment issued for all services (re-pulling ${ECR_URI}:latest)."
    echo "Running services restart now; any service at desiredCount 0 re-pulls on next scale-up."
fi
