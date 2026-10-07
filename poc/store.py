"""Single-process SQLite repository. Every personal row is scoped by owner_id."""
import json
import hashlib
import os
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet

from .models import MonitorConfig


class Store:
    def __init__(self, directory: Path, owner="owner"):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.owner = owner
        key_path = directory / "storage.key"
        if not key_path.exists():
            with os.fdopen(os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as f:
                f.write(Fernet.generate_key())
        self.fernet = Fernet(key_path.read_bytes())
        self.identity_key = hashlib.sha256(key_path.read_bytes() + b"profile-id").digest()
        self.db = sqlite3.connect(directory / "app.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS settings(owner TEXT PRIMARY KEY, config TEXT NOT NULL, enabled INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, owner TEXT, at REAL, kind TEXT, message TEXT);
          CREATE TABLE IF NOT EXISTS seen(owner TEXT, profile TEXT, key TEXT, at REAL, PRIMARY KEY(owner,profile,key));
          CREATE TABLE IF NOT EXISTS subscriptions(owner TEXT, id TEXT, encrypted TEXT, PRIMARY KEY(owner,id));
          CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, owner TEXT, sub_id TEXT, payload TEXT, due REAL, expires REAL, tries INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS limits(owner TEXT, endpoint TEXT, until REAL, PRIMARY KEY(owner,endpoint));
          CREATE TABLE IF NOT EXISTS panel_sessions(token TEXT PRIMARY KEY, owner TEXT, expires REAL, password_version TEXT);
        """)
        self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?,0)", (owner, MonitorConfig().model_dump_json()))
        self.db.commit()

    def settings(self):
        row = self.db.execute("SELECT config,enabled FROM settings WHERE owner=?", (self.owner,)).fetchone()
        return MonitorConfig.model_validate_json(row["config"]), bool(row["enabled"])

    def save_settings(self, config, enabled):
        with self.db:
            self.db.execute("UPDATE settings SET config=?,enabled=? WHERE owner=?",
                            (config.model_dump_json(), int(enabled), self.owner))

    def event(self, kind, message):
        with self.db:
            self.db.execute("INSERT INTO events(owner,at,kind,message) VALUES (?,?,?,?)",
                            (self.owner, time.time(), kind, message[:500]))
            self.db.execute("DELETE FROM events WHERE owner=? AND id NOT IN (SELECT id FROM events WHERE owner=? ORDER BY id DESC LIMIT 200)", (self.owner, self.owner))

    def events(self):
        return [dict(r) for r in self.db.execute("SELECT at,kind,message FROM events WHERE owner=? ORDER BY id DESC LIMIT 40", (self.owner,))]

    def cooldown(self, path):
        row = self.db.execute("SELECT until FROM limits WHERE owner=? AND endpoint=?", (self.owner, path)).fetchone()
        return row[0] if row else 0

    def defer(self, path, until):
        with self.db:
            self.db.execute("INSERT INTO limits VALUES (?,?,?) ON CONFLICT(owner,endpoint) DO UPDATE SET until=max(until,excluded.until)", (self.owner, path, until))

    def subscribe(self, id, subscription):
        encrypted = self.fernet.encrypt(json.dumps(subscription).encode()).decode()
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?,?)", (self.owner, id, encrypted))

    def subscriptions(self):
        return [(r["id"], json.loads(self.fernet.decrypt(r["encrypted"].encode())))
                for r in self.db.execute("SELECT * FROM subscriptions WHERE owner=?", (self.owner,))]

    def unsubscribe(self, id):
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE owner=? AND id=?", (self.owner, id))
            self.db.execute("DELETE FROM outbox WHERE owner=? AND sub_id=?", (self.owner, id))

    def enqueue(self, payload, ttl=900):
        with self.db:
            for sub in self.db.execute("SELECT id FROM subscriptions WHERE owner=?", (self.owner,)).fetchall():
                self.db.execute("INSERT INTO outbox(owner,sub_id,payload,due,expires) VALUES (?,?,?,?,?)",
                                (self.owner, sub[0], json.dumps(payload), time.time(), time.time() + ttl))

    def record_matches(self, profile, matches, payload):
        # Seen + outbox are committed together, surviving container restarts.
        with self.db:
            self.db.execute("DELETE FROM seen WHERE owner=? AND at<?", (self.owner, time.time() - 60*86400))
            new = [s for s in matches if not self.db.execute("SELECT 1 FROM seen WHERE owner=? AND profile=? AND key=?", (self.owner, profile, s["key"])).fetchone()]
            for s in new:
                self.db.execute("INSERT INTO seen VALUES (?,?,?,?)", (self.owner, profile, s["key"], time.time()))
            if new:
                # Don't use nested transaction helper here.
                body = json.dumps(payload(new))
                for sub in self.db.execute("SELECT id FROM subscriptions WHERE owner=?", (self.owner,)).fetchall():
                    self.db.execute("INSERT INTO outbox(owner,sub_id,payload,due,expires) VALUES (?,?,?,?,?)", (self.owner, sub[0], body, time.time(), time.time()+900))
        return new

    def close(self):
        self.db.close()
