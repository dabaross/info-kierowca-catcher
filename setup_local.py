"""Generate private configuration without replacing an existing configuration."""
import os
import secrets
from pathlib import Path

target = Path(__file__).parent / ".env"
with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
    f.write("PUBLIC_ORIGIN=http://localhost:8000\n")
    f.write("PANEL_PASSWORD=" + secrets.token_urlsafe(32) + "\n")
    f.write("HANDOFF_SCHEMES=mobywatel\nHEADED=0\n")
print("Utworzono .env. Login panelu: owner. Hasło odczytaj lokalnie z PANEL_PASSWORD.")
