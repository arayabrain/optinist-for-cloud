# Dedicated ECS tier for unauthenticated traffic; runs the same image with
# INSTANCE_MODE=public so the heavy router/wrapper set is skipped.

resource "aws_lb_target_group" "public" {
  name        = "${local.env_prefix}-public-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "instance"

  # Match the listener idle_timeout so streaming responses survive deregistration.
  deregistration_delay = 600

  health_check {
    enabled             = true
    protocol            = "HTTP"
    path                = "/health"
    matcher             = "200"
    port                = "traffic-port"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  lifecycle {
    create_before_destroy = true
  }

  tags = {
    Name = "${local.env_prefix}-public-target-group"
    Tier = "public"
  }
}

resource "aws_launch_template" "public" {
  name_prefix   = "${local.env_prefix}-public-"
  image_id      = local.effective_ami_id
  instance_type = var.public_instance_type
  key_name      = aws_key_pair.subscr_optinist_cloud_key_pair.key_name

  vpc_security_group_ids = [aws_security_group.ecs.id]

  iam_instance_profile {
    name = aws_iam_instance_profile.ecs_instance_profile.name
  }

  # Root volume sized for OS + image only; experiment cache lives on EFS.
  # 30 GB matches the AMI's snapshot baseline (smaller fails CreateAutoScalingGroup);
  # further shrink would require rebaking a thinner AMI.
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 30
      volume_type = "gp3"
      encrypted   = true
    }
  }

  monitoring {
    enabled = true
  }

  user_data = base64encode(templatefile("${path.module}/../scripts/ecs-user-data.sh", {
    tier               = "public"
    cluster_name       = aws_ecs_cluster.main.name
    git_branch         = var.git_branch
    git_repo           = var.git_repo
    ecr_registry       = split("/", local.ecr_repository_url)[0]
    ecr_repository_url = local.ecr_repository_url
    efs_id             = ""
    db_host            = replace(aws_db_instance.main.endpoint, ":3306", "")
    swap_size_mb       = 0
    swap_device_name   = ""
  }))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name        = "${local.env_prefix}-public-instance"
      Type        = "ECS-Public"
      Tier        = "public"
      Service     = "public-spa"
      Environment = local.environment_label
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      Name        = "${local.env_prefix}-public-vol"
      Environment = local.environment_label
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "public" {
  name                = "${local.env_prefix}-public-asg"
  vpc_zone_identifier = [aws_subnet.private1.id, aws_subnet.private2.id]
  target_group_arns   = [aws_lb_target_group.public.arn]
  health_check_type   = "ELB"
  # Cold boot (ECR pull + container start + ASGI lifespan + startup-sync S3
  # warm) routinely takes >5 min; 300s tripped premature unhealthy replacements.
  health_check_grace_period = 900
  default_cooldown          = 300

  min_size         = var.public_asg_min_size
  max_size         = var.public_asg_max_size
  desired_capacity = var.public_asg_desired_capacity

  launch_template {
    id      = aws_launch_template.public.id
    version = "$Latest"
  }

  # force_delete=false so ASG-level removals respect the TG deregistration_delay
  # (in-flight streaming responses get the 600s drain we promise above).
  force_delete              = false
  termination_policies      = ["OldestInstance"]
  wait_for_capacity_timeout = "0"

  tag {
    key                 = "Name"
    value               = "${local.env_prefix}-public-asg-instance"
    propagate_at_launch = true
  }

  tag {
    key                 = "Tier"
    value               = "public"
    propagate_at_launch = true
  }

  lifecycle {
    create_before_destroy = true
    ignore_changes        = [desired_capacity]
  }
}

resource "aws_ecs_task_definition" "public" {
  family                   = "${var.environment}-public-optinist-cloud-taskdef"
  requires_compatibilities = ["EC2"]
  network_mode             = "bridge"
  # Reserves ~750 MiB for ECS/CW/SSM agents and kernel on a 2 GiB host.
  cpu                = 1024
  memory             = 1280
  task_role_arn      = aws_iam_role.ecs_task.arn
  execution_role_arn = aws_iam_role.ecs_task_execution.arn

  container_definitions = jsonencode([
    {
      # Name must match aws_ecs_service.public.load_balancer.container_name.
      name              = "${local.env_prefix}-cloud-container"
      image             = "${local.ecr_repository_url}:latest"
      cpu               = 1024
      memory            = 1280
      memoryReservation = 1024
      essential         = true
      workingDirectory  = "/app"
      entryPoint        = ["/bin/sh", "-c"]
      command           = ["./cloud-startup.sh"]

      portMappings = [
        {
          name          = "${local.env_prefix}-public-container-port-8000"
          containerPort = 8000
          hostPort      = 8000
          protocol      = "tcp"
        }
      ]

      # Many of these vars are duplicated in compute.tf and background_service.tf;
      # a shared value must be changed in all three task definitions.
      environment = [
        { name = "ENV_PREFIX", value = var.environment },
        { name = "AWS_DEFAULT_REGION", value = var.aws_region },
        { name = "CLOUDWATCH_LOG_GROUP", value = aws_cloudwatch_log_group.public_optinist.name },
        { name = "PYTHONPATH", value = "/app/" },
        { name = "TZ", value = "Asia/Tokyo" },

        { name = "INSTANCE_MODE", value = "public" },

        # Blast-radius cap for the internet-facing tier on the shared RDS Proxy.
        { name = "SQLALCHEMY_POOL_SIZE", value = "8" },
        { name = "SQLALCHEMY_MAX_OVERFLOW", value = "8" },

        { name = "UVICORN_ACCESS_LOG", value = "1" },

        { name = "DISABLE_BACKGROUND_SCHEDULER", value = "1" },

        { name = "DB_HOST", value = aws_db_proxy.main.endpoint },
        { name = "DB_PORT", value = "3306" },
        { name = "DB_USER", value = var.mysql_user },
        { name = "DB_NAME", value = var.mysql_database },
        { name = "DB_PASSWORD", value = var.mysql_password },
        { name = "MYSQL_SSL_MODE", value = "REQUIRED" },

        { name = "BACKEND_HOST", value = "0.0.0.0" },
        { name = "BACKEND_PORT", value = "8000" },

        { name = "FRONTEND_SERVER_HOST", value = local.effective_frontend_domain },
        { name = "FRONTEND_SERVER_PORT", value = local.effective_frontend_port },
        { name = "FRONTEND_SERVER_PROTO", value = var.frontend_protocol },

        { name = "INITIAL_FIREBASE_UID", value = var.optinist_admin_uid },
        { name = "INITIAL_USER_NAME", value = var.optinist_admin_name },
        { name = "INITIAL_USER_EMAIL", value = var.optinist_admin_email },
        { name = "ADMIN_STORAGE_QUOTA_BYTES", value = "107374182400" },
        { name = "SECRET_KEY", value = var.optinist_secret_key },

        { name = "S3_DEFAULT_BUCKET_NAME", value = aws_s3_bucket.app_storage.id },
        { name = "S3_USER_BUCKET_PREFIX", value = var.s3_user_bucket_prefix },
        { name = "REMOTE_STORAGE_TYPE", value = "2" },

        { name = "LOG_LEVEL", value = "INFO" },
        { name = "CORS_ORIGINS", value = "*" },
        { name = "PYTHONUNBUFFERED", value = "1" },
        { name = "OPTINIST_DIR", value = "/app/studio_data" },

        # Stripe vars omitted: subscriptions router is gated out on public.
        { name = "ROUTING_SECRET_KEY", value = var.routing_secret_key },

        { name = "INTERNAL_API_SECRET", value = random_password.internal_api_secret.result },
        { name = "UVICORN_WORKERS", value = "1" },
      ]


      mountPoints = [
        {
          sourceVolume  = "${local.env_prefix}-public-published-data-volume"
          containerPath = "/app/studio_data/output"
          readOnly      = false
        },
        {
          # Raw inputs are synced on demand; keep them on shared EFS (not the
          # lean root EBS) so both instances share one copy and it can be swept.
          sourceVolume  = "${local.env_prefix}-public-input-cache-volume"
          containerPath = "/app/studio_data/input"
          readOnly      = false
        }
      ]

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://127.0.0.1:8000/health || exit 1"]
        interval    = 60
        timeout     = 5
        retries     = 3
        startPeriod = 180
      }

      dockerLabels = {
        "health.check.enabled" = "true"
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"             = aws_cloudwatch_log_group.public_optinist.name
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
    name = "${local.env_prefix}-public-published-data-volume"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.published_data.id
      root_directory     = "/"
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.published_data.id
        iam             = "DISABLED"
      }
    }
  }

  volume {
    name = "${local.env_prefix}-public-input-cache-volume"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.published_data.id
      root_directory     = "/"
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.published_data_input.id
        iam             = "DISABLED"
      }
    }
  }

  tags = {
    Name = "${var.environment}-public-optinist-cloud-taskdef"
    Tier = "public"
  }
}

resource "aws_ecs_service" "public" {
  name            = "${var.environment}-public-optinist-cloud-service"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.public.arn
  desired_count   = var.public_asg_desired_capacity
  # min_healthy=50 keeps one serving. max 200 because AZ rebalancing rejects <= 100
  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = 50

  # Direct EC2 launch_type; the shared capacity provider is bound to the free ASG.
  launch_type = "EC2"

  enable_execute_command = true

  placement_constraints {
    type       = "memberOf"
    expression = "attribute:tier == public"
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.public.arn
    container_name   = "${local.env_prefix}-cloud-container"
    container_port   = 8000
  }

  depends_on = [
    aws_autoscaling_group.public,
    aws_db_instance.main,
  ]

  tags = {
    Name = "${var.environment}-public-optinist-cloud-service"
    Tier = "public"
  }
}

resource "aws_cloudwatch_log_group" "public_optinist" {
  name              = "/ecs/${var.environment}-public-optinist-cloud-taskdef"
  retention_in_days = 30

  tags = {
    Name = "Public Service Logs"
    Type = "Public-CloudWatch"
  }
}

output "public_service_name" {
  description = "Name of the public ECS service"
  value       = aws_ecs_service.public.name
}

output "public_task_definition_arn" {
  description = "ARN of the public task definition"
  value       = aws_ecs_task_definition.public.arn
}

output "public_target_group_arn" {
  description = "ARN of the public target group"
  value       = aws_lb_target_group.public.arn
}

# wait_for_capacity_timeout=0 means `terraform apply` returns before targets
# are healthy. Use this output to verify the public tier is serving traffic
# before relying on the listener default-action flip.
output "public_tg_health_check_command" {
  description = "Run this after `terraform apply` to confirm public targets are healthy"
  value       = "aws elbv2 describe-target-health --target-group-arn ${aws_lb_target_group.public.arn} --query 'TargetHealthDescriptions[*].[Target.Id,TargetHealth.State]' --output table"
}
