#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
OLD_DIR="${OLD_APP_DIR:-/home/ubuntu/info-login-poc}"
if [[ ! -f .env ]]; then
  if [[ ! -f "$OLD_DIR/.env" ]]; then
    echo 'Brak konfiguracji. Skopiuj .env ze starej aplikacji do tego katalogu.' >&2
    exit 1
  fi
  install -m 600 "$OLD_DIR/.env" .env
fi
chmod 600 .env
docker compose -p info-login-poc --profile public config --quiet
echo 'Budowanie aktualizacji. Dotychczasowa aplikacja nadal działa.'
docker compose -p info-login-poc --profile public build app
echo 'Uruchamianie nowej wersji z zachowaniem certyfikatu i adresu.'
docker compose -p info-login-poc --profile public up -d --wait --wait-timeout 120
docker compose -p info-login-poc ps
echo 'Gotowe. Otwórz dotychczasowy adres. Zaloguj ponownie do Info-Kierowca.'
