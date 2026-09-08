# Provider configuration
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Environment = local.environment_label
      ManagedBy   = "terraform"
      Project     = "optinist-cloud"
    }
  }
}

terraform {
  backend "s3" {
    # Backend configuration is provided via -backend-config=backends/<env>.hcl
    # dynamodb_table = "terraform-state-lock"  # Uncomment after initial bootstrap
    # encrypt        = true                     # Uncomment after initial bootstrap
  }
}


# Variables
variable "aws_region" {
  description = "AWS region to deploy resources"
  default     = ""
}
variable "availability_zone" {
  description = "Availability zone for the subnet"
  default     = ""
}

variable "environment" {
  description = "Environment name prefix for resource naming (e.g. subscr, development)"
  type        = string

  validation {
    condition     = length(var.environment) > 0
    error_message = "The environment variable must not be empty. Use 'subscr' for production or 'development' for development."
  }
}

variable "enable_custom_domain" {
  description = "Toggle Route53/ACM/HTTPS resources (false for dev environments)"
  type        = bool
  default     = true
}

variable "vpc_cidr" {
  description = "VPC CIDR block"
  type        = string
  default     = "10.1.0.0/16"
}

variable "s3_user_bucket_prefix" {
  description = "Prefix for per-user S3 bucket wildcard in IAM policies"
  type        = string
  default     = "optinist-user"
}

variable "s3_user_bucket_secret" {
  description = "Secret seed for deterministic per-user S3 bucket name hashing"
  type        = string
  sensitive   = true
}

# Database configuration
variable "mysql_root_password" {
  description = "MySQL/MariaDB root password"
  type        = string
  default     = ""
  sensitive   = true
}

variable "mysql_database" {
  description = "MySQL/MariaDB database name"
  type        = string
  default     = ""
}

variable "mysql_user" {
  description = "MySQL/MariaDB user"
  type        = string
  default     = ""
}

variable "mysql_password" {
  description = "MySQL/MariaDB password"
  type        = string
  default     = ""
  sensitive   = true
}

variable "optinist_org_name" {
  description = "Name for initial organization"
  type        = string
}

variable "optinist_admin_name" {
  description = "Name for initial admin user"
  type        = string
}

variable "optinist_admin_email" {
  description = "Email for initial admin user"
  type        = string
}

variable "optinist_admin_uid" {
  description = "Firebase UID for initial admin user"
  type        = string
}

variable "optinist_secret_key" {
  description = "Secret key for OptiNiSt"
  type        = string
  sensitive   = true
}

variable "stripe_secret_key" {
  description = "Stripe secret key for API authentication"
  type        = string
  sensitive   = true
}

variable "stripe_webhook_secret" {
  description = "Stripe webhook secret for validating webhook events"
  type        = string
  sensitive   = true
}

variable "routing_secret_key" {
  description = "Secret key for generating non-reversible routing IDs (HMAC-SHA256)"
  type        = string
  sensitive   = true
}

variable "firebase_config_json" {
  description = "Firebase web configuration JSON"
  type        = string
  sensitive   = true
}

variable "firebase_private_json" {
  description = "Firebase service account private key JSON"
  type        = string
  sensitive   = true
}

variable "test_users" {
  description = "Test user configuration with Firebase UIDs and subscription details"
  type = list(object({
    email                = string
    name                 = string
    firebase_uid         = string
    subscription_plan_id = number
    role_id              = number
    storage_quota_gb     = number
  }))
  default   = []
  sensitive = true
}

variable "subscription_plans" {
  description = "Subscription plan configurations with Stripe integration details"
  type = list(object({
    id                = number
    name              = string
    price             = number
    billing_cycle     = number
    currency          = number
    status            = number
    stripe_product_id = string
    stripe_price_id   = string
    storage_quota_gb  = number
    features = map(list(object({
      text      = string
      isPremium = bool
    })))
  }))
  default   = []
  sensitive = true
}

variable "git_repo" {
  description = "Git repository"
  type        = string
}

variable "git_branch" {
  description = "Git branch to checkout"
  type        = string
}

variable "ecr_repository_url" {
  description = "ECR repository URL for a pre-existing repo (production). If empty, Terraform creates a new repo named <environment>-optinist-for-cloud."
  type        = string
  default     = ""
}

variable "docker_image_tag" {
  description = "Docker image tag to deploy (use env-specific tags to isolate dev from prod)"
  type        = string
  default     = "latest"
}

variable "asg_min_size" {
  description = "Minimum number of instances in ASG"
  type        = number
  default     = 1
}

variable "asg_max_size" {
  description = "Maximum number of instances in ASG"
  type        = number
  default     = 3
}

variable "asg_desired_capacity" {
  description = "Desired number of instances in ASG"
  type        = number
  default     = 1
}

# Instance type configuration
variable "free_instance_type" {
  description = "Instance type for free tier instances"
  type        = string
  default     = "t3.large"
}

variable "premium_instance_type" {
  description = "Instance type for premium tier instances"
  type        = string
  default     = "t3.large"
}

variable "premium_backend_port" {
  description = "Premium studio container listen port. Single source of truth for the premium task def's containerPort/hostPort/BACKEND_PORT and the premium_manager/cleanup Lambdas' BACKEND_PORT env, so the reconciler's networkBindings filter cannot drift from the live port mapping."
  type        = number
  default     = 8000
}

variable "reconcile_premium_tg_ports_enabled" {
  description = "Kill-switch for the premium target-group port reconciler in handle_scheduled_monitoring. Set false to disable without redeploying code."
  type        = bool
  default     = true
}

variable "background_instance_type" {
  description = "Instance type for background service instance"
  type        = string
  default     = "t3.micro"
}

variable "public_instance_type" {
  description = "EC2 instance type for the public ASG (serves SPA shell and public dataview API; no workflows)"
  type        = string
  default     = "t3.small"
}

variable "public_asg_min_size" {
  description = "Minimum size of the public ASG. desired=min=2 provides HA on SPA delivery."
  type        = number
  default     = 2
}

variable "public_asg_max_size" {
  description = "Maximum size of the public ASG."
  type        = number
  default     = 4
}

variable "public_asg_desired_capacity" {
  description = "Desired capacity of the public ASG (also used for ECS service desired_count)."
  type        = number
  default     = 2
}

# Frontend domain configuration
variable "frontend_domain" {
  description = "Custom domain name for the frontend application"
  type        = string
  default     = "araya-optinist.com"
}

variable "frontend_protocol" {
  description = "Protocol for frontend access (http or https)"
  type        = string
  default     = "https"
}

variable "frontend_port" {
  description = "Port for frontend access (80 for http, 443 for https)"
  type        = string
  default     = "443"
}

variable "admin_storage_quota_bytes" {
  description = "Storage quota for admin user in bytes"
  type        = number
  default     = 214748364800 # 200 GB
}

variable "enable_second_nat" {
  description = "Whether to create a second NAT instance for AZ redundancy"
  type        = bool
  default     = true
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget in USD. Alert fires when projected spend exceeds this."
  type        = number
}

variable "enable_dev_schedule" {
  description = "Enable scheduled start/stop for dev environment (08:00-22:00 JST Mon-Fri)"
  type        = bool
  default     = false
}

variable "dev_schedule_stop_mode" {
  description = "RDS shutdown mode: 'stop' (fast resume, EBS still billed) or 'destroy' (snapshot + delete, max savings)"
  type        = string
  default     = "destroy"

  validation {
    condition     = contains(["stop", "destroy"], var.dev_schedule_stop_mode)
    error_message = "dev_schedule_stop_mode must be 'stop' or 'destroy'."
  }
}

variable "use_custom_ami" {
  description = "Use pre-baked custom AMI from Image Builder instead of stock ECS-optimized AMI"
  type        = bool
  default     = false
}

# Data sources
data "aws_caller_identity" "current" {}
data "aws_elb_service_account" "main" {}

locals {
  # Known environment prefix values — single source of truth for Terraform.
  # Python equivalent: PremiumInstanceConfig.PRODUCTION_ENV_PREFIX in aws_constants.py
  production_env_prefix = "subscr"

  env_prefix        = "${var.environment}-optinist"
  environment_label = var.environment == local.production_env_prefix ? "Production" : "Development"

  # Resolve frontend host/port from ALB DNS when no custom domain is configured
  # - Production: uses custom domain on port 443
  # - Development: uses ALB DNS name on port 8080
  effective_frontend_domain = var.enable_custom_domain ? var.frontend_domain : aws_lb.autoscaling.dns_name
  effective_frontend_port   = var.enable_custom_domain ? var.frontend_port : "8080"
}

# =======
# Outputs
# =======
output "environment" {
  description = "Current environment name"
  value       = var.environment
}

output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.main.id
}

output "vpc_cidr" {
  description = "CIDR block of the VPC"
  value       = aws_vpc.main.cidr_block
}

output "public_subnet_ids" {
  description = "IDs of public subnets"
  value = {
    public1 = aws_subnet.public1.id
    public2 = aws_subnet.public2.id
  }
}

output "private_subnet_ids" {
  description = "IDs of private subnets"
  value = {
    private1 = aws_subnet.private1.id
    private2 = aws_subnet.private2.id
  }
}

output "public_subnet_cidrs" {
  description = "CIDR blocks of public subnets"
  value = {
    public1 = aws_subnet.public1.cidr_block
    public2 = aws_subnet.public2.cidr_block
  }
}

output "private_subnet_cidrs" {
  description = "CIDR blocks of private subnets"
  value = {
    private1 = aws_subnet.private1.cidr_block
    private2 = aws_subnet.private2.cidr_block
  }
}

output "rds_endpoint" {
  description = "RDS instance endpoint (autoscaling)"
  value       = aws_db_instance.main.endpoint
}

output "rds_security_group_id" {
  value = aws_security_group.rds.id
}

output "ecs_security_group_id" {
  value = aws_security_group.ecs.id
}

output "alb_dns_name" {
  description = "ALB DNS name"
  value       = aws_lb.autoscaling.dns_name
}

output "docker_image_tag" {
  description = "Docker image tag used by this environment"
  value       = var.docker_image_tag
}

output "ecs_cluster_name" {
  description = "Name of the ECS cluster"
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name_autoscaling" {
  description = "Name of the ECS service (free tier)"
  value       = aws_ecs_service.autoscaling.name
}

output "ecs_service_name_premium" {
  description = "Name of the ECS service (premium tier)"
  value       = aws_ecs_service.premium.name
}

output "efs_id" {
  description = "ID of the EFS file system"
  value       = aws_efs_file_system.snmk.id
}

output "app_storage_bucket" {
  description = "S3 bucket for application storage (autoscaling)"
  value       = aws_s3_bucket.app_storage.id
}

output "asg_name" {
  description = "Name of the Auto Scaling Group"
  value       = aws_autoscaling_group.main.name
}

output "launch_template_id" {
  description = "ID of the Launch Template"
  value       = aws_launch_template.ecs.id
}

# Configuration Outputs
output "frontend_config_autoscaling" {
  description = "Configuration values for frontend/.env.production"
  value = {
    REACT_APP_SERVER_HOST             = aws_lb.autoscaling.dns_name
    REACT_APP_SERVER_PORT             = "80"
    REACT_APP_SERVER_PROTO            = "http"
    REACT_APP_EXPDB_METADATA_EDITABLE = true
  }
}


output "backend_config" {
  description = "Configuration values for studio/auth/config/.env"
  value = {
    S3_DEFAULT_BUCKET_NAME = aws_s3_bucket.app_storage.id
  }
}

# Output the key information
output "ssh_key_name" {
  description = "Name of the generated SSH key pair"
  value       = aws_key_pair.subscr_optinist_cloud_key_pair.key_name
}

output "ssh_private_key_path" {
  description = "Path to the private key file"
  value       = local_file.private_key.filename
}

output "ssh_command" {
  description = "SSH command to connect to instances"
  value       = "ssh -i ${local_file.private_key.filename} ec2-user@<INSTANCE_IP>"
}

output "alb_arn" {
  description = "ARN of the main ALB for premium instance routing"
  value       = aws_lb.autoscaling.arn
}

output "alb_listener_arn" {
  description = "ARN of the main ALB HTTPS listener for premium routing rules"
  value       = aws_lb_listener.autoscaling_https.arn
}

output "premium_instance_ids" {
  description = "IDs of the premium standby instances"
  value       = aws_instance.premium[*].id
}

output "test_users" {
  description = "Test user configuration for load testing (includes Firebase UIDs)"
  value       = var.test_users
  sensitive   = true
}

# Route53 and SSL outputs
output "domain_name" {
  description = "Effective domain name for the application (ALB DNS in dev, custom domain in prod)"
  value       = local.effective_frontend_domain
}

output "domain_url" {
  description = "Full URL for the application"
  value       = "${var.frontend_protocol}://${local.effective_frontend_domain}"
}

output "domain_protocol" {
  description = "Protocol for the application (http or https)"
  value       = var.frontend_protocol
}

output "domain_port" {
  description = "Effective port for the application (8080 in dev, frontend_port in prod)"
  value       = local.effective_frontend_port
}

output "acm_certificate_arn" {
  description = "ARN of the ACM certificate for HTTPS"
  value       = var.enable_custom_domain ? aws_acm_certificate.main[0].arn : null
}

output "acm_certificate_status" {
  description = "Validation status of the ACM certificate"
  value       = var.enable_custom_domain ? aws_acm_certificate.main[0].status : null
}

output "route53_zone_id" {
  description = "Route53 hosted zone ID"
  value       = var.enable_custom_domain ? data.aws_route53_zone.main[0].zone_id : null
}

output "alb_listener_https_arn" {
  description = "ARN of the HTTPS ALB listener"
  value       = aws_lb_listener.autoscaling_https.arn
}

output "rds_proxy_endpoint" {
  value = aws_db_proxy.main.endpoint
}
