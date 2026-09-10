
# =========================
# Application Load Balancer
# =========================
resource "aws_lb" "autoscaling" {
  name               = "${local.env_prefix}-lb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id, aws_security_group.ecs.id]
  subnets            = [aws_subnet.public1.id, aws_subnet.public2.id]

  enable_deletion_protection = false
  # Long timeout for streaming cold-cache S3 fallbacks; pair with TG deregistration_delay.
  idle_timeout = 600

  # Enable access logs for detailed monitoring
  access_logs {
    bucket  = aws_s3_bucket.app_storage.id
    prefix  = "alb-logs"
    enabled = true
  }

  depends_on = [
    aws_s3_bucket_policy.app_storage
  ]

  tags = {
    Name = "${local.env_prefix}-load-balancer"
  }
}

# Load Balancer Listener - HTTP port 80 (redirects to main listener)
# With custom domain: redirect HTTP -> HTTPS (443)
# Without custom domain (dev): redirect to port 8080
# Note: Port 8080 must be open in the ALB security group for dev to work.
# The redirect ensures the browser loads the frontend from the main listener
# port, so all API calls go through the correct port with premium routing rules.
resource "aws_lb_listener" "autoscaling" {
  load_balancer_arn = aws_lb.autoscaling.arn
  port              = "80"
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = var.enable_custom_domain ? "443" : "8080"
      protocol    = var.enable_custom_domain ? "HTTPS" : "HTTP"
      status_code = "HTTP_301"
    }
  }
}


# Default action lands on public; authenticated traffic is steered to free
# by the Authorization-header rule and a few overrides. To change the default
# safely under load, apply listener rules first with `terraform apply -target=...`
# (a depends_on back to the rules would cycle with their listener_arn refs).
resource "aws_lb_listener" "autoscaling_https" {
  load_balancer_arn = aws_lb.autoscaling.arn
  port              = var.enable_custom_domain ? "443" : "8080"
  protocol          = var.enable_custom_domain ? "HTTPS" : "HTTP"
  ssl_policy        = var.enable_custom_domain ? "ELBSecurityPolicy-TLS13-1-2-2021-06" : null
  certificate_arn   = var.enable_custom_domain ? aws_acm_certificate_validation.main[0].certificate_arn : null

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.public.arn
  }
}

# Target Group for ALB
resource "aws_lb_target_group" "autoscaling" {
  name        = "${local.env_prefix}-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "instance"

  health_check {
    enabled             = true
    healthy_threshold   = 2
    unhealthy_threshold = 5
    interval            = 60
    matcher             = "200"
    path                = "/health"
    port                = "traffic-port"
    protocol            = "HTTP"
    timeout             = 30
  }

  stickiness {
    type            = "lb_cookie"
    cookie_duration = 300 # 5 minutes (matches Lambda check interval for fast rebalancing)
    enabled         = true
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "${local.env_prefix}-cloud-target-group"
  }
}

# ======================================
# Launch Template for Auto Scaling Group
# ======================================

# Get the latest ECS-optimized AMI
data "aws_ami" "ecs_optimized" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-ecs-hvm-*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# Custom AMI from Image Builder (when enabled)
# depends_on ensures the SSM parameter resource is created before this
# data source tries to read it on first apply with use_custom_ami = true.
data "aws_ssm_parameter" "custom_ami_id" {
  count      = var.use_custom_ami ? 1 : 0
  name       = "/${var.environment}/optinist/custom-ami-id"
  depends_on = [aws_ssm_parameter.custom_ami_id]
}

locals {
  effective_ami_id = (
    var.use_custom_ami
    ? data.aws_ssm_parameter.custom_ami_id[0].value
    : data.aws_ami.ecs_optimized.id
  )
}

resource "aws_launch_template" "ecs" {
  name_prefix   = "${local.env_prefix}-ecs-"
  image_id      = local.effective_ami_id
  instance_type = var.free_instance_type
  key_name      = aws_key_pair.subscr_optinist_cloud_key_pair.key_name

  vpc_security_group_ids = [aws_security_group.ecs.id]

  iam_instance_profile {
    name = aws_iam_instance_profile.ecs_instance_profile.name
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 88 # Reduced from 120: 32 GB swap moved to dedicated volume
      volume_type = "gp3"
      encrypted   = true
    }
  }

  # Dedicated swap volume — mkswap on a block device takes <1 second
  # vs ~4.5 minutes for dd-based swap file creation on root volume
  block_device_mappings {
    device_name = "/dev/xvds"
    ebs {
      volume_size           = 32
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  monitoring {
    enabled = true
  }

  user_data = base64encode(templatefile("${path.module}/../scripts/ecs-user-data.sh", {
    tier               = "free"
    cluster_name       = aws_ecs_cluster.main.name
    git_branch         = var.git_branch
    git_repo           = var.git_repo
    ecr_registry       = split("/", local.ecr_repository_url)[0]
    ecr_repository_url = local.ecr_repository_url
    efs_id             = aws_efs_file_system.snmk.id
    db_host            = replace(aws_db_instance.main.endpoint, ":3306", "")
    swap_size_mb       = 32768 # 32GB swap for workflow memory spikes
    swap_device_name   = "/dev/xvds"
  }))
  tag_specifications {
    resource_type = "instance"
    tags = {
      Name        = "${local.env_prefix}-asg-instance"
      Type        = "ECS-ASG"
      Service     = "autoscaling"
      Environment = local.environment_label
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      Name        = "${local.env_prefix}-asg-vol"
      Environment = local.environment_label
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ==================
# Auto Scaling Group
# ==================
resource "aws_autoscaling_group" "main" {
  name                      = "${local.env_prefix}-asg"
  vpc_zone_identifier       = [aws_subnet.private1.id, aws_subnet.private2.id]
  target_group_arns         = [aws_lb_target_group.autoscaling.arn]
  health_check_type         = "ELB"
  health_check_grace_period = 900
  default_cooldown          = 300

  min_size         = var.asg_min_size
  max_size         = var.asg_max_size
  desired_capacity = var.asg_desired_capacity

  launch_template {
    id      = aws_launch_template.ecs.id
    version = "$Latest"
  }

  force_delete              = true
  termination_policies      = ["OldestInstance"]
  wait_for_capacity_timeout = "0"

  # Enable instance scale-in protection
  protect_from_scale_in = false

  # Enable detailed monitoring
  enabled_metrics = [
    "GroupMinSize",
    "GroupMaxSize",
    "GroupDesiredCapacity",
    "GroupInServiceInstances",
    "GroupTotalInstances",
    "GroupPendingInstances",
    "GroupStandbyInstances",
    "GroupTerminatingInstances"
  ]

  tag {
    key                 = "Name"
    value               = "${local.env_prefix}-asg-instance"
    propagate_at_launch = true
  }

  tag {
    key                 = "Service"
    value               = "autoscaling"
    propagate_at_launch = true
  }

  tag {
    key                 = "Type"
    value               = "ASG-ECS"
    propagate_at_launch = true
  }

  tag {
    key                 = "LaunchTemplateVersion"
    value               = aws_launch_template.ecs.latest_version
    propagate_at_launch = true
  }

  tag {
    key                 = "Environment"
    value               = local.environment_label
    propagate_at_launch = true
  }

  tag {
    key                 = "ManagedBy"
    value               = "terraform"
    propagate_at_launch = true
  }

  tag {
    key                 = "Project"
    value               = "optinist-cloud"
    propagate_at_launch = true
  }

  instance_refresh {
    strategy = "Rolling"
    preferences {
      instance_warmup        = 300
      min_healthy_percentage = 50
    }
  }

  lifecycle {
    ignore_changes = [desired_capacity]
  }

  timeouts {
    delete = "30m"
  }

  # Lifecycle hooks for logging
  initial_lifecycle_hook {
    name                 = "${local.env_prefix}-launch-hook"
    default_result       = "CONTINUE"
    heartbeat_timeout    = 300
    lifecycle_transition = "autoscaling:EC2_INSTANCE_LAUNCHING"
  }

  initial_lifecycle_hook {
    name                 = "${local.env_prefix}-terminate-hook"
    default_result       = "CONTINUE"
    heartbeat_timeout    = 300
    lifecycle_transition = "autoscaling:EC2_INSTANCE_TERMINATING"
  }
}

# Auto Scaling Policies
resource "aws_autoscaling_policy" "scale_up" {
  name                   = "${local.env_prefix}-scale-up"
  scaling_adjustment     = 1
  adjustment_type        = "ChangeInCapacity"
  cooldown               = 300
  autoscaling_group_name = aws_autoscaling_group.main.name
}

resource "aws_autoscaling_policy" "scale_down" {
  name                   = "${local.env_prefix}-scale-down"
  scaling_adjustment     = -1
  adjustment_type        = "ChangeInCapacity"
  cooldown               = 300
  autoscaling_group_name = aws_autoscaling_group.main.name
}

# =============
# PREMIUM TIER
# ============

# Premium ECS Service for pre-warmed containers
resource "aws_ecs_service" "premium" {
  name                               = "${var.environment}-premium-optinist-cloud-service"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.premium.arn
  desired_count                      = 1
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 0
  launch_type                        = "EC2"

  enable_execute_command = true

  # Target premium spot fleet instances only
  placement_constraints {
    type       = "memberOf"
    expression = "attribute:tier == premium"
  }


  depends_on = [
    aws_instance.premium
  ]
}

# Premium Launch Template - Optimized for dedicated premium users
resource "aws_launch_template" "premium" {
  name_prefix   = "${local.env_prefix}-premium-"
  image_id      = local.effective_ami_id
  instance_type = var.premium_instance_type
  key_name      = aws_key_pair.subscr_optinist_cloud_key_pair.key_name

  vpc_security_group_ids = [aws_security_group.ecs.id]

  iam_instance_profile {
    name = aws_iam_instance_profile.ecs_instance_profile.name
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 48 # Reduced from 80: 32 GB swap moved to dedicated volume
      volume_type = "gp3"
      encrypted   = true
    }
  }

  # Dedicated swap volume — mkswap on a block device takes <1 second
  # vs ~4.5 minutes for dd-based swap file creation on root volume
  block_device_mappings {
    device_name = "/dev/xvds"
    ebs {
      volume_size           = 32
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  monitoring {
    enabled = true
  }

  user_data = base64encode(templatefile("${path.module}/../scripts/ecs-user-data.sh", {
    tier               = "premium"
    cluster_name       = aws_ecs_cluster.main.name
    git_branch         = var.git_branch
    git_repo           = var.git_repo
    ecr_registry       = split("/", local.ecr_repository_url)[0]
    ecr_repository_url = local.ecr_repository_url
    efs_id             = aws_efs_file_system.snmk.id
    db_host            = replace(aws_db_instance.main.endpoint, ":3306", "")
    swap_size_mb       = 32768 # 32GB swap for workflow memory spikes
    swap_device_name   = "/dev/xvds"
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name        = "${local.env_prefix}-premium-instance"
      Type        = "ECS-Premium"
      Tier        = "premium"
      Service     = "premium-spot-fleet"
      Environment = local.environment_label
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      Name        = "${local.env_prefix}-premium-vol"
      Environment = local.environment_label
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ===========
# ECS Cluster
# ===========
resource "aws_ecs_cluster" "main" {
  name = "${local.env_prefix}-cloud-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  service_connect_defaults {
    namespace = aws_service_discovery_private_dns_namespace.main.arn
  }

  tags = {
    Name = "${local.env_prefix}-cloud-cluster"
    # Terraform apply provenance (see deploy_info.tf). Traces the running
    # deployment back to the infrastructure/ git revision it was applied from.
    TfGitCommit = data.external.tf_build_info.result.git_commit
    TfGitBranch = data.external.tf_build_info.result.git_branch
  }
}

# Service Discovery
resource "aws_service_discovery_private_dns_namespace" "main" {
  name = "${var.environment}.optinist.local"
  vpc  = aws_vpc.main.id
}

# ECS Capacity Provider
resource "aws_ecs_capacity_provider" "main" {
  name = "${local.env_prefix}-capacity-provider"

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.main.arn
    managed_termination_protection = "DISABLED"

    managed_scaling {
      maximum_scaling_step_size = 1
      minimum_scaling_step_size = 1
      status                    = "DISABLED"
      target_capacity           = 90
      instance_warmup_period    = 300
    }
  }

  depends_on = [
    aws_autoscaling_group.main,
    aws_launch_template.ecs
  ]

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "${local.env_prefix}-capacity-provider"
  }
}

# ECS Cluster Capacity Providers
resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name = aws_ecs_cluster.main.name

  capacity_providers = [aws_ecs_capacity_provider.main.name]

  default_capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.main.name
    weight            = 1
    base              = 0
  }

  depends_on = [
    aws_ecs_capacity_provider.main,
    aws_autoscaling_group.main,
    aws_ecs_cluster.main,
    aws_launch_template.ecs
  ]

  lifecycle {
    create_before_destroy = false
    prevent_destroy       = false
    ignore_changes        = [capacity_providers]
  }
}

resource "aws_instance" "premium" {
  count = 1 # Start with 1 premium instance as base capacity

  launch_template {
    id      = aws_launch_template.premium.id
    version = "$Latest"
  }

  instance_type = var.premium_instance_type
  subnet_id     = aws_subnet.private1.id

  # On shutdown, stop instance instead of terminating
  instance_initiated_shutdown_behavior = "stop"

  # Prevent accidental termination
  disable_api_termination = false

  tags = {
    # Pre-provisioned at deploy time. Distinct from "premium-dedicated"
    # instances which are dynamically created per user sign-in.
    # See PremiumInstanceConfig.INSTANCE_NAME_SUFFIX in aws_constants.py.
    Name          = "${var.environment}-premium-initial"
    Type          = "Premium-Instance"
    Service       = "premium-tier"
    Tier          = "premium"
    InstanceIndex = count.index + 1
  }

  # Stop instance on creation to reduce costs when not in use
  # Lambda will start instances when users request premium access
  provisioner "local-exec" {
    command = "aws ec2 stop-instances --instance-ids ${self.id} --region ${var.aws_region} || true"
  }


  lifecycle {
    create_before_destroy = true
  }
}

# ===================
# ECS Task Definition
# ===================
resource "aws_ecs_task_definition" "autoscaling" {
  family                   = "${local.env_prefix}-cloud-taskdef"
  requires_compatibilities = ["EC2"]
  network_mode             = "bridge"
  cpu                      = 2048
  memory                   = 7168
  task_role_arn            = aws_iam_role.ecs_task.arn
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([
    {
      name              = "${local.env_prefix}-cloud-container"
      image             = "${local.ecr_repository_url}:latest"
      cpu               = 1536
      memory            = 6656
      memoryReservation = 4096
      essential         = true
      workingDirectory  = "/app"
      entryPoint        = ["/bin/sh", "-c"]
      command           = ["./cloud-startup.sh"]

      linuxParameters = {
        maxSwap    = 32768 # Max swap in MiB (matches 32GB host swap on EBS)
        swappiness = 20    # Only swap under memory pressure (host also set to 20)
      }

      portMappings = [
        {
          name          = "${local.env_prefix}-cloud-container-port-8000"
          containerPort = 8000
          hostPort      = 8000
          protocol      = "tcp"
        }
      ]

      # Many of these vars are duplicated in public_service.tf and background_service.tf;
      # a shared value must be changed in all three task definitions.
      environment = [
        {
          name  = "ENV_PREFIX"
          value = var.environment
        },
        {
          name  = "AWS_DEFAULT_REGION"
          value = var.aws_region
        },
        {
          name  = "CLOUDWATCH_LOG_GROUP"
          value = "/ecs/${local.env_prefix}-cloud-taskdef"
        },
        {
          name  = "PYTHONPATH"
          value = "/app/"
        },
        {
          name  = "TZ"
          value = "Asia/Tokyo"
        },
        {
          name  = "DB_HOST"
          value = aws_db_proxy.main.endpoint
        },
        {
          name  = "DB_PORT"
          value = "3306"
        },
        {
          name  = "DB_USER"
          value = var.mysql_user
        },
        {
          name  = "DB_NAME"
          value = var.mysql_database
        },
        {
          name  = "DB_PASSWORD"
          value = var.mysql_password
        },
        {
          name  = "MYSQL_SSL_MODE"
          value = "REQUIRED"
        },
        {
          name  = "BACKEND_HOST"
          value = "0.0.0.0"
        },
        {
          name  = "BACKEND_PORT"
          value = "8000"
        },
        {
          name  = "FRONTEND_SERVER_HOST"
          value = local.effective_frontend_domain
        },
        {
          name  = "FRONTEND_SERVER_PORT"
          value = local.effective_frontend_port
        },
        {
          name  = "FRONTEND_SERVER_PROTO"
          value = var.frontend_protocol
        },
        {
          name  = "INITIAL_FIREBASE_UID"
          value = var.optinist_admin_uid
        },
        {
          name  = "INITIAL_USER_NAME"
          value = var.optinist_admin_name
        },
        {
          name  = "INITIAL_USER_EMAIL"
          value = var.optinist_admin_email
        },
        {
          name  = "ADMIN_STORAGE_QUOTA_BYTES"
          value = "107374182400"
        },
        {
          name  = "SECRET_KEY"
          value = var.optinist_secret_key
        },
        {
          name  = "S3_DEFAULT_BUCKET_NAME"
          value = aws_s3_bucket.app_storage.id
        },
        {
          name  = "S3_USER_BUCKET_PREFIX"
          value = var.s3_user_bucket_prefix
        },
        {
          name  = "S3_USER_BUCKET_SECRET"
          value = var.s3_user_bucket_secret
        },
        {
          name  = "REMOTE_STORAGE_TYPE"
          value = "2"
        },
        {
          name  = "LOG_LEVEL"
          value = "INFO"
        },
        {
          name  = "UVICORN_ACCESS_LOG"
          value = "1"
        },
        {
          name  = "CORS_ORIGINS"
          value = "*"
        },
        {
          name  = "PYTHONUNBUFFERED"
          value = "1"
        },
        {
          name  = "OPTINIST_DIR"
          value = "/app/studio_data"
        },
        {
          name  = "TEST_USERS_CONFIG"
          value = jsonencode(var.test_users)
        },
        {
          name  = "SUBSCRIPTION_PLANS_CONFIG"
          value = jsonencode(var.subscription_plans)
        },
        {
          name  = "STRIPE_CALLBACK_URL"
          value = "${var.frontend_protocol}://${local.effective_frontend_domain}"
        },
        {
          name  = "STRIPE_SECRET_KEY"
          value = var.stripe_secret_key
        },
        {
          name  = "STRIPE_WEBHOOK_SECRET"
          value = var.stripe_webhook_secret
        },
        {
          name  = "ROUTING_SECRET_KEY"
          value = var.routing_secret_key
        },
        {
          name  = "SKIP_STORAGE_CHECKS"
          value = "false"
        },
        {
          name  = "INTERNAL_API_SECRET"
          value = random_password.internal_api_secret.result
        },
        # Disable scheduler - background jobs run in dedicated background service
        {
          name  = "DISABLE_BACKGROUND_SCHEDULER"
          value = "1"
        },
        # Enable standalone cleanup worker on free-tier instances.
        # cloud-startup.sh starts studio/cleanup_worker.py when this is set.
        {
          name  = "ENABLE_LOCAL_CLEANUP"
          value = "1"
        },
        {
          name  = "PREMIUM_MANAGER_FUNCTION_NAME"
          value = "${var.environment}-premium-manager"
        },
      ]
      mountPoints = [
        {
          sourceVolume  = "${local.env_prefix}-cloud-snmk-volume"
          containerPath = "/app/.snakemake"
          readOnly      = false
        }
      ]

      healthCheck = {
        command     = ["CMD-SHELL", "curl -v http://127.0.0.1:8000/health"]
        interval    = 300
        timeout     = 5
        retries     = 3
        startPeriod = 300
      }

      dockerLabels = {
        "health.check.enabled" = "true"
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"             = "/ecs/${local.env_prefix}-cloud-taskdef"
          "awslogs-multiline-pattern" = "^\\d{4}-\\d{2}-\\d{2}\\s\\d{2}:\\d{2}:\\d{2}"
          "max-buffer-size"           = "25m"
          "awslogs-region"            = var.aws_region
          "awslogs-create-group"      = "true"
          "awslogs-stream-prefix"     = "ecs"
          "mode"                      = "non-blocking"
        }
      }
    }
  ])

  volume {
    name = "${local.env_prefix}-cloud-snmk-volume"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.snmk.id
      root_directory     = "/"
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.snmk.id
        iam             = "DISABLED"
      }
    }
  }

  tags = {
    Name = "${local.env_prefix}-cloud-taskdef"
  }
}


# Premium ECS Task Definition - Pre-warmed containers for instant access
resource "aws_ecs_task_definition" "premium" {
  family                   = "${var.environment}-premium-optinist-cloud-taskdef"
  requires_compatibilities = ["EC2"]
  network_mode             = "bridge"
  cpu                      = 2048
  memory                   = 7168
  task_role_arn            = aws_iam_role.ecs_task.arn
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([
    {
      name              = "${var.environment}-premium-optinist-cloud-container"
      image             = "${local.ecr_repository_url}:latest"
      cpu               = 1536
      memory            = 6656
      memoryReservation = 4096
      essential         = true
      workingDirectory  = "/app"
      entryPoint        = ["/bin/sh", "-c"]
      command           = ["./cloud-startup.sh"]

      linuxParameters = {
        maxSwap    = 32768 # Max swap in MiB (matches 32GB host swap on EBS)
        swappiness = 20    # Only swap under memory pressure (host also set to 20)
      }

      portMappings = [
        {
          name          = "${var.environment}-premium-optinist-cloud-container-port-${var.premium_backend_port}"
          containerPort = var.premium_backend_port
          hostPort      = var.premium_backend_port
          protocol      = "tcp"
        }
      ]

      # Many of these vars are duplicated in public_service.tf and background_service.tf;
      # a shared value must be changed in all three task definitions.
      environment = [
        {
          name  = "ENV_PREFIX"
          value = var.environment
        },
        {
          name  = "AWS_DEFAULT_REGION"
          value = var.aws_region
        },
        {
          name  = "CLOUDWATCH_LOG_GROUP"
          value = "/ecs/${var.environment}-premium-optinist-cloud-taskdef"
        },
        {
          name  = "PYTHONPATH"
          value = "/app/"
        },
        {
          name  = "USER_TIER"
          value = "premium"
        },
        {
          name  = "TZ"
          value = "Asia/Tokyo"
        },
        {
          name  = "DB_HOST"
          value = aws_db_proxy.main.endpoint
        },
        {
          name  = "DB_PORT"
          value = "3306"
        },
        {
          name  = "DB_USER"
          value = var.mysql_user
        },
        {
          name  = "DB_NAME"
          value = var.mysql_database
        },
        {
          name  = "DB_PASSWORD"
          value = var.mysql_password
        },
        {
          name  = "MYSQL_SSL_MODE"
          value = "REQUIRED"
        },
        {
          name  = "BACKEND_HOST"
          value = "0.0.0.0"
        },
        {
          name  = "BACKEND_PORT"
          value = tostring(var.premium_backend_port)
        },
        {
          name  = "FRONTEND_SERVER_HOST"
          value = local.effective_frontend_domain
        },
        {
          name  = "FRONTEND_SERVER_PORT"
          value = local.effective_frontend_port
        },
        {
          name  = "FRONTEND_SERVER_PROTO"
          value = var.frontend_protocol
        },
        {
          name  = "INITIAL_FIREBASE_UID"
          value = var.optinist_admin_uid
        },
        {
          name  = "INITIAL_USER_NAME"
          value = var.optinist_admin_name
        },
        {
          name  = "INITIAL_USER_EMAIL"
          value = var.optinist_admin_email
        },
        {
          name  = "ADMIN_STORAGE_QUOTA_BYTES"
          value = "107374182400"
        },
        {
          name  = "SECRET_KEY"
          value = var.optinist_secret_key
        },
        {
          name  = "S3_DEFAULT_BUCKET_NAME"
          value = aws_s3_bucket.app_storage.id
        },
        {
          name  = "S3_USER_BUCKET_PREFIX"
          value = var.s3_user_bucket_prefix
        },
        {
          name  = "S3_USER_BUCKET_SECRET"
          value = var.s3_user_bucket_secret
        },
        {
          name  = "REMOTE_STORAGE_TYPE"
          value = "2"
        },
        {
          name  = "LOG_LEVEL"
          value = "INFO"
        },
        {
          name  = "UVICORN_ACCESS_LOG"
          value = "1"
        },
        {
          name  = "CORS_ORIGINS"
          value = "*"
        },
        {
          name  = "PYTHONUNBUFFERED"
          value = "1"
        },
        {
          name  = "OPTINIST_DIR"
          value = "/app/studio_data"
        },
        {
          name  = "SUBSCRIPTION_PLANS_CONFIG"
          value = jsonencode(var.subscription_plans)
        },
        {
          name  = "STRIPE_CALLBACK_URL"
          value = "${var.frontend_protocol}://${local.effective_frontend_domain}"
        },
        {
          name  = "STRIPE_SECRET_KEY"
          value = var.stripe_secret_key
        },
        {
          name  = "STRIPE_WEBHOOK_SECRET"
          value = var.stripe_webhook_secret
        },
        {
          name  = "ROUTING_SECRET_KEY"
          value = var.routing_secret_key
        },
        {
          name  = "SKIP_STORAGE_CHECKS"
          value = "false"
        },
        {
          name  = "INTERNAL_API_SECRET"
          value = random_password.internal_api_secret.result
        },
        # Disable scheduler - background jobs run in dedicated background service
        {
          name  = "DISABLE_BACKGROUND_SCHEDULER"
          value = "1"
        },
      ]

      mountPoints = [
        {
          sourceVolume  = "${var.environment}-premium-optinist-cloud-snmk-volume"
          containerPath = "/app/.snakemake"
          readOnly      = false
        }
      ]

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:${var.premium_backend_port}/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"             = "/ecs/${var.environment}-premium-optinist-cloud-taskdef"
          "awslogs-multiline-pattern" = "^\\d{4}-\\d{2}-\\d{2}\\s\\d{2}:\\d{2}:\\d{2}"
          "max-buffer-size"           = "25m"
          "awslogs-region"            = var.aws_region
          "awslogs-create-group"      = "true"
          "awslogs-stream-prefix"     = "ecs"
          "mode"                      = "non-blocking"
        }
      }
    }
  ])

  volume {
    name = "${var.environment}-premium-optinist-cloud-snmk-volume"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.snmk.id
      root_directory     = "/"
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.snmk.id
        iam             = "DISABLED"
      }
    }
  }

  tags = {
    Name = "${var.environment}-premium-optinist-cloud-taskdef"
    Tier = "premium"
  }
}

# ===========
# ECS Service
# ===========
resource "aws_ecs_service" "autoscaling" {
  name                               = "${local.env_prefix}-cloud-service"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.autoscaling.arn
  desired_count                      = 1
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 0

  capacity_provider_strategy {
    capacity_provider = aws_ecs_capacity_provider.main.name
    weight            = 1
    base              = 0
  }

  enable_execute_command = true

  load_balancer {
    target_group_arn = aws_lb_target_group.autoscaling.arn
    container_name   = "${local.env_prefix}-cloud-container"
    container_port   = 8000
  }

  depends_on = [
    aws_autoscaling_group.main,
    aws_db_instance.main,
    aws_lb.autoscaling,
    aws_lb_listener.autoscaling
  ]

  placement_constraints {
    type = "distinctInstance" # Force different instances
  }

  health_check_grace_period_seconds = 900

  tags = {
    Name = "${local.env_prefix}-cloud-service"
  }
}

# ===========================
# ECS Service Auto Scaling
# ===========================
# DISABLED: Scaling is managed by free_manager Lambda to handle slow startup times
# and user-count based scaling logic. ECS Application Auto Scaling conflicts with
# Lambda-driven scaling and causes race conditions.
#
# resource "aws_appautoscaling_target" "autoscaling_ecs" {
#   max_capacity       = 3
#   min_capacity       = 1
#   resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.autoscaling.name}"
#   scalable_dimension = "ecs:service:DesiredCount"
#   service_namespace  = "ecs"
#
#   depends_on = [aws_ecs_service.autoscaling]
# }
#
# # CPU-based scaling policy
# resource "aws_appautoscaling_policy" "autoscaling_ecs_cpu" {
#   name               = "subscr-optinist-ecs-cpu-scaling"
#   policy_type        = "TargetTrackingScaling"
#   resource_id        = aws_appautoscaling_target.autoscaling_ecs.resource_id
#   scalable_dimension = aws_appautoscaling_target.autoscaling_ecs.scalable_dimension
#   service_namespace  = aws_appautoscaling_target.autoscaling_ecs.service_namespace
#
#   target_tracking_scaling_policy_configuration {
#     predefined_metric_specification {
#       predefined_metric_type = "ECSServiceAverageCPUUtilization"
#     }
#     target_value       = 60.0
#     scale_in_cooldown  = 300
#     scale_out_cooldown = 60
#   }
# }
#
# # Memory-based scaling policy
# resource "aws_appautoscaling_policy" "autoscaling_ecs_memory" {
#   name               = "subscr-optinist-ecs-memory-scaling"
#   policy_type        = "TargetTrackingScaling"
#   resource_id        = aws_appautoscaling_target.autoscaling_ecs.resource_id
#   scalable_dimension = aws_appautoscaling_target.autoscaling_ecs.scalable_dimension
#   service_namespace  = aws_appautoscaling_target.autoscaling_ecs.service_namespace
#
#   target_tracking_scaling_policy_configuration {
#     predefined_metric_specification {
#       predefined_metric_type = "ECSServiceAverageMemoryUtilization"
#     }
#     target_value       = 80.0
#     scale_in_cooldown  = 300
#     scale_out_cooldown = 60
#   }
# }
