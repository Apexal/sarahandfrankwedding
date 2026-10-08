#!/bin/bash
set -e

# The container starts as root, then re-execs itself as nonroot.
if [ "$(id -u)" = "0" ]; then
    # Fly volumes mount owned by root; hand the mount to nonroot before dropping
    # privileges so SQLite can create/write the database (and its -wal/-shm files).
    if [ -d /data ]; then
        chown nonroot:nonroot /data
    fi
    exec setpriv --reuid=nonroot --regid=nonroot --init-groups "$0" "$@"
fi

# setpriv changes the uid but not the environment — without this, HOME is still
# /root and gunicorn's control server can't create its socket there.
export HOME=/home/nonroot

cleanup() {
    echo "Shutting down..."
    kill "$WORKER_PID" "$SERVER_PID" 2>/dev/null || true
    wait "$WORKER_PID" "$SERVER_PID" 2>/dev/null || true
    exit 0
}
trap cleanup TERM INT

echo "Starting migrations..."
python manage.py migrate --noinput

echo "Ensuring cache table exists..."
python manage.py createcachetable

echo "Starting steady_queue worker..."
python manage.py steady_queue &
WORKER_PID=$!

echo "Starting webserver..."
gunicorn -c ./gunicorn.conf.py --bind [::]:8000 --workers 3 sarahandfrankwedding.wsgi &
SERVER_PID=$!

# If any supervised process dies, exit so Fly restarts the machine — otherwise
# a dead worker leaves a "healthy" web process with no background jobs running.
wait -n "$WORKER_PID" "$SERVER_PID"
echo "A supervised process exited unexpectedly; shutting down."
exit 1
