#!/usr/bin/env bash
# extension-yandex/ — генерируемый байт-в-байт клон extension/ (+ свой README.md).
# Без флагов — синхронизировать; --check — только проверить (CI-гард).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "${1:-}" = "--check" ]; then
    if diff -r --exclude=README.md extension extension-yandex >/dev/null; then
        echo "extension-yandex синхронизирован"
    else
        echo "extension-yandex РАЗЪЕХАЛСЯ с extension/ — запусти scripts/sync_yandex_extension.sh" >&2
        diff -rq --exclude=README.md extension extension-yandex >&2 || true
        exit 1
    fi
    exit 0
fi

# --delete НЕ трогает excluded README.md (rsync удаляет excluded только с --delete-excluded)
rsync -a --delete --exclude=README.md extension/ extension-yandex/
echo "extension-yandex обновлён из extension/"
