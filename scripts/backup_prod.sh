#!/usr/bin/env bash
# Ежедневный off-box бэкап платформы SilentQA (/root/projects/silentqa) на релей-VPS.
# Дампит Postgres (pg_dump -Fc + pg_dumpall --globals-only), копирует Redis dump.rdb,
# тарит .env + companies/ в приватный архив, rsync-ит дампы дня и зеркалит медиа
# (data/) на релей, ротирует старые daily-каталоги (локально и на релее).
# Идемпотентен, безопасен к повторному запуску. Секреты в stdout НЕ печатаются.
# Установка/запуск — за контроллером; юниты см. ops/systemd/silentqa-backup.{service,timer}.
set -euo pipefail
umask 077   # всё создаваемое — 0600 (файлы) / 0700 (каталоги): в бэкапе секреты

# --- конфиг (env с дефолтами) ---
BACKUP_SSH_KEY="${BACKUP_SSH_KEY:-/root/.ssh/rogov_relay}"
BACKUP_REMOTE="${BACKUP_REMOTE:-root@89.207.255.231}"
BACKUP_REMOTE_DIR="${BACKUP_REMOTE_DIR:-/root/backups/silentqa-box}"
BACKUP_KEEP_DAILY="${BACKUP_KEEP_DAILY:-7}"
PROD_DIR="${PROD_DIR:-/root/projects/silentqa}"
BACKUP_LOCAL_ROOT="${BACKUP_LOCAL_ROOT:-/root/backups/daily}"

DATE="$(date +%F)"                       # YYYY-MM-DD
STAGING="$BACKUP_LOCAL_ROOT/$DATE"
SSH_OPTS=(-i "$BACKUP_SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10)
START=$SECONDS

log() { printf '[backup] %s\n' "$*" >&2; }
die() { printf '[backup] FATAL: %s\n' "$*" >&2; exit 1; }

[ -f "$PROD_DIR/.env" ]  || die "прод .env не найден: $PROD_DIR/.env"
[ -r "$BACKUP_SSH_KEY" ] || die "SSH-ключ недоступен: $BACKUP_SSH_KEY"

mkdir -p "$STAGING"
chmod 700 "$STAGING"

# --- прод-окружение: читаем только URL-ы; значения НЕ логируются ---
set -a
# shellcheck disable=SC1091
source "$PROD_DIR/.env"
set +a
: "${DATABASE_URL_SYNC:?DATABASE_URL_SYNC отсутствует в $PROD_DIR/.env}"

# --- разбор DATABASE_URL_SYNC (python3/urllib надёжнее sed; секреты в env, не в stdout) ---
mapfile -t _db < <(_PARSE_URL="$DATABASE_URL_SYNC" python3 - <<'PY'
import os
from urllib.parse import urlparse, unquote
u = urlparse(os.environ["_PARSE_URL"])
d = lambda x: unquote(x) if x else ''
print(d(u.hostname) or 'localhost')
print(u.port or 5432)
print(d(u.username))
print(d(u.password))
print((u.path or '').lstrip('/'))
PY
)
PGHOST="${_db[0]}"; PGPORT="${_db[1]}"; PGUSER="${_db[2]}"
export PGPASSWORD="${_db[3]}"; PGDATABASE="${_db[4]}"
[ -n "$PGDATABASE" ] || die "не удалось разобрать имя БД из DATABASE_URL_SYNC"

# --- 1. Postgres: дамп БД (custom format) + глобальные объекты (роли/tablespaces) ---
log "pg_dump $PGDATABASE → db.dump (-Fc)"
pg_dump -Fc -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -d "$PGDATABASE" -f "$STAGING/db.dump"
log "pg_dumpall --globals-only → globals.sql"
pg_dumpall --globals-only -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -l "$PGDATABASE" -f "$STAGING/globals.sql"

# --- 2. Redis: копия dump.rdb (best-effort — основная durability переведена на AOF) ---
REDIS_STAT="skipped"
if [ -n "${REDIS_URL:-}" ]; then
  mapfile -t _rd < <(_PARSE_URL="$REDIS_URL" python3 - <<'PY'
import os
from urllib.parse import urlparse, unquote
u = urlparse(os.environ["_PARSE_URL"])
print(u.hostname or 'localhost')
print(u.port or 6379)
print(unquote(u.password) if u.password else '')
PY
)
  RHOST="${_rd[0]}"; RPORT="${_rd[1]}"; RPASS="${_rd[2]}"
  [ -n "$RPASS" ] && export REDISCLI_AUTH="$RPASS"
  if RDIR="$(redis-cli -h "$RHOST" -p "$RPORT" CONFIG GET dir 2>/dev/null | tail -n1)" \
     && RFILE="$(redis-cli -h "$RHOST" -p "$RPORT" CONFIG GET dbfilename 2>/dev/null | tail -n1)" \
     && [ -n "$RDIR" ] && [ -n "$RFILE" ] && [ -f "$RDIR/$RFILE" ]; then
    cp -p "$RDIR/$RFILE" "$STAGING/redis-dump.rdb"
    REDIS_STAT="ok"
  else
    log "WARN: Redis dump.rdb недоступен (AOF-only / нет snapshot) — пропуск, не фатально"
  fi
  unset REDISCLI_AUTH || true
fi

# --- 3. Конфиг: .env + companies/ в приватный tar.gz (0600 — секреты) ---
log "tar .env + companies/ → config.tar.gz"
tar -czf "$STAGING/config.tar.gz" -C "$PROD_DIR" .env companies
chmod 600 "$STAGING/config.tar.gz"

# --- 4. rsync на релей: (а) дампы дня, (б) зеркало медиа data/ ---
log "rsync: дампы дня + зеркало медиа → релей"
ssh "${SSH_OPTS[@]}" "$BACKUP_REMOTE" \
    "mkdir -p '$BACKUP_REMOTE_DIR/daily/$DATE' '$BACKUP_REMOTE_DIR/data-mirror' && chmod 700 '$BACKUP_REMOTE_DIR'"
rsync -az -e "ssh ${SSH_OPTS[*]}" "$STAGING/" "$BACKUP_REMOTE:$BACKUP_REMOTE_DIR/daily/$DATE/"
if [ -d "$PROD_DIR/data" ]; then
  rsync -az --delete -e "ssh ${SSH_OPTS[*]}" "$PROD_DIR/data/" "$BACKUP_REMOTE:$BACKUP_REMOTE_DIR/data-mirror/"
else
  log "WARN: $PROD_DIR/data не найден — зеркало медиа пропущено"
fi

# --- 5. Ротация: держать BACKUP_KEEP_DAILY последних daily (локально и на релее) ---
prune_local() {
  local dirs=() n i
  mapfile -t dirs < <(find "$BACKUP_LOCAL_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort)
  n=${#dirs[@]}
  for ((i = 0; i < n - BACKUP_KEEP_DAILY; i++)); do rm -rf "${dirs[i]}"; done
}
prune_local
ssh "${SSH_OPTS[@]}" "$BACKUP_REMOTE" \
    "find '$BACKUP_REMOTE_DIR/daily' -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | head -n -$BACKUP_KEEP_DAILY | xargs -r rm -rf"

# --- итог: одна строка (размеры + длительность), exit 0 ---
sz() { du -sh "$1" 2>/dev/null | cut -f1; }
DUR=$((SECONDS - START))
printf '[backup] OK %s | db=%s globals=%s redis=%s config=%s total=%s | media-mirror synced | %ss\n' \
  "$DATE" "$(sz "$STAGING/db.dump")" "$(sz "$STAGING/globals.sql")" \
  "$REDIS_STAT" "$(sz "$STAGING/config.tar.gz")" "$(sz "$STAGING")" "$DUR"
exit 0
