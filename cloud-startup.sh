#!/bin/bash
set -e

echo 'Starting container'
echo 'Attempting to connect to RDS'
echo "DB_HOST: ${DB_HOST}"
echo "DB_USER: ${DB_USER}"
echo "DB_NAME: ${DB_NAME}"

# Wait for RDS to be available
max_tries=30
counter=0
until mysql -h "${DB_HOST}" -u "${DB_USER}" -p"${DB_PASSWORD}" "${DB_NAME}" -e 'SELECT 1;'
do
    sleep 2
    [[ counter -eq $max_tries ]] && echo "Failed to connect to Database" && exit 1
    echo "Attempt $counter: Waiting for Database..."
    ((counter++))
done

echo 'Database connection successful'

# Run database migrations
alembic upgrade head

# check backend input parameters
echo "Host: $BACKEND_HOST"
echo "Port: $BACKEND_PORT"

if [ -z "$BACKEND_HOST" ] || [ -z "$BACKEND_PORT" ]; then
    echo "Please provide 'BACKEND_HOST' and 'BACKEND_PORT' environment variables"
    exit 1
fi

# Start the application in the background
poetry run python main.py --host="$BACKEND_HOST" --port="$BACKEND_PORT" &
APP_PID=$!

# Check load balancer
check_load_balancer() {
    if [ -n "$AWS_SERVICE_URL" ]; then
        echo "Checking load balancer status..."
        max_tries=30
        counter=0
        until curl -s -o /dev/null --max-time 10 "$AWS_SERVICE_URL"
        do
            sleep 10
            [[ counter -eq $max_tries ]] && echo "Load balancer not ready after 5 minutes" && return 1
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

# Run load balancer check
check_load_balancer &
LB_CHECK_PID=$!

# Wait for both processes
wait $APP_PID
wait $LB_CHECK_PID
