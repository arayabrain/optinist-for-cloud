#!/bin/bash
set -e
exec > /var/log/ecs-setup.log 2>&1

echo "$(date): Starting ECS setup with OptiNiSt configuration"

# ECS Configuration
echo ECS_CLUSTER=${cluster_name} >> /etc/ecs/ecs.config
echo ECS_ENABLE_CONTAINER_METADATA=true >> /etc/ecs/ecs.config
echo ECS_ENABLE_TASK_IAM_ROLE=true >> /etc/ecs/ecs.config
echo ECS_INSTANCE_ATTRIBUTES='{"tier":"${tier}"}' >> /etc/ecs/ecs.config

# Only premium standbys are reaped while stopped, so only they must wipe the stale checkpoint on boot to re-register.
if [ "${tier}" = "premium" ]; then
  cat > /etc/systemd/system/ecs-clear-checkpoint.service << 'UNIT_EOF'
[Unit]
Description=Clear stale ECS agent checkpoint before startup
Before=ecs.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=true
ExecStart=/bin/rm -f /var/lib/ecs/data/agent.db /var/lib/ecs/data/ecs_agent_data.json

[Install]
WantedBy=multi-user.target
UNIT_EOF

  systemctl daemon-reload
  systemctl enable ecs-clear-checkpoint.service
fi

# Install packages (skip if AMI is pre-baked by Image Builder)
if [ -f /etc/optinist-ami-baked ]; then
    echo "$(date): Pre-baked AMI detected, skipping package installation"
    cat /etc/optinist-ami-baked
    # AWS CLI v2 installed to /usr/local/bin by Image Builder
    export PATH="/usr/local/bin:$PATH"
else
    echo "$(date): Stock AMI detected, installing packages"
    yum update -y
    yum install -y amazon-ssm-agent mariadb105 amazon-efs-utils nc git docker amazon-cloudwatch-agent unzip
    # Install AWS CLI v2 (awscli v1 yum package no longer available)
    curl -sL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
    cd /tmp && unzip -qo awscliv2.zip
    /tmp/aws/install --update
    rm -rf /tmp/aws /tmp/awscliv2.zip
    echo "$(date): Package installation complete"
fi

# Setup swap as memory safety net (defense-in-depth for OOM prevention)
# This provides a buffer before OOM killer activates, giving workflows
# a chance to complete during temporary memory spikes
SWAP_SIZE_MB=${swap_size_mb}  # Configurable per instance type (0 to skip)
SWAP_DEVICE_NAME="${swap_device_name}"  # Dedicated swap EBS volume device (empty = use file-based swap)
SWAP_FILE=/swapfile

if [ "$SWAP_SIZE_MB" -gt 0 ]; then
    echo "$(date): Setting up swap space ($${SWAP_SIZE_MB}MB)"

    SWAP_TARGET=""

    # Strategy 1: Dedicated EBS swap volume (instant, preferred)
    if [ -n "$SWAP_DEVICE_NAME" ]; then
        # On Nitro instances (t3, m5, etc.), /dev/xvds may appear as /dev/nvme*n1.
        # The ECS-optimized AMI creates symlinks via udev rules (ec2-utils).
        if [ -b "$SWAP_DEVICE_NAME" ]; then
            SWAP_TARGET="$SWAP_DEVICE_NAME"
            echo "$(date): Found swap device at $SWAP_DEVICE_NAME"
        else
            echo "$(date): $SWAP_DEVICE_NAME not found, scanning NVMe devices..."
            for nvme_dev in /dev/nvme*n1; do
                [ -b "$nvme_dev" ] || continue
                # ebsnvme-id prints the attachment name without the /dev/ prefix
                mapped_name=$(/sbin/ebsnvme-id -b "$nvme_dev" 2>/dev/null || true)
                if [ "$mapped_name" = "$(basename "$SWAP_DEVICE_NAME")" ]; then
                    SWAP_TARGET="$nvme_dev"
                    echo "$(date): Found swap device at $nvme_dev (mapped from $SWAP_DEVICE_NAME)"
                    break
                fi
            done
        fi
    fi

    if [ -n "$SWAP_TARGET" ]; then
        # Block device swap (instant setup - mkswap writes only a header)
        mkswap "$SWAP_TARGET"
        swapon "$SWAP_TARGET"
        echo "$SWAP_TARGET swap swap defaults,nofail 0 0" >> /etc/fstab
        echo "$(date): Block device swap setup complete ($SWAP_TARGET)"
    elif [ ! -f "$SWAP_FILE" ]; then
        # Fallback: file-based swap (slow, for tiers without dedicated swap volume)
        # If a dedicated swap device was expected but not found, use 1/4 size
        # to avoid filling the reduced root volume.
        FALLBACK_SIZE_MB=$SWAP_SIZE_MB
        if [ -n "$SWAP_DEVICE_NAME" ]; then
            FALLBACK_SIZE_MB=$((SWAP_SIZE_MB / 4))
            echo "$(date): [CRITICAL] Swap device $SWAP_DEVICE_NAME not found — expected dedicated EBS volume is missing"
            echo "$(date): [CRITICAL] Falling back to $${FALLBACK_SIZE_MB}MB file swap (1/4 of $${SWAP_SIZE_MB}MB) on root volume"
        else
            echo "$(date): Using file-based swap ($${FALLBACK_SIZE_MB}MB)"
        fi
        dd if=/dev/zero of=$SWAP_FILE bs=1M count=$FALLBACK_SIZE_MB status=progress
        chmod 600 $SWAP_FILE
        mkswap $SWAP_FILE
        swapon $SWAP_FILE
        echo "$SWAP_FILE swap swap defaults 0 0" >> /etc/fstab
        echo "$(date): File-based swap setup complete ($${FALLBACK_SIZE_MB}MB)"
    else
        echo "$(date): Swap file already exists"
        swapon $SWAP_FILE 2>/dev/null || true
    fi

    # Set low swappiness - only use swap under real memory pressure
    if ! grep -q 'vm.swappiness' /etc/sysctl.conf; then
        echo "vm.swappiness=20" >> /etc/sysctl.conf
    fi
    sysctl vm.swappiness=20
    echo "$(date): Swap setup complete ($${SWAP_SIZE_MB}MB, swappiness=20)"
else
    echo "$(date): Skipping swap setup (swap_size_mb=0)"
fi

# Start SSM agent
if ! systemctl is-active --quiet amazon-ssm-agent; then
    systemctl enable amazon-ssm-agent
    systemctl start amazon-ssm-agent
fi

# Create CloudWatch agent config
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json << 'CW_CONFIG'
{
    "metrics": {
        "namespace": "CWAgent",
        "append_dimensions": {
            "AutoScalingGroupName": "$${aws:AutoScalingGroupName}"
        },
        "aggregation_dimensions": [
            ["AutoScalingGroupName"]
        ],
        "metrics_collected": {
            "mem": {
                "measurement": [
                    "mem_used_percent"
                ]
            },
            "cpu": {
                "measurement": [
                    "cpu_usage_idle",
                    "cpu_usage_iowait"
                ],
                "totalcpu": true
            },
            "diskio": {
                "measurement": [
                    "iops_in_progress",
                    "io_time"
                ]
            }
        }
    },
    "logs": {
        "logs_collected": {
            "files": {
                "collect_list": [
                    {
                        "file_path": "/proc/loadavg",
                        "log_group_name": "/aws/ec2/loadavg",
                        "log_stream_name": "{instance_id}",
                        "timezone": "UTC"
                    }
                ]
            }
        }
    }
}
CW_CONFIG

# Start CloudWatch agent
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -s \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json

# Start Docker (using same safe approach)
if ! systemctl is-active --quiet docker; then
    systemctl enable docker || echo "$(date): Docker enable failed"
    systemctl start docker || echo "$(date): Docker start failed"
fi
for user in ec2-user ssm-user; do
  if id "$user" &>/dev/null; then
      usermod -a -G docker "$user" && echo "$(date): Added $user to docker group"
      break
  fi
done

# Clone and build OptiNiSt
echo "$(date): Cloning OptiNiSt repository"
cd /opt
    git clone -b ${git_branch} ${git_repo} optinist-for-cloud || {
    echo "$(date): ERROR: Git clone failed!"
    exit 1
}
if [ ! -d "optinist-for-cloud" ]; then
    echo "$(date): ERROR: Repository directory not created"
    exit 1
fi
cd optinist-for-cloud

# Add AWS Batch plugins to Dockerfile
echo "$(date): Adding AWS Batch plugins to Dockerfile"
# Build the Docker image
echo "$(date): Building OptiNiSt Docker image"
if [ ! -f "studio/config/docker/Dockerfile" ]; then
    echo "ERROR: Dockerfile not found in repository"
    ls -la
    exit 1
fi

# ECR login and pull pre-built image
echo "$(date): Logging into ECR and pulling pre-built image"
aws ecr get-login-password --region ap-northeast-1 | docker login --username AWS --password-stdin ${ecr_registry}
echo "$(date): Pulling OptiNiSt Docker image from ECR"
docker pull "${ecr_repository_url}:latest" || {
    echo "ERROR: Docker pull failed!"
    exit 1
}

# Optional EFS mount; skipped when efs_id is empty.
if [ -n "${efs_id}" ]; then
    mkdir -p /mnt/efs
    echo "${efs_id}.efs.ap-northeast-1.amazonaws.com:/ /mnt/efs efs tls,_netdev" >> /etc/fstab
    mount -a || echo "EFS will retry"
fi

# Test DB connection (non-blocking)
nc -z ${db_host} 3306 && echo "DB accessible" || echo "DB will be available"
