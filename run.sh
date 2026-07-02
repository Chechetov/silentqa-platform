#!/usr/bin/env bash
# Запуск всех сервисов Rogov Analytics локально.
# Требования: PostgreSQL, Redis, ffmpeg, Python 3.12+, venv
#
# Использование:
#   ./run.sh backend   — только API-сервер
#   ./run.sh worker    — только Celery-воркер
#   ./run.sh all       — бэкенд + воркер вместе (фоново)
#   ./run.sh stop      — остановить фоновые процессы

set -euo pipefail
cd "$(dirname "$0")"

# Загрузить .env если есть
if [ -f .env ]; then
    set -a; source .env; set +a
fi

# Дефолты
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://realestate:changeme@localhost:5434/realestate}"
export DATABASE_URL_SYNC="${DATABASE_URL_SYNC:-postgresql+psycopg2://realestate:changeme@localhost:5434/realestate}"
export REDIS_URL="${REDIS_URL:-redis://localhost:6381/0}"
export AUDIO_STORAGE_PATH="${AUDIO_STORAGE_PATH:-./data/audio}"
export RESULTS_STORAGE_PATH="${RESULTS_STORAGE_PATH:-./data/results}"

# Создать директории для данных
mkdir -p "$AUDIO_STORAGE_PATH" "$RESULTS_STORAGE_PATH"

run_migrations() {
    echo "Applying migrations (shared + все тенанты)..."
    (cd backend && python -m app.migrate)
}

PIDFILE_BACKEND=".pid.backend"
PIDFILE_WORKER=".pid.worker"

start_backend() {
    run_migrations
    echo "Starting backend on :8002..."
    cd backend
    uvicorn app.main:app --host 0.0.0.0 --port 8002 "$@"
}

start_worker() {
    echo "Starting Celery worker..."
    cd worker
    celery -A tasks.celery_app worker \
        --loglevel=info \
        --concurrency="${CELERY_CONCURRENCY:-1}" \
        --max-tasks-per-child=10 \
        -Q default,transcription,analysis "$@"
}

start_all() {
    echo "Starting all services..."
    mkdir -p data/audio data/results
    run_migrations

    cd backend
    uvicorn app.main:app --host 0.0.0.0 --port 8002 &
    echo $! > "../$PIDFILE_BACKEND"
    cd ..

    cd worker
    celery -A tasks.celery_app worker \
        --loglevel=info \
        --concurrency="${CELERY_CONCURRENCY:-1}" \
        --max-tasks-per-child=10 \
        -Q default,transcription,analysis &
    echo $! > "../$PIDFILE_WORKER"
    cd ..

    echo "Backend PID: $(cat $PIDFILE_BACKEND)"
    echo "Worker PID:  $(cat $PIDFILE_WORKER)"
    echo "Press Ctrl+C to stop all"
    trap "kill $(cat $PIDFILE_BACKEND) $(cat $PIDFILE_WORKER) 2>/dev/null; rm -f $PIDFILE_BACKEND $PIDFILE_WORKER" EXIT
    wait
}

stop_all() {
    for f in "$PIDFILE_BACKEND" "$PIDFILE_WORKER"; do
        if [ -f "$f" ]; then
            pid=$(cat "$f")
            if kill -0 "$pid" 2>/dev/null; then
                echo "Stopping PID $pid..."
                kill "$pid"
            fi
            rm -f "$f"
        fi
    done
    echo "Stopped."
}

case "${1:-all}" in
    backend) start_backend "${@:2}" ;;
    worker)  start_worker "${@:2}" ;;
    all)     start_all ;;
    stop)    stop_all ;;
    *)       echo "Usage: $0 {backend|worker|all|stop}"; exit 1 ;;
esac
