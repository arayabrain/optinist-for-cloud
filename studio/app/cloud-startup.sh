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

# Check load balancer (consider removing this if not necessary)
poetry run python main.py --check-load-balancer

echo "Host: $HOST"
echo "Port: $PORT"

if [ -z "$HOST" ] || [ -z "$PORT" ]; then
    echo "Please provide HOST and PORT environment variables"
    exit 1
fi

# Start the application
exec poetry run python main.py --host="$HOST" --port="$PORT"
