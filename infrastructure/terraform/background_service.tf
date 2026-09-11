# ===============================================
# Background Service ECS Infrastructure
# ===============================================
# Dedicated ECS service for running background jobs:
# - PublishedExperimentSyncJob (sync published experiments from S3)
# - DataCleanupJob (cleanup data for logged-out free users)
# - StorageReconciliationJob (reconcile storage tracking with S3)
#
# This replaces the file-based locking mechanism for multi-worker
# APScheduler coordination. API services disable their schedulers
# (DISABLE_BACKGROUND_SCHEDULER=1) while this dedicated service
# runs all background jobs.

# ===========================
# Background Service Launch Template
# ===========================
resource "aws_launch_template" "background" {
  name_prefix   = "${local.env_prefix}-background-"
  image_id      = local.effective_ami_id
  instance_type = var.background_instance_type

  vpc_security_group_ids = [aws_security_group.ecs.id]

  iam_instance_profile {
    name = aws_iam_instance_profile.ecs_instance_profile.name
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 30 # Minimum for ECS-optimized AMI snapshot
      volume_type = "gp3"
      encrypted   = true
    }
  }

  monitoring {
    enabled = true
  }

  user_data = base64encode(templatefile("${path.module}/../scripts/ecs-user-data.sh", {
    tier               = "background"
    cluster_name       = aws_ecs_cluster.main.name
    git_branch         = var.git_branch
    git_repo           = var.git_repo
    ecr_registry       = split("/", local.ecr_repository_url)[0]
    ecr_repository_url = local.ecr_repository_url
    efs_id             = aws_efs_file_system.snmk.id
    db_host            = replace(aws_db_instance.main.endpoint, ":3306", "")
    swap_size_mb       = 1536 # 2x task memory (768MB) for stable background job operation
    swap_device_name   = ""   # File-based swap (1.5GB dd is fast, no dedicated volume needed)
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name    = "${local.env_prefix}-background-instance"
      Type    = "ECS-Background"
      Tier    = "background"
      Service = "background-jobs"
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ===========================
# Background Service Instance
# ===========================
resource "aws_instance" "background" {
  launch_template {
    id      = aws_launch_template.background.id
    version = "$Latest"
  }

  instance_type = var.background_instance_type
  subnet_id     = aws_subnet.private1.id

  tags = {
    Name    = "${local.env_prefix}-background"
    Type    = "Background-Instance"
    Service = "background-jobs"
    Tier    = "background"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ===========================
# Background ECS Task Definition
# ===========================
resource "aws_ecs_task_definition" "background" {
  family                   = "${var.environment}-background-optinist-cloud-taskdef"
  requires_compatibilities = ["EC2"]
  network_mode             = "bridge"
  cpu                      = 512 # Lower CPU for background jobs
  memory                   = 768 # Fits on t3.micro with ECS overhead
  task_role_arn            = aws_iam_role.ecs_task.arn
  execution_role_arn       = aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([
    {
      name              = "${var.environment}-background-optinist-cloud-container"
      image             = "${local.ecr_repository_url}:latest"
      cpu               = 512
      memory            = 768
      memoryReservation = 512
      essential         = true
      workingDirectory  = "/app"
      entryPoint        = ["/bin/sh", "-c"]
      command           = ["./cloud-startup.sh"]

      # No port mappings - background service doesn't serve HTTP

      # Many of these vars are duplicated in compute.tf and public_service.tf;
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
          value = "/ecs/${var.environment}-background-optinist-cloud-taskdef"
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
          name  = "REMOTE_STORAGE_TYPE"
          value = "2"
        },
        {
          name  = "LOG_LEVEL"
          value = "INFO"
        },
        {
          name  = "UVICORN_ACCESS_LOG"
          value = "0" # Disable access log for background service
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
        {
          name  = "ALB_DNS_NAME"
          value = aws_lb.autoscaling.dns_name
        },
        # Base URL for the sync job's internal /system-internal call.
        # Scheme/port mirror aws_lb_listener.autoscaling_https: HTTPS:443 in
        # prod, HTTP:8080 in dev. enable_custom_domain gates both this and
        # compute.tf, so the flag can't drift them apart; the ":8080" literal
        # is kept to match compute.tf and must be changed there in lockstep.
        {
          name  = "INTERNAL_API_BASE_URL"
          value = var.enable_custom_domain ? "https://${aws_lb.autoscaling.dns_name}" : "http://${aws_lb.autoscaling.dns_name}:8080"
        },
        # Background scheduler ENABLED - this service runs all background jobs
        {
          name  = "DISABLE_BACKGROUND_SCHEDULER"
          value = "0"
        },
        # Single worker for background service
        {
          name  = "UVICORN_WORKERS"
          value = "1"
        },
        {
          name  = "PREMIUM_MANAGER_FUNCTION_NAME"
          value = "${var.environment}-premium-manager"
        },
      ]
      mountPoints = [
        {
          sourceVolume  = "${var.environment}-background-optinist-cloud-snmk-volume"
          containerPath = "/app/.snakemake"
          readOnly      = false
        }
      ]

      # Health check via container health (not ALB)
      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8000/health || exit 1"]
        interval    = 60
        timeout     = 10
        retries     = 3
        startPeriod = 120
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"             = "/ecs/${var.environment}-background-optinist-cloud-taskdef"
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
    name = "${var.environment}-background-optinist-cloud-snmk-volume"
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
    Name = "${var.environment}-background-optinist-cloud-taskdef"
    Tier = "background"
  }
}

# ===========================
# Background ECS Service
# ===========================
resource "aws_ecs_service" "background" {
  name                               = "${var.environment}-background-optinist-cloud-service"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.background.arn
  desired_count                      = 1 # Single instance for all background jobs
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 0
  launch_type                        = "EC2"

  enable_execute_command = true

  # Target background instance only
  placement_constraints {
    type       = "memberOf"
    expression = "attribute:tier == background"
  }

  # No load balancer - background service doesn't serve HTTP traffic

  depends_on = [
    aws_instance.background,
    aws_db_instance.main
  ]

  tags = {
    Name = "${var.environment}-background-optinist-cloud-service"
    Tier = "background"
  }
}

# ===========================
# CloudWatch Log Group
# ===========================
resource "aws_cloudwatch_log_group" "background_logs" {
  name              = "/ecs/${var.environment}-background-optinist-cloud-taskdef"
  retention_in_days = 14

  tags = {
    Name = "Background Service Logs"
    Type = "Background-CloudWatch"
  }
}

# ===========================
# CloudWatch Alarms
# ===========================

# Alarm for background task stopped (service count drops to 0)
resource "aws_cloudwatch_metric_alarm" "background_task_stopped" {
  alarm_name          = "${var.environment}-background-task-stopped"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "RunningTaskCount"
  namespace           = "ECS/ContainerInsights"
  period              = "300" # 5 minutes
  statistic           = "Average"
  threshold           = "1"
  alarm_description   = "Background service task count dropped below 1 - background jobs not running"
  alarm_actions       = local.critical_alerts_actions
  ok_actions          = local.critical_alerts_actions

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.background.name
  }

  tags = {
    Name = "Background Task Stopped Alarm"
    Type = "Background-CloudWatch"
  }
}

# Alarm for background service CPU utilization (warn if overloaded)
resource "aws_cloudwatch_metric_alarm" "background_cpu_high" {
  alarm_name          = "${var.environment}-background-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "3"
  metric_name         = "CpuUtilized"
  namespace           = "ECS/ContainerInsights"
  period              = "300" # 5 minutes
  statistic           = "Average"
  threshold           = "400" # 80% of 512 CPU units
  alarm_description   = "Background service CPU utilization is high - jobs may be delayed"
  alarm_actions       = local.critical_alerts_actions
  ok_actions          = local.critical_alerts_actions

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.background.name
  }

  tags = {
    Name = "Background CPU High Alarm"
    Type = "Background-CloudWatch"
  }
}

# Alarm for background service memory utilization
resource "aws_cloudwatch_metric_alarm" "background_memory_high" {
  alarm_name          = "${var.environment}-background-memory-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "3"
  metric_name         = "MemoryUtilized"
  namespace           = "ECS/ContainerInsights"
  period              = "300" # 5 minutes
  statistic           = "Average"
  threshold           = "600" # ~80% of 768 MB
  alarm_description   = "Background service memory utilization is high"
  alarm_actions       = local.critical_alerts_actions
  ok_actions          = local.critical_alerts_actions

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.background.name
  }

  tags = {
    Name = "Background Memory High Alarm"
    Type = "Background-CloudWatch"
  }
}

# ===========================
# Outputs
# ===========================
output "background_service_name" {
  description = "Name of the background ECS service"
  value       = aws_ecs_service.background.name
}

output "background_task_definition_arn" {
  description = "ARN of the background task definition"
  value       = aws_ecs_task_definition.background.arn
}

output "background_instance_id" {
  description = "ID of the background EC2 instance"
  value       = aws_instance.background.id
}
