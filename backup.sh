#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
umask 077
backup_dir="${HOME}/catcher-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir/data"
docker compose -p info-login-poc exec -T app python - <<'PY'
import sqlite3
source = sqlite3.connect('/app/data/app.sqlite3')
with sqlite3.connect('/app/data/backup.sqlite3') as target:
    source.backup(target)
source.close()
PY
docker compose -p info-login-poc cp app:/app/data/backup.sqlite3 "$backup_dir/data/app.sqlite3"
docker compose -p info-login-poc cp app:/app/data/storage.key "$backup_dir/data/storage.key"
docker compose -p info-login-poc cp app:/app/data/vapid.pem "$backup_dir/data/vapid.pem"
install -m 600 .env "$backup_dir/.env"
echo "Kopia konfiguracji, bazy i kluczy: $backup_dir"
