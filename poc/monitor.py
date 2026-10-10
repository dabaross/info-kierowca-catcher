import asyncio
import time
from contextlib import suppress
from datetime import datetime

from .models import (
    PORD_GDANSK_ID, now_local, one_center_calendar_dates,
    parse_one_center_schedule,
)
from .session import PortalError, REFRESH_INTERVAL_SECONDS, SCHEDULE_PATH

FAILURE_BACKOFF_SECONDS = 360
MAX_FAILURE_BACKOFF_SECONDS = 3600

MESSAGES = {
    "NEEDS_LOGIN": "Potwierdź logowanie w mObywatelu, aby wznowić monitoring.",
    "NEEDS_PROFILE": "Wybierz profil PKK kategorii B dostępny na aktualnie zalogowanym koncie.",
    "NEEDS_CENTER": "Ta wersja monitoruje wyłącznie PORD Gdańsk. Wybierz Gdańsk i zapisz konfigurację.",
    "NETWORK": "Info-Kierowca nie odpowiada. Spróbujemy ponownie.",
    "RATE_LIMITED": "Limit zapytań Info-Kierowca. Czekamy przed kolejną próbą.",
    "UPSTREAM": "Portal odrzucił odczyt terminarza. Sprawdź kod HTTP w diagnostyce.",
    "HTTP_400": "Portal odrzucił żądanie terminarza (HTTP 400). Automatyczne próby wstrzymano. Zapisz poprawioną konfigurację albo świadomie uruchom monitor ponownie. Treść odpowiedzi nie jest zapisywana; do dalszej diagnozy potrzebny jest bezpieczny kod/nazwa błędu walidacji.",
    "SCHEMA": "Portal zwrócił nieznany format terminarza. Potrzebna aktualizacja adaptera.",
}


class Monitor:
    def __init__(self, store, sessions, push):
        self.store, self.sessions, self.push = store, sessions, push
        self.config, self.enabled = store.settings()
        self.revision = 0
        self.state = "NEEDS_CENTER" if self.config.center_id != PORD_GDANSK_ID else ("NEEDS_LOGIN" if self.enabled else "PAUSED")
        self.message = MESSAGES["NEEDS_CENTER"] if self.config.center_id != PORD_GDANSK_ID else ("Monitoring czeka na zalogowanie." if self.enabled else "Ustaw filtry i uruchom monitoring.")
        timing = store.schedule_timing()
        self.last_check = timing["last_success"] if timing else None
        self.next_check = timing["next_check"] if timing else 0
        self.error_status = None
        self.diagnostic = None
        blocked = store.schedule_block()
        if blocked and self.config.center_id == PORD_GDANSK_ID:
            self.state = "HTTP_400"
            self.message = MESSAGES["HTTP_400"]
            self.error_status = blocked["status"]
            self.diagnostic = blocked["diagnostic"] or None
        elif self.enabled and self.config.center_id == PORD_GDANSK_ID and timing is None:
            self.next_check = time.time() + self.config.interval_seconds
            store.save_schedule_timing(None, self.next_check)
        self.calendar = self.store.schedule_calendar(
            f"{self.config.profile_id}:one-center"
        )
        self.task = None
        self.expiry_notified = False
        self.generation = sessions.generation
        self.failures = 0

    def save(self, config):
        self.config = config
        self.revision += 1
        self.calendar = self.store.schedule_calendar(
            f"{config.profile_id}:one-center"
        )
        block = self.store.schedule_block()
        self.store.clear_schedule_block()
        self.error_status = self.diagnostic = None
        if block or self.config.center_id != PORD_GDANSK_ID:
            self.next_check = 0
        elif self.last_check is not None:
            self.next_check = max(self.next_check, self.last_check + config.interval_seconds)
        if self.config.center_id != PORD_GDANSK_ID:
            self.state, self.message = "NEEDS_CENTER", MESSAGES["NEEDS_CENTER"]
        elif self.enabled:
            self.state, self.message = "WAITING", "Konfiguracja zapisana. Oczekujemy na odczyt."
        else:
            self.state, self.message = "PAUSED", "Ustawienia zapisane. Uruchom monitoring."
        self.store.save_schedule_timing(self.last_check, self.next_check)
        self.store.save_settings(config, self.enabled)
        self.store.event("config", "Zapisano filtry monitorowania.")

    def toggle(self, enabled):
        self.enabled = enabled
        self.revision += 1
        self.store.save_settings(self.config, enabled)
        if enabled:
            self.store.clear_schedule_block()
            self.error_status = self.diagnostic = None
            self.failures = 0
            self.next_check = 0
        if self.config.center_id != PORD_GDANSK_ID:
            self.state, self.message = "NEEDS_CENTER", MESSAGES["NEEDS_CENTER"]
        else:
            self.state = "WAITING" if enabled else "PAUSED"
            self.message = "Monitoring uruchomiony." if enabled else "Monitoring wstrzymany."
        self.store.save_schedule_timing(self.last_check, self.next_check)
        self.store.event("monitor", self.message)

    def public(self):
        now = now_local()
        slots = sorted(
            (
                slot for slot in (self.calendar or {}).get("slots", [])
                if self.config.matches(slot, now)
            ),
            key=lambda slot: slot["start"],
        )
        return {"enabled": self.enabled, "state": self.state, "message": self.message,
                "next_check": 0 if self.state == "HTTP_400" else max(self.next_check, self.store.cooldown(SCHEDULE_PATH)),
                "last_check": self.last_check, "http_status": self.error_status,
                "diagnostic": self.diagnostic,
                "slots": slots, "calendar": self.calendar,
                "config": self.config.model_dump(mode="json"),
                "request_policy": {
                    "schedule_interval_seconds": self.config.interval_seconds,
                    "jwt_refresh_min_interval_seconds": REFRESH_INTERVAL_SECONDS,
                    "schedule_error_backoff_initial_seconds": FAILURE_BACKOFF_SECONDS,
                    "schedule_error_backoff_max_seconds": MAX_FAILURE_BACKOFF_SECONDS,
                    "rate_limit_headers": ["Retry-After", "X-RateLimit-Reset"],
                    "profile_check": "Przy logowaniu (weryfikacja przeglądarki i klienta oraz kontrola anonimowa) i po odpowiedzi refresh innej niż 200/204.",
                }}

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    def login_needed(self):
        self.state = "NEEDS_LOGIN"
        self.message = MESSAGES[self.state]
        if not self.expiry_notified:
            self.expiry_notified = True
            self.push.send_event("Odśwież sesję", "Monitoring czeka na potwierdzenie logowania. Otwórz aplikację.", "session")
            self.store.event("session", self.message)

    async def tick(self):
        if self.generation != self.sessions.generation:
            self.generation = self.sessions.generation
            self.expiry_notified = False
            self.store.event("session", "Nowa sesja gotowa. Zachowano ustawienia monitorowania.")
        if not self.enabled:
            return
        if self.config.center_id != PORD_GDANSK_ID:
            self.state, self.message = "NEEDS_CENTER", MESSAGES["NEEDS_CENTER"]
            return
        blocked = self.store.schedule_block()
        if blocked:
            self.state, self.message = "HTTP_400", MESSAGES["HTTP_400"]
            self.error_status = blocked["status"]
            self.diagnostic = blocked["diagnostic"] or None
            return
        if not self.sessions.client:
            self.login_needed()
            return
        if self.config.profile_id not in self.sessions.profiles:
            self.state, self.message = "NEEDS_PROFILE", MESSAGES["NEEDS_PROFILE"]
            return
        if self.sessions.profiles[self.config.profile_id]["category"] != "B":
            self.state, self.message = "NEEDS_PROFILE", MESSAGES["NEEDS_PROFILE"]
            return
        if not self.sessions.warning_sent and time.time() - self.sessions.connected_at >= 50*60:
            self.sessions.warning_sent = True
            self.push.send_event("Sesja może niedługo wygasnąć", "Od logowania minęło 50 minut. Możesz potwierdzić nową sesję.", "session")
        start = self.config.start_date(now_local().date())
        if start is None:
            self.toggle(False)
            self.state, self.message = "FINISHED", "Wybrany zakres dat już minął. Ustaw nowy zakres."
            return
        due = max(self.next_check, self.store.cooldown(SCHEDULE_PATH))
        if time.time() < due:
            if self.state in {"NEEDS_LOGIN", "NEEDS_PROFILE", "WAITING"}:
                self.state, self.message = "WAITING", "Czekamy na zaplanowany odczyt."
            return
        config, revision = self.config, self.revision
        self.state, self.message = "CHECKING", "Sprawdzamy wolne terminy…"
        self.next_check = time.time() + config.interval_seconds
        self.store.save_schedule_timing(self.last_check, self.next_check)
        try:
            raw = await self.sessions.schedule(config.profile_id, config.center_id, start)
            try:
                category = self.sessions.profiles[config.profile_id]["category"]
                slots = parse_one_center_schedule(raw, category)
                calendar_dates = one_center_calendar_dates(raw)
            except (ValueError, TypeError, KeyError, OverflowError):
                raise PortalError("SCHEMA") from None
            if revision != self.revision or not self.enabled:
                return
            calendar_slots = slots
            matches = [
                s for s in calendar_slots
                if config.matches(s, now_local())
            ]
            at = time.time()
            for s in calendar_slots:
                s["checked_at"] = at
            calendar = {
                "requested_start": start.isoformat(),
                "calendar_dates": [day.isoformat() for day in calendar_dates],
                "at": at,
                "slots": calendar_slots,
            }
            snapshot_key = f"{self.config.profile_id}:one-center"
            self.store.save_schedule_calendar(
                snapshot_key, start.isoformat(),
                calendar["calendar_dates"], calendar_slots, at,
            )
            self.calendar = calendar
            self.last_check, self.error_status = at, None
            self.diagnostic = None
            self.failures = 0
            self.state = "WATCHING"
            returned_days = len(calendar_dates)
            self.message = (
                f"Odczyt zakończony: {len(matches)} pasujących terminów; "
                f"portal zwrócił {returned_days} dni kalendarza. "
                "Pełny zakres filtrów nie jest potwierdzony."
            )
            def payload(new):
                ordered = sorted(new, key=lambda slot: slot["start"])
                times = [
                    datetime.fromisoformat(slot["start"]).strftime("%d.%m %H:%M")
                    for slot in ordered[:3]
                ]
                remaining = len(ordered) - len(times)
                suffix = f" i +{remaining}" if remaining else ""
                body = (
                    f"PORD Gdańsk · {len(ordered)} nowych terminów. Najbliższe: "
                    f"{', '.join(times)}{suffix}. Sprawdź portal."
                )
                return self.push.payload("Nowe terminy praktyczne", body, "slots")
            new = self.store.record_matches(config.profile_id, matches, payload)
            self.store.event("match" if new else "check", f"Kalendarz od {start}: {len(matches)} pasujących, {len(new)} nowych.")
            self.next_check = at + config.interval_seconds
            self.store.save_schedule_timing(at, self.next_check)
        except PortalError as exc:
            if revision != self.revision:
                return
            self.state, self.message, self.error_status = exc.code, MESSAGES[exc.code], exc.status
            self.diagnostic = exc.diagnostic
            if exc.diagnostic:
                self.message += f" Kod walidacji portalu: {exc.diagnostic}."
            self.failures += 1
            if exc.code == "HTTP_400":
                self.store.block_schedule(exc.status, exc.diagnostic)
                self.next_check = 0
            else:
                backoff = min(FAILURE_BACKOFF_SECONDS*2**min(self.failures-1,3),
                              MAX_FAILURE_BACKOFF_SECONDS)
                self.next_check = max(time.time()+backoff, exc.until)
            self.store.save_schedule_timing(self.last_check, self.next_check)
            if exc.code == "NEEDS_LOGIN":
                self.login_needed()
            else:
                self.store.event("error", self.message + (f" HTTP {exc.status}." if exc.status else ""))

    async def run(self):
        while True:
            try:
                await self.tick()
                if self.sessions.client:
                    try:
                        await self.sessions.maintain()
                    except PortalError as exc:
                        if exc.code == "NEEDS_LOGIN" and self.enabled:
                            self.login_needed()
            except Exception as exc:
                self.state, self.message = "ERROR", "Błąd monitora. Sprawdź historię zdarzeń."
                self.store.event("error", f"Błąd wewnętrzny: {type(exc).__name__}.")
                await asyncio.sleep(30)
            await asyncio.sleep(2)
