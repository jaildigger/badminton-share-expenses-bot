#!/bin/sh
# Always target this bot's production service, regardless of current CLI linkage.
set -eu
cd "$(dirname "$0")/.."
if command -v railway >/dev/null 2>&1; then
    railway_bin=$(command -v railway)
elif [ -x /tmp/bad-bot-railway-cli/node_modules/.bin/railway ]; then
    railway_bin=/tmp/bad-bot-railway-cli/node_modules/.bin/railway
else
    echo 'Railway CLI не найден. Установите @railway/cli и выполните railway login.' >&2
    exit 1
fi
exec "$railway_bin" ssh -i "$HOME/.ssh/id_ed25519" \
    --project e189a8d2-ff5b-4d8d-a1a5-5e26448fb209 \
    --environment production --service bad-bot \
    -- python -c "$(cat badminton/reset_history.py)" "$@"
