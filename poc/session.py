import asyncio
import hashlib
import hmac
import time
from contextlib import asynccontextmanager
from datetime import date
from email.utils import parsedate_to_datetime

import httpx

from .core import INFO_ORIGIN, PROFILE_PATH, profile_response_valid
from .models import CATEGORIES

SCHEDULE_PATH = "/bknd/exam/api/v1/Schedules/user/MultipleCentersExams"
REFRESH_PATH = "/bknd/auth/api/v1/jwt/refresh"
ALLOWED = {("GET", PROFILE_PATH), ("GET", REFRESH_PATH), ("POST", SCHEDULE_PATH)}


class PortalError(Exception):
    def __init__(self, code, status=None, until=0):
        super().__init__(code)
        self.code, self.status, self.until = code, status, until


def backoff_until(headers, now):
    deadlines = [now + 360]
    retry = headers.get("retry-after", "")
    try:
        deadlines.append(now + max(0, float(retry)))
    except ValueError:
        try:
            deadlines.append(parsedate_to_datetime(retry).timestamp())
        except (ValueError, TypeError, OverflowError):
            pass
    try:
        deadlines.append(float(headers.get("x-ratelimit-reset", 0)))
    except ValueError:
        pass
    return max(deadlines)


def build_schedule_payload(profile: dict, center_ids: list[int], start: date) -> dict:
    return {
        "startDate": start.isoformat(),
        "organizationId": center_ids,
        "category": CATEGORIES.index(profile["category"]),
        "profileNumber": profile["number"],
        "profileType": "Pkk",
    }


class Sessions:
    def __init__(self, store):
        self.store = store
        self.lock = asyncio.Lock()
        self.client = None
        self.profiles = {}
        self.generation = 0
        self.connected_at = None
        self.last_verified = None
        self.last_refresh = 0
        self.warning_sent = False
        self.note = "Zaloguj się do Info-Kierowca."

    async def install(self, client, rows):
        async with self.lock:
            old = self.client
            self.client = client
            self.profiles = {}
            for row in rows:
                number, category = row["pkkNumber"], row["categoryName"]
                if not isinstance(number, str) or not number.strip() or not isinstance(category, str):
                    continue
                number, category = number.strip(), category.upper()
                if category not in CATEGORIES:
                    continue
                id = hmac.new(self.store.identity_key, f"{number}:{category}".encode(), hashlib.sha256).hexdigest()
                self.profiles[id] = {"number": number, "category": category}
            self.generation += 1
            self.connected_at = self.last_verified = time.time()
            self.last_refresh = time.time()
            self.warning_sent = False
            self.note = "Sesja potwierdzona."
            if old:
                await old.aclose()

    def public(self):
        return {"active": self.client is not None, "generation": self.generation,
                "connected_at": self.connected_at, "last_verified": self.last_verified,
                "message": self.note, "profiles": [{"id": k, "category": v["category"], "label": f"Kategoria {v['category']} · PKK …{v['number'][-4:]}"} for k,v in self.profiles.items()]}

    async def close(self):
        async with self.lock:
            await self._expire()

    async def _expire(self):
        if self.client:
            await self.client.aclose()
        self.client = None
        self.profiles = {}
        self.note = "Potrzebne ponowne potwierdzenie w mObywatelu."

    async def _call(self, method, path, body=None):
        if (method, path) not in ALLOWED:
            raise ValueError("Endpoint not allowed")
        now = time.time()
        until = self.store.cooldown(path)
        if until > now:
            raise PortalError("RATE_LIMITED", until=until)
        # Persist even on failures/restarts, so UI toggles cannot bypass rate limits.
        self.store.defer(path, now + (360 if path == SCHEDULE_PATH else 60))
        try:
            response = await self.client.request(method, INFO_ORIGIN + path, json=body)
        except httpx.HTTPError:
            raise PortalError("NETWORK") from None
        if response.status_code == 429 or response.headers.get("x-ratelimit-remaining") == "0":
            self.store.defer(path, backoff_until(response.headers, now))
        if response.status_code == 429:
            raise PortalError("RATE_LIMITED", 429, self.store.cooldown(path))
        return response

    async def _probe(self):
        result = await self._call("GET", PROFILE_PATH)
        if result.status_code in {401, 403} or result.is_redirect:
            await self._expire()
            raise PortalError("NEEDS_LOGIN", result.status_code)
        if result.status_code != 200:
            raise PortalError("UPSTREAM", result.status_code)
        try:
            if not profile_response_valid(result.json()):
                raise ValueError()
        except ValueError:
            raise PortalError("SCHEMA", result.status_code) from None
        self.last_verified = time.time()

    async def _refresh(self):
        if time.time() - self.last_refresh < 480:
            return
        self.last_refresh = time.time()
        result = await self._call("GET", REFRESH_PATH)
        if result.status_code in {401, 403} or result.is_redirect:
            await self._expire()
            raise PortalError("NEEDS_LOGIN", result.status_code)
        if result.status_code not in {200, 204}:
            # A refused refresh does not prove an existing token stopped working.
            await self._probe()
            self.note = "Odnawianie tokenu nie powiodło się, ale sesja nadal odpowiada."

    async def maintain(self):
        async with self.lock:
            if self.client:
                await self._refresh()

    async def schedule(self, profile_id, center_id, start):
        async with self.lock:
            if not self.client:
                raise PortalError("NEEDS_LOGIN")
            if profile_id not in self.profiles:
                raise PortalError("NEEDS_PROFILE")
            await self._refresh()
            p = self.profiles[profile_id]
            body = build_schedule_payload(p, [center_id], start)
            result = await self._call("POST", SCHEDULE_PATH, body)
            if result.status_code in {401, 403} or result.is_redirect:
                await self._expire()
                raise PortalError("NEEDS_LOGIN", result.status_code)
            if result.status_code != 200:
                raise PortalError("UPSTREAM", result.status_code)
            try:
                data = result.json()
            except ValueError:
                raise PortalError("SCHEMA", result.status_code) from None
            self.last_verified = time.time()
            return data
