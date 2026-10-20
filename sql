#!/usr/bin/env sh
# Lance le client SQL dans le conteneur (démarre la stack au besoin).
set -e
cd "$(dirname "$0")"
docker compose up -d
exec docker compose exec tui sql-tui
