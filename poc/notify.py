import asyncio
import base64
import hashlib
import json
import os
import time
from contextlib import suppress
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import webpush, WebPushException


def validate_subscription(data):
    if not isinstance(data, dict):
        raise ValueError("Nieprawidłowa subskrypcja")
    endpoint = data.get("endpoint", "")
    url = urlsplit(endpoint)
    host = url.hostname or ""
    allowed = (host == "fcm.googleapis.com" or host == "updates.push.services.mozilla.com"
               or host.endswith(".push.apple.com") or host == "web.push.apple.com")
    if (not allowed or url.scheme != "https" or url.port not in (None, 443)
            or url.username or url.password or url.fragment or len(endpoint) > 4096):
        raise ValueError("Nieobsługiwany serwer Web Push. Użyj Safari, Chrome lub Firefox.")
    keys = data.get("keys", {})
    try:
        p = keys["p256dh"]
        a = keys["auth"]
        raw_p = base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))
        raw_a = base64.urlsafe_b64decode(a + "=" * (-len(a) % 4))
        if len(raw_a) != 16 or len(raw_p) != 65:
            raise ValueError()
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw_p)
    except (KeyError, ValueError, TypeError):
        raise ValueError("Nieprawidłowy klucz powiadomień") from None
    return {"endpoint": endpoint, "keys": {"p256dh": p, "auth": a}}


def subscription_id(data):
    return hashlib.sha256(data["endpoint"].encode()).hexdigest()


class NoRedirectSession(requests.Session):
    def request(self, *args, **kwargs):
        kwargs["allow_redirects"] = False
        return super().request(*args, **kwargs)


class Push:
    def __init__(self, store, directory, origin):
        self.store, self.origin = store, origin
        self.key_path = directory / "vapid.pem"
        if not self.key_path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            with os.fdopen(os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as f:
                f.write(data)
        key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_key = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.task = None
        self.last_result = None

    def payload(self, title, body, tag):
        return {"title": title, "body": body, "tag": tag, "url": "/"}

    def send_event(self, title, body, tag):
        self.store.enqueue(self.payload(title, body, tag))

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    def _deliver(self, subscription, payload, ttl):
        try:
            with NoRedirectSession() as transport:
                transport.trust_env = False
                result = webpush(subscription_info=subscription, data=payload,
                                 vapid_private_key=str(self.key_path), vapid_claims={"sub": self.origin},
                                 ttl=ttl, timeout=12, requests_session=transport)
                return result.status_code
        except WebPushException as e:
            return e.response.status_code if e.response is not None else 0
        except Exception:
            return 0

    async def flush(self):
        now = time.time()
        with self.store.db:
            expired = self.store.db.execute("DELETE FROM outbox WHERE owner=? AND expires<?", (self.store.owner, now)).rowcount
        if expired:
            self.store.event("push_error", "Nie dostarczono powiadomienia przed końcem jego ważności.")
        rows = self.store.db.execute("SELECT * FROM outbox WHERE owner=? AND due<=? ORDER BY id LIMIT 10", (self.store.owner, now)).fetchall()
        subs = dict(self.store.subscriptions())
        for row in rows:
            sub = subs.get(row["sub_id"])
            code = await asyncio.to_thread(self._deliver, sub, row["payload"], max(1, int(row["expires"]-time.time()))) if sub else 410
            if code in {404, 410}:
                self.store.unsubscribe(row["sub_id"])
            with self.store.db:
                if 200 <= code < 300 or code in {404, 410}:
                    self.store.db.execute("DELETE FROM outbox WHERE id=? AND owner=?", (row["id"], self.store.owner))
                else:
                    delay = min(60 * 3 ** min(row["tries"], 3), 600)
                    self.store.db.execute("UPDATE outbox SET due=?,tries=tries+1 WHERE id=? AND owner=?", (time.time()+delay, row["id"], self.store.owner))
            self.last_result = {"at": time.time(), "accepted": 200 <= code < 300, "status": code}
            if 200 <= code < 300:
                self.store.event("push", "Serwer powiadomień przyjął wiadomość. Dostarczenie zależy od telefonu.")
            elif row["tries"] == 0:
                self.store.event("push_error", f"Wysyłka powiadomienia nie powiodła się (HTTP {code or 'brak odpowiedzi'}).")

    async def run(self):
        while True:
            try:
                await self.flush()
            except Exception:
                self.last_result = {"at": time.time(), "accepted": False, "status": 0}
            await asyncio.sleep(5)
