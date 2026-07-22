#!/usr/bin/env bash
# Деплой прода SilentQA (/root/projects/silentqa): FF-merge → миграции → рестарт.
# Миграции ДО рестарта: если упали — старый код продолжает работать,
# даунтайма нет (раньше миграция жила в lifespan и роняла старт).
set -euo pipefail

PROD_DIR="${PROD_DIR:-/root/projects/silentqa}"
BRANCH="${BRANCH:-multi-tenant-core-phase1}"
# Прод-remote назван "github", а не "origin" (проверено 2026-07-02:
# `git -C /root/projects/silentqa remote -v`). Переопределяемо через REMOTE.
REMOTE="${REMOTE:-github}"

cd "$PROD_DIR"
PREV_SHA=$(git rev-parse HEAD)
echo "rollback point: $PREV_SHA (откат: git reset --hard $PREV_SHA && systemctl restart silentqa-backend silentqa-worker silentqa-worker-io)"
git fetch "$REMOTE"
git merge --ff-only "$REMOTE/$BRANCH"

# Зависимости (no-op, если requirements не менялись)
.venv/bin/pip install -q -r backend/requirements.txt -r worker/requirements.txt

# Миграции с прод-окружением — до рестарта сервисов
set -a; source .env; set +a
(cd backend && "$PROD_DIR/.venv/bin/python" -m app.migrate)

systemctl restart silentqa-backend silentqa-worker silentqa-worker-io

# Readiness с ретраями (до ~30с): /health/ready = процесс жив И БД доступна
for i in $(seq 1 15); do
    sleep 2
    if curl -sf localhost:8007/health/ready >/dev/null; then
        echo "deploy OK: /health/ready отвечает (попытка $i)"
        exit 0
    fi
done
echo "deploy FAILED: /health/ready не отвечает 30с — journalctl -u silentqa-backend -n 50"
echo "откат: git reset --hard $PREV_SHA && systemctl restart silentqa-backend silentqa-worker silentqa-worker-io"
exit 1
