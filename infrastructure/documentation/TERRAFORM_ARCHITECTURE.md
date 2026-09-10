# Terraform Architecture

> See also: [Deployment](INFRA_DEPLOYMENT_PROCEDURE.md) | [Security](INFRA_SECURITY_MODEL.md)

## Executive Summary

- **Single parameterized codebase** manages both production and development AWS environments
- **Separate S3 backends** per environment ensure complete state isolation — no accidental cross-environment destruction
- **Variable-driven configuration** controls resource naming, VPC CIDRs, domain/SSL settings, and scaling parameters
- **Conditional resources** (Route53, ACM, HTTPS) are enabled for production and disabled for development
- **Provider `default_tags`** automatically tag every AWS resource with `Environment`, `ManagedBy`, and `Project`

---

## Directory Structure

```
infrastructure/terraform/
├── main.tf                      # Provider, variables, locals, data sources, outputs
├── infrastructure.tf            # VPC, subnets, NAT, S3, EFS, RDS, RDS Proxy
├── compute.tf                   # ALB, ECS cluster, ASG, launch templates, services, task definitions
├── compute_domain.tf            # Route53, ACM certificate (conditional — production only)
├── security.tf                  # IAM roles/policies, security groups, key pairs, Secrets Manager
├── monitoring.tf                # CloudWatch log groups, alarms, dashboard
├── deployment.tf                # SSM document + association running app_setup.sh on each host every 30 min
├── background_service.tf        # Background job ECS service and task definition
├── premium_manager.tf           # Premium tier Lambda functions and scheduling
├── free_manager.tf              # Free tier Lambda functions and scheduling
├── common_user_manager.tf       # Shared user lifecycle Lambda function
├── lambda_layers.tf             # Shared Lambda layer (aws_constants)
├── deploy_info.tf               # Apply-time git provenance → ECS cluster tags (see "Deployment Provenance")
│
├── backends/
│   ├── production.hcl           # S3 backend config → subscr-optinist-for-cloud-tfstate
│   └── development.hcl          # S3 backend config → development-optinist-for-cloud-tfstate
│
├── environments/
│   ├── production.tfvars.example    # Production variable template (secrets redacted)
│   ├── development.tfvars.example   # Development variable template (placeholders)
│   ├── production.tfvars            # Actual production values (gitignored, stored in Google Drive)
│   └── development.tfvars           # Actual development values (gitignored, stored in Google Drive)
│
├── *_package/                   # Lambda function source code directories
│
└── .terraform/                  # Auto-generated — provider plugins, state config (gitignored)
```

### Shared Resources (already exist, not created by Terraform)

The following resources are **pre-existing** and shared across environments. They are **not created or destroyed** by `terraform apply` / `terraform destroy`:

| Resource                    | Status          | Location / Notes                                                                                                                                                 |
| --------------------------- | --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **S3 state buckets**        | Already created | `subscr-optinist-for-cloud-tfstate` (prod), `development-optinist-for-cloud-tfstate` (dev). These persist across all testing rounds to preserve Terraform state. |
| **ECR repository**          | Already created | `637423646530.dkr.ecr.ap-northeast-1.amazonaws.com/optinist-for-cloud` — shared between prod and dev                                                             |
| **Firebase project (prod)** | Already created | Production Firebase project in [Firebase Console](https://console.firebase.google.com/)                                                                          |
| **Firebase project (dev)**  | Already created | Separate development Firebase project. Config stored in `development.tfvars`                                                                                     |
| **Stripe account**          | Already created | Test mode keys for dev, live mode keys for prod. Keys in [Stripe Dashboard](https://dashboard.stripe.com/)                                                       |
| **tfvars files**            | Already created | Stored in Google Drive: [https://drive.google.com/drive/folders/1FBIAqBjIdzkXCvNKKGgKu17J4-O3dX-F]. Download to `environments/` before running Terraform         |

---

## What Gets Destroyed vs What Persists

When you run `terraform destroy`, only the AWS resources managed by Terraform are removed. External dependencies and configuration files persist:

| Destroyed by `terraform destroy`         | NOT destroyed (persists)                                      |
| ---------------------------------------- | ------------------------------------------------------------- |
| VPC, subnets, route tables               | S3 tfstate bucket (keeps your state history)                  |
| EC2 instances (NAT, ASG, premium)        | ECR repository and Docker images                              |
| RDS instance and RDS Proxy               | Firebase project and user accounts                            |
| ALB, target groups, listeners            | Stripe products, prices, and webhook configs                  |
| ECS cluster, services, task definitions  | `*.tfvars` files (local files on your machine / Google Drive) |
| S3 app storage bucket                    | `backends/*.hcl` files (checked into git)                     |
| EFS filesystem                           | `*.tfvars.example` files (checked into git)                   |
| Lambda functions and layers              | AWS IAM user credentials (if saved locally)                   |
| Secrets Manager secrets                  |                                                               |
| CloudWatch log groups, alarms, dashboard |                                                               |
| IAM roles, policies, user                |                                                               |
| Security groups                          |                                                               |
| Route53/ACM (production only)            |                                                               |

**Important**: The S3 tfstate bucket is **never destroyed** by `terraform destroy`. It is kept permanently so you can track state history and re-create the environment. If you ever need to remove it, do so manually with `aws s3 rb s3://development-optinist-for-cloud-tfstate --force`.

---

## Key Variables

These variables control environment-specific behavior:

| Variable                | Type   | Description                     | Production             | Development                   |
| ----------------------- | ------ | ------------------------------- | ---------------------- | ----------------------------- |
| `environment`           | string | Resource name prefix            | `"subscr"`             | `"development"`               |
| `enable_custom_domain`  | bool   | Toggle Route53/ACM/HTTPS        | `true`                 | `false`                       |
| `vpc_cidr`              | string | VPC CIDR block                  | `"10.1.0.0/16"`        | `"10.2.0.0/16"`               |
| `s3_user_bucket_prefix` | string | Per-user S3 bucket IAM wildcard | `"optinist-user"`      | `"development-optinist-user"` |
| `frontend_domain`       | string | Custom domain                   | `"araya-optinist.com"` | `""` (uses ALB DNS)           |
| `frontend_protocol`     | string | HTTP or HTTPS                   | `"https"`              | `"http"`                      |
| `frontend_port`         | string | Listener port                   | `"443"`                | `"80"`                        |
| `asg_max_size`          | number | Max ASG instances               | `3`                    | `2`                           |

### Database Username (`mysql_user`)

> **Caution:** Changing `mysql_user` requires destroying and recreating the RDS database instance. Do not rename the database user on a running environment unless you are prepared for full data loss and re-provisioning.

### How Resource Names Are Generated

```hcl
locals {
  env_prefix = "${var.environment}-optinist"
}

# Examples:
# Production: subscr-optinist-app-storage, subscr-optinist-cloud-cluster
# Development: development-optinist-app-storage, development-optinist-cloud-cluster
```

---

## Deployment Provenance (Apply Traceability)

To make the *actually-applied* infrastructure version verifiable from the running
environment, each `terraform apply` records which `infrastructure/` git revision it was
applied from. This mirrors the Docker `/app/BUILD_INFO` concept (image provenance) at the
infrastructure layer.

| File | Role |
| --- | --- |
| `scripts/terraform_build_info.sh` | Emits the apply-time git commit/branch/dirty as JSON (no `jq` dependency) |
| `deploy_info.tf` | `data.external.tf_build_info` runs the script at apply time |
| `compute.tf` (`aws_ecs_cluster.main`) | Stamps `TfGitCommit` / `TfGitBranch` tags from that data |

Design notes:

- The commit is stamped onto a **single** long-lived resource (the ECS cluster), not via
  `provider.default_tags`, so only that one resource changes on a real deploy instead of
  every taggable resource.
- The tag value changes **only when the git commit changes**, so no-op applies produce no
  diff.
- No timestamp is stored in the tag — "when was the last change-bearing apply" is already
  answered by the state file's `LastModified` in the S3 backend bucket.

See [INFRA_DEPLOYMENT_PROCEDURE.md](INFRA_DEPLOYMENT_PROCEDURE.md) → "Check Which Git
Revision Was Applied" for how to read it back.

---

## How `enable_custom_domain` Works

The `enable_custom_domain` variable controls whether Route53, ACM (SSL certificate), and HTTPS are set up. This has a significant impact on how the application is accessed.

### Production (`enable_custom_domain = true`)

```
User → https://araya-optinist.com
         │
         ▼
    Route53 (DNS)
         │  A record → ALB
         ▼
    ALB Listener (port 443, HTTPS)
         │  SSL terminated with ACM certificate
         │  TLS 1.3
         ▼
    ECS Target Group (port 8000)

    ALB Listener (port 80, HTTP)
         │  Redirects to HTTPS (301)
         ▼
    https://araya-optinist.com
```

**Resources created**: Route53 hosted zone, ACM certificate, DNS validation records, A records (apex + www), HTTPS listener with SSL.

### Development (`enable_custom_domain = false`)

```
User → http://<ALB-DNS-NAME>
         │
         ▼
    ALB Listener (port 80, HTTP)
         │  Forwards directly to target group (no redirect)
         ▼
    ECS Target Group (port 8000)

    ALB Listener (port 8080, HTTP)
         │  Secondary listener, also forwards to target group
         ▼
    ECS Target Group (port 8000)
```

**Resources NOT created**: No Route53, no ACM certificate, no DNS records, no SSL. The ALB uses plain HTTP only.

### Why HTTPS Is Not Enabled in Development

The reason is operational complexity, not a technical limitation. HTTPS requires a stable domain name, and the dev environment's ALB DNS changes every time it is destroyed and recreated (`development-optinist-lb-XXXXXXXXXX.ap-northeast-1.elb.amazonaws.com`).

Enabling HTTPS would require these additional AWS resources:

| Resource | Cost | Issue |
|----------|------|-------|
| **Route53 Hosted Zone** | ~$0.50/month | Requires owning a domain. The dev environment would need either a subdomain of `araya-optinist.com` (e.g., `dev.araya-optinist.com`) — which risks accidental production DNS changes — or a separate purchased domain (ongoing management overhead). |
| **ACM Certificate** | Free | DNS validation requires Route53 records, which ties back to the domain ownership issue. |
| **DNS Validation Records** | — | Must remain active for certificate renewal. If the environment is frequently destroyed and recreated, certificate validation must be re-done each time (takes 5–30 minutes). |

The core blocker is domain management, not cost (~$0.50/month is negligible). A custom domain would give the dev environment a stable URL, but that means either sharing the production Route53 zone (adds risk) or purchasing a separate domain (adds management overhead). Neither is justified for a short-lived test environment.

---

## How Firebase Configuration Flows

Firebase config reaches the **backend only** (Python API, runtime). The frontend has no Firebase dependency and no `REACT_APP_FIREBASE_*` variables: every Firebase call is proxied by the backend, as described in [FIREBASE_AUTH_ARCHITECTURE.md](FIREBASE_AUTH_ARCHITECTURE.md). The source of truth is the `firebase_config_json` and `firebase_private_json` variables in each environment's `.tfvars` file, which Terraform stores in AWS Secrets Manager.

### Config Flow Diagram

```
tfvars (firebase_config_json / firebase_private_json)
    │
    ▼
Secrets Manager
    ├── ${env}-optinist/firebase/config        (web config JSON)
    └── ${env}-optinist/firebase/private-key   (service account JSON)
    │
    ▼
cloud-startup.sh   (RUNTIME, backend)
    │
    │  Reads from Secrets Manager, writes to
    │  /app/studio/config/auth/firebase_config.json
    │                          firebase_private.json
    ▼
Python API (loaded at startup)
```

### Why This Is Necessary

The Docker image is shared across environments (single ECR repository) and contains **no** Firebase credentials at all: `.dockerignore` excludes `studio/config/auth/*.json` and keeps only the `*.example.json` templates. `cloud-startup.sh` creates both real files at container startup from the correct environment's secrets.

The failure mode therefore is not "wrong Firebase project", it is "no credentials". If the fetch fails, `firebase_config.json` and `firebase_private.json` never exist, Admin SDK initialization fails silently, and every authenticated request returns `401`. Note that the `Using defaults` wording in the script's own warning is misleading: there are no defaults to fall back on. See Edge Case 1 in [FIREBASE_AUTH_ARCHITECTURE.md](FIREBASE_AUTH_ARCHITECTURE.md).

### IAM Permissions

ECS containers authenticate to AWS using the **ECS Task Role**, delivered by the ECS agent via `AWS_CONTAINER_CREDENTIALS_RELATIVE_URI` — not static IAM user keys. (Earlier task definitions injected `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from Secrets Manager; that injection has since been removed in favor of the Task Role.)

A separate IAM user (`${var.environment}-optinist-cloud-user`) and its access key still exist in Terraform but are no longer injected into any container. Its policy is scoped, for example:

```hcl
secretsmanager:GetSecretValue → ${var.environment}-optinist/*
```

This scoping ensures the development IAM user can only read development secrets, not production secrets.

---

## Environment Differences

### What Changes Between Production and Development

| Aspect                          | Production (`subscr`)               | Development (`development`)                                   |
| ------------------------------- | ----------------------------------- | ------------------------------------------------------------- |
| **Access URL**                  | `https://araya-optinist.com`        | `http://<ALB-DNS-NAME>` (see `terraform output alb_dns_name`) |
| **VPC CIDR**                    | `10.1.0.0/16`                       | `10.2.0.0/16`                                                 |
| **Custom domain**               | `araya-optinist.com` (HTTPS)        | ALB DNS name (HTTP)                                           |
| **Route53 / ACM**               | Created                             | Not created (`enable_custom_domain = false`)                  |
| **ALB HTTP listener (port 80)** | Redirects to HTTPS (301)            | Forwards directly to target group                             |
| **ALB HTTPS listener**          | Port 443, TLS 1.3, ACM cert         | Port 8080, plain HTTP, no cert                                |
| **ASG max instances**           | 3                                   | 2                                                             |
| **S3 state bucket**             | `subscr-optinist-for-cloud-tfstate` | `development-optinist-for-cloud-tfstate`                      |
| **Stripe keys**                 | Live mode                           | Test mode                                                     |
| **Firebase project**            | Production project                  | Separate dev project                                          |
| **Resource name prefix**        | `subscr-optinist-*`                 | `development-optinist-*`                                      |

### Subnet CIDRs

Subnets are derived automatically via `cidrsubnet(var.vpc_cidr, 4, N)`:

| Subnet    | Production      | Development     |
| --------- | --------------- | --------------- |
| VPC       | `10.1.0.0/16`   | `10.2.0.0/16`   |
| Public 1  | `10.1.0.0/20`   | `10.2.0.0/20`   |
| Public 2  | `10.1.16.0/20`  | `10.2.16.0/20`  |
| Private 1 | `10.1.128.0/20` | `10.2.128.0/20` |
| Private 2 | `10.1.144.0/20` | `10.2.144.0/20` |

### What Stays the Same

- AWS region (`ap-northeast-1`)
- ECR repository (shared between environments)
- Architecture (VPC, subnets, NAT, RDS, ECS, ALB, Lambda functions)
- Lambda function code and scheduling intervals
- Security group rules structure
- Monitoring alarms and dashboard layout

---

## AWS Resources Created Per Environment

| Category           | Resources                                                                                    | Named As                                                           |
| ------------------ | -------------------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| **Networking**     | VPC, 2 public + 2 private subnets, IGW, NAT instances, route tables                          | `${env}-optinist-cloud-*`                                          |
| **Compute**        | ECS cluster, ASG, launch template, 4 ECS services (main, premium, public, background), EC2 premium instances | `${env}-optinist-cloud-*`                          |
| **Load Balancing** | ALB, target groups, listeners                                                                | `${env}-optinist-lb`, `${env}-optinist-tg`                         |
| **Database**       | RDS MySQL, RDS Proxy, subnet group                                                           | `${env}-optinist-rds-*`                                            |
| **Storage**        | S3 bucket, EFS filesystem                                                                    | `${env}-optinist-app-storage`, `${env}-optinist-cloud-snmk-volume` |
| **Lambda**         | 5 functions (premium-manager, free-manager, common-user-manager, free-cleanup, cost-tracker) | `${env}-premium-manager`, `${env}-free-manager`, etc.              |
| **Secrets**        | 5 Secrets Manager secrets (firebase, database, app, stripe config)                           | `${env}-optinist/firebase/config`, etc.                            |
| **Monitoring**     | CloudWatch log groups, alarms, dashboard                                                     | `${env}-optinist-*`                                                |
| **DNS/SSL**        | Route53 zone, ACM certificate (production only)                                              | `araya-optinist.com`                                               |
| **IAM**            | Task execution role, task role, instance role, IAM user, Lambda roles                        | `${env}-optinist-*`, `${env}-*`                                    |
| **Background**     | Background ECS service, launch template, ASG                                                 | `${env}-optinist-background-*`                                     |

---

## Development Environment Lifecycle

### When to Create / Destroy

| Scenario                     | Action                                         |
| ---------------------------- | ---------------------------------------------- |
| Testing a new release        | `terraform apply` → test → `terraform destroy` |
| PR review with infra changes | Create, test, destroy                          |
| Long-running QA              | Keep alive, destroy when done                  |
| Cost concern                 | Always destroy when not in use                 |

### Cost Considerations

The development environment runs the same infrastructure as production (VPC, RDS, ECS, ALB, NAT instances, Lambda functions). **Destroy it when not in use** to avoid unnecessary costs. Key cost drivers:

- **RDS instance** — runs 24/7 while the environment is up
- **NAT instances** — 2x t3.nano running continuously
- **EC2 instances** — ASG maintains at least 1 instance
- **ALB** — hourly charge while provisioned
