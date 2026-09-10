# Infrastructure Management Guide

This document covers **Terraform operations, AWS environment management, and ECR image management**. Use this guide when you need to create, modify, or destroy AWS infrastructure.

> **For application deployment, release procedures, and Git workflow**, see [DEPLOYMENT_PROCEDURE.md](DEPLOYMENT_PROCEDURE.md).

> See also: [Architecture](TERRAFORM_ARCHITECTURE.md) | [Security](INFRA_SECURITY_MODEL.md) | [Dev Schedule](../scripts/DEV_SCHEDULE_GUIDE.md)

---

## Table of Contents

- [Prerequisites](#prerequisites)
- [AWS Profile Configuration](#aws-profile-configuration)
- [First-Time Setup (One-Time Only)](#first-time-setup-one-time-only)
- [Working with Production](#working-with-production)
- [Working with Development](#working-with-development)
- [Switching Between Environments](#switching-between-environments)
- [Destroying Environments](#destroying-environments)
- [ECR Repository Management](#ecr-repository-management)
- [How to Access the Development Site](#how-to-access-the-development-site)
- [Common Terraform Tasks](#common-terraform-tasks)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

```bash
# Install Terraform (v1.0+)
brew install terraform

# Install AWS CLI
brew install awscli

# Install Docker (required for ECR image build/push)
# https://docs.docker.com/get-docker/
```

---

## AWS Profile Configuration

This project uses an AWS account dedicated to OptiNiSt. When your machine has multiple AWS profiles configured (e.g., personal account, other project accounts), you **must** specify the correct profile before running any `aws` or `terraform` command.

### Setting Up a Named Profile

```bash
# Create (or update) a named profile for this project
aws configure --profile optinist
# AWS Access Key ID:     <your-key>       (obtain from team lead)
# AWS Secret Access Key: <your-secret>    (obtain from team lead)
# Default region name:   ap-northeast-1
# Default output format: json
```

This stores credentials in `~/.aws/credentials` and region settings in `~/.aws/config`.

### Activating the Profile

There are two ways to tell CLI tools which profile to use.

#### Option A: Environment Variable (recommended)

Set `AWS_PROFILE` once at the start of your terminal session. **All subsequent `aws` and `terraform` commands will use this profile automatically.**

```bash
export AWS_PROFILE=optinist

# Verify the correct account is active
aws sts get-caller-identity
```

> **Tip:** Add `export AWS_PROFILE=optinist` to your shell configuration file (`~/.bashrc`, `~/.zshrc`) if this is the only AWS project you work on regularly.

#### Option B: Per-Command `--profile` Flag (AWS CLI only)

Pass the profile explicitly with each AWS CLI command:

```bash
aws ecs list-clusters --profile optinist --region ap-northeast-1
```

> **Note:** Terraform does **not** have a `--profile` flag. It reads the profile from the `AWS_PROFILE` environment variable or from the `profile` attribute in the `provider "aws"` block in `main.tf`. For this project, **use `export AWS_PROFILE=...`** before running any `terraform` commands.

### Verifying Your Profile

```bash
# Confirm you are operating on the correct AWS account
aws sts get-caller-identity
# Expected: the account ID and ARN for the OptiNiSt project account
```

---

## First-Time Setup (One-Time Only)

The following steps only need to be done **once**. If another developer has already completed them, skip to [Working with Development](#working-with-development).

### 1. Create S3 bucket for Terraform state

> **Note**: These buckets already exist. This step is only needed if setting up a completely new environment from scratch. The state buckets are **never destroyed** — they persist across all testing rounds.

```bash
# Production state bucket (already exists)
aws s3api create-bucket \
  --bucket subscr-optinist-for-cloud-tfstate \
  --region ap-northeast-1 \
  --create-bucket-configuration LocationConstraint=ap-northeast-1

# Development state bucket (already exists)
aws s3api create-bucket \
  --bucket development-optinist-for-cloud-tfstate \
  --region ap-northeast-1 \
  --create-bucket-configuration LocationConstraint=ap-northeast-1
```

### 2. Create a separate Firebase project (development only)

> **Note**: The development Firebase project already exists. Config is stored in the shared `development.tfvars` on Google Drive.

If creating from scratch:

1. Create a new Firebase project in the [Firebase Console](https://console.firebase.google.com/)
2. Enable Authentication (Email/Password provider)
3. Generate a service account key (Project Settings → Service Accounts → Generate new private key)
4. Update `firebase_config_json` and `firebase_private_json` in `environments/development.tfvars`

### 3. Set up Stripe test mode (development only)

> **Note**: Stripe test mode is already configured. Keys are stored in the shared `development.tfvars` on Google Drive.

If creating from scratch:

1. In the [Stripe Dashboard](https://dashboard.stripe.com/), toggle to "Test mode"
2. Create test products and prices matching the production structure
3. Create a test webhook endpoint pointing to the test ALB
4. Update `stripe_secret_key`, `stripe_webhook_secret`, and plan IDs in `environments/development.tfvars`

### 4. Get tfvars files

Download the tfvars files from Google Drive and place them in the `environments/` directory:

```bash
# Download from Google Drive: [https://drive.google.com/drive/folders/1xUsptIrcWYMAgeqNraRzObG_CUqMXHZh]
# Place files at:
#   infrastructure/terraform/environments/production.tfvars
#   infrastructure/terraform/environments/development.tfvars
```

If the tfvars files don't exist yet, create them from the examples:

```bash
cd infrastructure/terraform/environments
cp development.tfvars.example development.tfvars
# Edit development.tfvars — replace all <PLACEHOLDER> values
```

---

## Working with Production

```bash
cd infrastructure/terraform

# 1. Activate the correct AWS profile
export AWS_PROFILE=optinist

# 2. Initialize — connects to production state bucket
#    Use -reconfigure if you were previously initialized to a different environment
terraform init -backend-config=backends/production.hcl -reconfigure

# 3. Preview changes
terraform plan -var-file=environments/production.tfvars

# 4. Apply changes
terraform apply -var-file=environments/production.tfvars

# 5. View outputs (ALB DNS, RDS endpoint, etc.)
terraform output
```

---

## Working with Development

```bash
cd infrastructure/terraform

# 1. Activate the correct AWS profile
export AWS_PROFILE=optinist

# 2. Initialize — connects to development state bucket
#    Use -reconfigure if you were previously initialized to a different environment
terraform init -backend-config=backends/development.hcl -reconfigure

# 3. Ensure development.tfvars exists (download from Google Drive or copy from example)

# 4. Preview what will be created
terraform plan -var-file=environments/development.tfvars

# 5. Deploy the development environment
terraform apply -var-file=environments/development.tfvars

# 6. Get the development site URL:
terraform output alb_dns_name
# Access at: http://<ALB-DNS-NAME> (redirects to port 8080)
```

---

## Switching Between Environments

**Important**: You must re-initialize when switching environments. This is a safety feature — it prevents accidentally modifying the wrong environment.

```bash
# Switch from dev → production
terraform init -backend-config=backends/production.hcl -reconfigure

# Switch from production → dev
terraform init -backend-config=backends/development.hcl -reconfigure
```

The `-reconfigure` flag tells Terraform to switch backends without migrating state.

---

## Destroying Environments

### Destroying Development

```bash
# Destroy development (safe — only affects dev state)
terraform init -backend-config=backends/development.hcl -reconfigure
terraform destroy -var-file=environments/development.tfvars
```

This destroys all AWS resources but **preserves**: S3 tfstate bucket, Firebase project, Stripe config, tfvars files.

> **Note**: The development ECR repository (`development-optinist-for-cloud`) is managed by Terraform with `force_delete = true`, so `terraform destroy` will delete the repository and all images inside it. If you need to preserve images before destroying, push them to another repository first.

To recreate later, simply run `terraform apply` again — no first-time setup needed.

### Destroying Production

```bash
# DANGER: Destroy production — requires explicit confirmation
terraform init -backend-config=backends/production.hcl -reconfigure
terraform destroy -var-file=environments/production.tfvars
```

---

## ECR Repository Management

### Repository Isolation

Production and development use **separate ECR repositories** to ensure complete image isolation:

| Environment | ECR Repository                   | Managed by                                             |
| ----------- | -------------------------------- | ------------------------------------------------------ |
| Production  | `optinist-for-cloud`             | Pre-existing (outside Terraform)                       |
| Development | `development-optinist-for-cloud` | Terraform (created when `ecr_repository_url` is empty) |

Both environments push to `:latest` within their own repo. A Docker push for dev testing **cannot** affect production.

### Building and Pushing Images

The build script reads the target environment from Terraform output, displays a confirmation prompt, and requires explicit approval before pushing:

```bash
cd infrastructure/scripts

# Ensure the correct AWS profile is active
export AWS_PROFILE=optinist

# Standard usage — auto-generates version tag, asks for confirmation
./ecr_build_push.sh

# Custom version tag
./ecr_build_push.sh --tag v1.2.3

# Skip confirmation (for CI/CD pipelines)
./ecr_build_push.sh --yes
```

The script will display:

```
============================================
  BUILD AND PUSH CONFIRMATION
============================================
  Environment : development
  ECR Repo    : development-optinist-for-cloud
  Tags        : latest, 20260317-143022-a1b2c3d
============================================

Proceed with build and push? (y/N):
```

For production, an additional **WARNING** banner is shown.

- If initialized to **development** → pushes to `development-optinist-for-cloud:latest`
- If initialized to **production** → pushes to `optinist-for-cloud:latest`

Every push creates **two tags**:

- `:latest` — used by ECS task definitions (always current)
- `:YYYYMMDD-HHMMSS-<git-sha>` — immutable version for history and rollback (e.g., `20260317-143022-a1b2c3d`)

### Cycling All Services

**Pushing `:latest` does not deploy anything, and cycling one service does not deploy everywhere.** ECS resolves the image tag to a digest when a deployment is *created*, so existing tasks keep running the digest they started with. A `:latest` retag followed by a single `update-service` updates only that service and leaves every other service on the old image.

The cluster runs several services (currently main, premium, public, and background). Discover them rather than typing the list, so a newly added service is never missed:

```bash
CLUSTER=development-optinist-cloud-cluster
REGION=ap-northeast-1

SERVICES=$(aws ecs list-services --cluster "$CLUSTER" --region "$REGION" \
  --query 'serviceArns[]' --output text)
echo "Cycling: $SERVICES"

for SERVICE in $SERVICES; do
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
    --force-new-deployment --region "$REGION" >/dev/null
done
```

Then confirm every service settled on one deployment, rather than assuming it did:

```bash
aws ecs describe-services --cluster "$CLUSTER" --region "$REGION" \
  --services $SERVICES \
  --query 'services[].{name:serviceName,deployments:length(deployments),running:runningCount}'
```

### Safe Deployment Workflow

1. Switch to dev backend: `terraform init -backend-config=backends/development.hcl -reconfigure`
2. Build and push dev image: `cd ../scripts && ./ecr_build_push.sh`
3. Force ECS redeployment across **every** service, using the loop in [Cycling All Services](#cycling-all-services)
4. Verify in development
5. When ready for production: switch backend, rebuild, push, and redeploy

### Rollback to a Previous Image

List available image versions:

```bash
aws ecr list-images \
  --repository-name development-optinist-for-cloud \
  --region ap-northeast-1 \
  --query 'imageIds[?imageTag!=`latest`].imageTag' \
  --output table
```

Retag a previous version as `:latest` and redeploy:

```bash
# 1. Get the manifest of the version you want to roll back to
MANIFEST=$(aws ecr batch-get-image \
  --repository-name development-optinist-for-cloud \
  --image-ids imageTag=20260316-091500-f4e5d6a \
  --query 'images[0].imageManifest' --output text \
  --region ap-northeast-1)

# 2. Retag it as :latest
aws ecr put-image \
  --repository-name development-optinist-for-cloud \
  --image-tag latest \
  --image-manifest "$MANIFEST" \
  --region ap-northeast-1

# 3. Force ECS to pull the rolled-back image, across EVERY service
CLUSTER=development-optinist-cloud-cluster
REGION=ap-northeast-1

SERVICES=$(aws ecs list-services --cluster "$CLUSTER" --region "$REGION" \
  --query 'serviceArns[]' --output text)

for SERVICE in $SERVICES; do
  aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
    --force-new-deployment --region "$REGION" >/dev/null
done
```

A rollback that cycles only one service is the worst case of the digest-pinning behaviour described in [Cycling All Services](#cycling-all-services): the image you are rolling back *away from* keeps serving on every service you did not cycle. Confirm all of them settled before declaring the rollback complete.

### Image Cleanup

The ECR lifecycle policy automatically manages storage:

- **Untagged images**: removed after 7 days
- **Versioned images**: only the last 10 are kept
- **`:latest`**: always retained

---

## How to Access the Development Site

After `terraform apply`, get the ALB DNS name:

```bash
# Get the ALB DNS name from Terraform outputs
terraform output alb_dns_name

# Example output:
# development-optinist-lb-1234567890.ap-northeast-1.elb.amazonaws.com
```

Access the development site at:

```
http://<ALB-DNS-NAME>
```

This URL is auto-generated by AWS and changes if you destroy and recreate the environment. Port 80 redirects to port 8080 (the main listener). `FRONTEND_SERVER_HOST` and `FRONTEND_SERVER_PORT` are auto-resolved from the ALB DNS name when `enable_custom_domain = false`.

---

## Common Terraform Tasks

### View Current Terraform State

```bash
# List all resources in current environment's state
terraform state list

# Show details of a specific resource
terraform state show aws_ecs_cluster.main
```

### View Outputs

```bash
# All outputs
terraform output

# Specific output
terraform output alb_dns_name
terraform output rds_endpoint

# Sensitive output
terraform output -raw effective_ami_id
```

### Update a Single Resource

```bash
# Target a specific resource (e.g., after changing only the Lambda code)
terraform apply -var-file=environments/development.tfvars -target=aws_lambda_function.free_manager
```

### Import an Existing Resource

```bash
# If a resource was created manually and needs to be managed by Terraform
terraform import -var-file=environments/development.tfvars aws_s3_bucket.app_storage development-optinist-app-storage
```

### Check Which Environment You're Connected To

```bash
# The backend config tells you which state bucket is active
cat .terraform/terraform.tfstate | python3 -c "import sys,json; print(json.load(sys.stdin)['backend']['config']['bucket'])"
```

### Check Which Git Revision Was Applied

Every `terraform apply` stamps the applied `infrastructure/` git revision onto the ECS
cluster as tags (`TfGitCommit` / `TfGitBranch`), so you can confirm which infrastructure
version is actually running and detect deploy mistakes. The tag only changes when the git
commit changes, so no-op applies produce no diff.

> **Why only the ECS cluster is tagged:** the commit is deliberately *not* added to
> `provider.default_tags`. A default tag would apply the value to every taggable resource,
> so each new-commit apply would churn dozens of resources' tags at once. Instead it is
> stamped onto a single long-lived, representative resource — the ECS cluster, which is the
> compute plane the app runs on — so only that one resource changes on a real deploy.

```bash
# Get the cluster name for the active environment from Terraform state
# (unambiguous — returns exactly one name, even when dev and prod share an account)
CLUSTER_NAME=$(terraform output -raw ecs_cluster_name)

# Note: ECS tags use lowercase `key` (unlike EC2's `Key`)
aws ecs describe-clusters --clusters "$CLUSTER_NAME" \
  --include TAGS --region ap-northeast-1 \
  --query 'clusters[0].tags[?starts_with(key, `Tf`)]' --output table
```

> To see *when* the last change-bearing apply ran, check the state file's `LastModified`
> in the environment's state bucket (a no-op apply does not rewrite state, so it reflects
> the last apply that actually changed something). The bucket name matches the active
> environment: `subscr-optinist-for-cloud-tfstate` (production) or
> `development-optinist-for-cloud-tfstate` (development).
>
> ```bash
> aws s3api head-object --bucket <STATE_BUCKET> \
>   --key terraform.tfstate --region ap-northeast-1 --query 'LastModified' --output text
> ```

---

## Troubleshooting

### "Backend configuration changed" error

This happens when switching environments. Use `-reconfigure`:

```bash
terraform init -backend-config=backends/development.hcl -reconfigure
```

### "Error acquiring state lock"

Another Terraform process is running against this environment's state:

```bash
# Check who holds the lock
aws dynamodb scan --table-name terraform-state-lock --region ap-northeast-1

# Force unlock (only if you're sure no other process is running)
terraform force-unlock <LOCK_ID>
```

### "Resource already exists" error

The resource was created outside Terraform. Import it:

```bash
terraform import -var-file=environments/<env>.tfvars <resource_address> <resource_id>
```

### Provider plugin errors during validate

Re-initialize providers:

```bash
terraform init -backend=false    # Just install plugins, skip backend
terraform validate
```

### Development tfvars placeholders

Before first `terraform apply` on development, replace all `<PLACEHOLDER>` values in `environments/development.tfvars`:

| Placeholder           | How to Generate                                                     |
| --------------------- | ------------------------------------------------------------------- |
| MySQL passwords       | `openssl rand -base64 24`                                           |
| `optinist_secret_key` | `openssl rand -hex 32`                                              |
| `routing_secret_key`  | `openssl rand -hex 32`                                              |
| Stripe TEST keys      | From Stripe Dashboard → Developers → API Keys (test mode)           |
| Firebase config       | From Firebase Console → Project Settings → Web app config           |
| Firebase private key  | From Firebase Console → Service Accounts → Generate new private key |
| `optinist_admin_uid`  | Create a test user in Firebase Auth, copy their UID                 |
