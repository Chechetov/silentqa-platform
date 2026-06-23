#!/usr/bin/env bash
# One-shot storage cutover: nest existing audio/results under realestate/.
# Run with the worker STOPPED, right after the DB cutover migration.
set -euo pipefail
AUDIO="${AUDIO_STORAGE_PATH:-./data/audio}"
RESULTS="${RESULTS_STORAGE_PATH:-./data/results}"
SLUG="realestate"

if [ -d "$AUDIO/$SLUG" ]; then
    echo "$AUDIO/$SLUG already exists — storage already migrated?" >&2
    exit 1
fi
mkdir -p "$AUDIO/$SLUG" "$RESULTS/$SLUG"
for d in sessions amocrm; do
    [ -d "$AUDIO/$d" ] && mv "$AUDIO/$d" "$AUDIO/$SLUG/$d"
done
shopt -s nullglob
for p in "$RESULTS"/*; do
    base="$(basename "$p")"
    # webhooks.json stays at the root until Plan 2 removes the route
    [ "$base" = "$SLUG" ] && continue
    [ "$base" = "webhooks.json" ] && continue
    mv "$p" "$RESULTS/$SLUG/$base"
done
echo "storage migrated under $AUDIO/$SLUG and $RESULTS/$SLUG"
