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
          CREATE TABLE IF NOT EXISTS schedule_calendars(owner TEXT, profile TEXT, payload TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(owner,profile));
          CREATE TABLE IF NOT EXISTS subscriptions(owner TEXT, id TEXT, encrypted TEXT, PRIMARY KEY(owner,id));
          CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY, owner TEXT, sub_id TEXT, payload TEXT, due REAL, expires REAL, tries INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS limits(owner TEXT, endpoint TEXT, until REAL, PRIMARY KEY(owner,endpoint));
          CREATE TABLE IF NOT EXISTS monitor_blocks(owner TEXT PRIMARY KEY, status INTEGER NOT NULL, diagnostic TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS schedule_timing(owner TEXT PRIMARY KEY, last_success REAL, next_check REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS panel_sessions(token TEXT PRIMARY KEY, owner TEXT, expires REAL, password_version TEXT);
        """)
        legacy_snapshots = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schedule_snapshots'"
        ).fetchone()
        if legacy_snapshots:
            with self.db:
                self.db.execute("""
                    INSERT OR IGNORE INTO schedule_calendars(owner,profile,payload,at)
                    SELECT legacy.owner, legacy.profile, legacy.payload, legacy.at
                    FROM schedule_snapshots AS legacy
                    WHERE legacy.at = (
                        SELECT MAX(latest.at) FROM schedule_snapshots AS latest
                        WHERE latest.owner = legacy.owner AND latest.profile = legacy.profile
                    )
                """)
                self.db.execute("DROP TABLE schedule_snapshots")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES (?,?,0)", (owner, MonitorConfig().model_dump_json()))
        self.db.commit()

    def settings(self):
        row = self.db.execute("SELECT config,enabled FROM settings WHERE owner=?", (self.owner,)).fetchone()
        config_data = json.loads(row["config"])
        if config_data.get("interval_seconds") in {360, 600}:
            config_data["interval_seconds"] = 1200
            with self.db:
                self.db.execute("UPDATE settings SET config=? WHERE owner=?",
                                (json.dumps(config_data), self.owner))
        return MonitorConfig.model_validate(config_data), bool(row["enabled"])

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

    def schedule_block(self):
        row = self.db.execute("SELECT status,diagnostic FROM monitor_blocks WHERE owner=?", (self.owner,)).fetchone()
        return dict(row) if row else None

    def block_schedule(self, status, diagnostic):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO monitor_blocks VALUES (?,?,?)",
                            (self.owner, status, diagnostic or ""))

    def clear_schedule_block(self):
        with self.db:
            self.db.execute("DELETE FROM monitor_blocks WHERE owner=?", (self.owner,))

    def schedule_timing(self):
        row = self.db.execute(
            "SELECT last_success,next_check FROM schedule_timing WHERE owner=?",
            (self.owner,),
        ).fetchone()
        return dict(row) if row else None

    def save_schedule_timing(self, last_success, next_check):
        with self.db:
            self.db.execute(
                "INSERT INTO schedule_timing VALUES (?,?,?) "
                "ON CONFLICT(owner) DO UPDATE SET "
                "last_success=excluded.last_success,next_check=excluded.next_check",
                (self.owner, last_success, next_check),
            )

    def schedule_calendar(self, profile):
        rows = self.db.execute(
            "SELECT payload,at FROM schedule_calendars "
            "WHERE owner=? AND profile=?",
            (self.owner, profile),
        ).fetchone()
        if not rows:
            return None
        payload = json.loads(rows["payload"])
        return {
            "requested_start": payload["requested_start"],
            "calendar_dates": payload["calendar_dates"],
            "at": rows["at"],
            "slots": payload["slots"],
        }

    def save_schedule_calendar(self, profile, requested_start, calendar_dates, slots, at):
        payload = json.dumps({
            "requested_start": requested_start,
            "calendar_dates": calendar_dates,
            "slots": slots,
        })
        with self.db:
            self.db.execute(
                "INSERT INTO schedule_calendars VALUES (?,?,?,?) "
                "ON CONFLICT(owner,profile) DO UPDATE SET "
                "payload=excluded.payload,at=excluded.at",
                (self.owner, profile, payload, at),
            )

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
