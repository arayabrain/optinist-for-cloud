#!/bin/bash
set -e  # Exit immediately if a command exits with a non-zero status

# === AWS CREDENTIAL DIAGNOSTIC (remove after investigation of #612) ===
echo "=== AWS CREDENTIAL DIAGNOSTIC ==="
echo "AWS_ACCESS_KEY_ID set: $([ -n "$AWS_ACCESS_KEY_ID" ] && echo "YES (${AWS_ACCESS_KEY_ID:0:4}...)" || echo "NO")"
echo "AWS_SECRET_ACCESS_KEY set: $([ -n "$AWS_SECRET_ACCESS_KEY" ] && echo "YES" || echo "NO")"
echo "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI: ${AWS_CONTAINER_CREDENTIALS_RELATIVE_URI:-NOT SET}"
echo "Calling sts get-caller-identity..."
aws sts get-caller-identity --region "${AWS_DEFAULT_REGION:-ap-northeast-1}" 2>&1 || echo "STS CALL FAILED"
echo "=== END DIAGNOSTIC ==="
# === END DIAGNOSTIC ===

# Map infrastructure environment variables (DB_*) to application expected variables (MYSQL_*)
# This allows the application's config.py to find the correct environment variables
# while maintaining infrastructure naming conventions
export MYSQL_SERVER="${DB_HOST}"
export MYSQL_USER="${DB_USER}"
export MYSQL_PASSWORD="${DB_PASSWORD}"
export MYSQL_DATABASE="${DB_NAME}"

# Fetch Firebase config from Secrets Manager (overrides files baked into Docker image)
if [ -n "$ENV_PREFIX" ]; then
    echo "Fetching Firebase config from Secrets Manager for environment: ${ENV_PREFIX}"
    FIREBASE_CONFIG_DIR="/app/studio/config/auth"
    mkdir -p "$FIREBASE_CONFIG_DIR"

    FIREBASE_CONFIG=$(aws secretsmanager get-secret-value \
        --secret-id "${ENV_PREFIX}-optinist/firebase/config" \
        --query "SecretString" --output text --region "${AWS_DEFAULT_REGION:-ap-northeast-1}" 2>/dev/null || echo "")
    if [ -n "$FIREBASE_CONFIG" ]; then
        echo "$FIREBASE_CONFIG" > "$FIREBASE_CONFIG_DIR/firebase_config.json"
        echo "Firebase config written from Secrets Manager"
    else
        echo "WARNING: Could not fetch Firebase config from Secrets Manager. Using defaults."
    fi

    FIREBASE_PRIVATE=$(aws secretsmanager get-secret-value \
        --secret-id "${ENV_PREFIX}-optinist/firebase/private-key" \
        --query "SecretString" --output text --region "${AWS_DEFAULT_REGION:-ap-northeast-1}" 2>/dev/null || echo "")
    if [ -n "$FIREBASE_PRIVATE" ]; then
        echo "$FIREBASE_PRIVATE" > "$FIREBASE_CONFIG_DIR/firebase_private.json"
        echo "Firebase private key written from Secrets Manager"
    else
        echo "WARNING: Could not fetch Firebase private key from Secrets Manager. Using defaults."
    fi
fi

echo 'Starting container'
echo 'Attempting to connect to RDS'
# Log environment variables for debugging
echo "DB_HOST: ${MYSQL_SERVER}"
echo "DB_USER: ${MYSQL_USER}"
echo "DB_NAME: ${MYSQL_DATABASE}"

# Wait for RDS to be available
# This is necessary because RDS might still be initializing when container starts
# (e.g., after dev scheduler starts the environment — RDS takes 5-10 min)
# Tries 60 times with 10 second intervals (total 10 minutes timeout)
max_tries=60
counter=0

# Build SSL options based on MYSQL_SSL_MODE
if [ -n "${MYSQL_SSL_MODE}" ]; then
    case "${MYSQL_SSL_MODE}" in
        DISABLED)
            ssl_opts="--skip-ssl"
            ;;
        *)
            ssl_opts="--ssl"
            ;;
    esac
else
    ssl_opts=""
fi

until mysql ${ssl_opts} -h "${MYSQL_SERVER}" -u "${MYSQL_USER}" -p"${MYSQL_PASSWORD}" "${MYSQL_DATABASE}" -e 'SELECT 1;'
do
    sleep 10
    [[ counter -eq $max_tries ]] && echo "Failed to connect to Database" && exit 1
    echo "Attempt $counter: Waiting for Database..."
    ((counter++))
done

echo 'Database connection successful'

# Run database migrations using alembic
# This ensures all database tables and schemas are up to date
cd /app

# Verify database SSL connection before running migrations
echo "Verifying database SSL connection..."
python3 -c "
from studio.app.common.db.config import DATABASE_CONFIG, get_ssl_creator
from sqlalchemy import create_engine, text
creator = get_ssl_creator()
kwargs = {'creator': creator} if creator else {}
engine = create_engine(DATABASE_CONFIG.DATABASE_URL, **kwargs)
with engine.connect() as c:
    r = c.execute(text('SHOW STATUS LIKE \"Ssl_cipher\"'))
    cipher = r.fetchone()
    print(f'SSL: {cipher[1] if cipher and cipher[1] else \"disabled\"}')
engine.dispose()
" 2>&1 || echo "WARNING: SSL verification failed (see above)"

# Run Alembic upgrade - if migrations fail, the container will exit
# This causes ECS to mark the deployment as failed and revert to the previous version
echo "Running Alembic upgrade..."
if ! alembic upgrade head 2>&1; then
    echo "ERROR: Database migration failed!"
    echo "Container will exit to prevent data loss and trigger deployment rollback."
    echo "Please investigate the migration error before redeploying."
    exit 1
fi
echo "Database migrations completed successfully"

# Seed subscription plans from SUBSCRIPTION_PLANS_CONFIG env var
if [ -n "$SUBSCRIPTION_PLANS_CONFIG" ]; then
    echo "Seeding subscription plans..."
    python3 /app/scripts/seed_subscription_plans.py || echo "WARNING: Subscription plan seeding failed (non-fatal)"
else
    echo "SUBSCRIPTION_PLANS_CONFIG not set, skipping subscription plan seeding"
fi

# Create test users from TEST_USERS_CONFIG env var
if [ -n "$TEST_USERS_CONFIG" ]; then
    echo "Creating test users..."
    cd /app/scripts && python3 create_test_users.py || echo "WARNING: Test user creation failed (non-fatal)"
    cd /app
else
    echo "TEST_USERS_CONFIG not set, skipping test user creation"
fi

# Verify backend configuration
# Ensures required environment variables are set before starting the application
echo "Host: $BACKEND_HOST"
echo "Port: $BACKEND_PORT"
if [ -z "$BACKEND_HOST" ] || [ -z "$BACKEND_PORT" ]; then
    echo "Please provide 'BACKEND_HOST' and 'BACKEND_PORT' environment variables"
    exit 1
fi

# Configure Uvicorn worker processes
# Workers handle concurrent requests. Recommended formula: (2 × CPU cores) + 1
# t3.large has 2 vCPUs, so optimal range is 2-5 workers
# Set via environment variable UVICORN_WORKERS, defaults to 5 for production use
UVICORN_WORKERS=${UVICORN_WORKERS:-5}
echo "Uvicorn workers: $UVICORN_WORKERS"

# Get EC2 instance ID for free tier user tracking
# This is required for the FreeUserActivityMiddleware to track users across instances
echo "Retrieving EC2 instance ID from ECS container metadata..."
INSTANCE_ID=""

# Temporarily disable exit-on-error to allow graceful handling if metadata is unavailable
set +e

# Get the container instance ARN from ECS metadata
if [ -n "$ECS_CONTAINER_METADATA_URI_V4" ]; then
    TASK_METADATA=$(curl -s "${ECS_CONTAINER_METADATA_URI_V4}/task" 2>/dev/null)
    TASK_ARN=$(echo "$TASK_METADATA" | python3 -c "import sys, json; print(json.load(sys.stdin)['TaskARN'])" 2>/dev/null)

    if [ -n "$TASK_ARN" ]; then
        echo "Found ECS task: $TASK_ARN"
        # Extract cluster name from task ARN (format: arn:aws:ecs:region:account:task/cluster-name/task-id)
        CLUSTER_NAME=$(echo "$TASK_ARN" | cut -d'/' -f2)
        echo "Cluster: $CLUSTER_NAME"

        # Get container instance ARN
        CONTAINER_INSTANCE_ARN=$(aws ecs describe-tasks \
            --cluster "$CLUSTER_NAME" \
            --tasks "$TASK_ARN" \
            --query 'tasks[0].containerInstanceArn' \
            --output text 2>/dev/null)

        if [ -n "$CONTAINER_INSTANCE_ARN" ] && [ "$CONTAINER_INSTANCE_ARN" != "None" ]; then
            echo "Container instance ARN: $CONTAINER_INSTANCE_ARN"
            # Get EC2 instance ID from container instance
            INSTANCE_ID=$(aws ecs describe-container-instances \
                --cluster "$CLUSTER_NAME" \
                --container-instances "$CONTAINER_INSTANCE_ARN" \
                --query 'containerInstances[0].ec2InstanceId' \
                --output text 2>/dev/null)

            if [ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != "None" ]; then
                echo "Got instance ID from ECS container metadata: $INSTANCE_ID"
            fi
        else
            echo "Could not retrieve container instance ARN"
        fi
    else
        echo "Could not retrieve task ARN from ECS metadata"
    fi
else
    echo "ECS_CONTAINER_METADATA_URI_V4 not available"
fi

# Re-enable exit-on-error
set -e

# Export for the application to use
if [ -n "$INSTANCE_ID" ] && [ "$INSTANCE_ID" != "None" ]; then
    export INSTANCE_ID
    echo "INSTANCE_ID set to: $INSTANCE_ID"
else
    echo "WARNING: Could not retrieve EC2 instance ID. Free tier user tracking will not work."
    echo "This is expected in local development, but should not happen in production."
fi

# Forward ECS stop signals (SIGTERM) to child processes so the cleanup
# worker and app can shut down gracefully; without this the signal reaches
# only the shell and children are killed abruptly.
#
# Install the trap BEFORE launching any child so a stop signal arriving
# during startup (between process launch and trap installation) is still
# forwarded. CLEANUP_PID / APP_PID are empty until their process starts;
# the guards below skip any child that hasn't launched yet.
_forward_shutdown() {
    echo "Received shutdown signal, forwarding to child processes..."
    [ -n "$CLEANUP_PID" ] && kill -TERM "$CLEANUP_PID" 2>/dev/null
    [ -n "$APP_PID" ] && kill -TERM "$APP_PID" 2>/dev/null
}
trap _forward_shutdown TERM INT

# Start standalone cleanup worker on free-tier instances
# This runs DataCleanupJob in its own process, filtered by INSTANCE_ID,
# so cleanup happens on the instance that owns the local EBS data.
if [ "$ENABLE_LOCAL_CLEANUP" = "1" ]; then
    echo "Starting cleanup worker (ENABLE_LOCAL_CLEANUP=1)..."
    poetry run python -m studio.cleanup_worker &
    CLEANUP_PID=$!
    echo "Cleanup worker started (PID: $CLEANUP_PID)"
fi

# Start the application in background
echo "Starting application..."
poetry run python main.py --host="$BACKEND_HOST" --port="$BACKEND_PORT" --workers="$UVICORN_WORKERS" &
APP_PID=$!

# Allow initial startup time matching ECS health check startPeriod
echo "Waiting for initial startup..."
sleep 30

# Single initial health check before load balancer check
echo "Verifying initial health..."
if ! curl -v "http://${BACKEND_HOST}:${BACKEND_PORT}/health"; then
    echo "Initial health check failed"
    # Don't exit - let ECS handle it
fi

# Load balancer health check function
# Verifies that the application is accessible through the load balancer
check_load_balancer() {
    if [ -n "$AWS_SERVICE_URL" ]; then
        echo "Checking load balancer status..."
        readonly MAX_TRIES=30
        readonly WAIT_SECONDS=10
        local counter=0

        # Try for 5 minutes (30 attempts * 10 seconds)
        until curl -s -o /dev/null --max-time ${WAIT_SECONDS} "$AWS_SERVICE_URL"
        do
            sleep ${WAIT_SECONDS}
            [[ $counter -eq $MAX_TRIES ]] && echo "Load balancer not ready after 5 minutes" && return 1
            echo "Attempt $counter: Waiting for load balancer..."
            ((counter++))
        done
        echo "Load balancer is ready"
        return 0
    else
        echo "AWS_SERVICE_URL not provided, skipping load balancer check"
        return 0
    fi
}

# Run load balancer check in background
# This allows parallel checking while the application is starting
check_load_balancer &
LB_CHECK_PID=$!

# Wait for all background processes to complete
# This ensures the container keeps running as long as the application is running
wait $APP_PID
wait $LB_CHECK_PID
[ -n "$CLEANUP_PID" ] && wait "$CLEANUP_PID" 2>/dev/null
