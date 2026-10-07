import asyncio
import time
from contextlib import suppress
from datetime import datetime

from .models import now_local, parse_schedule
from .session import PortalError, SCHEDULE_PATH

MESSAGES = {
    "NEEDS_LOGIN": "Potwierdź logowanie w mObywatelu, aby wznowić monitoring.",
    "NEEDS_PROFILE": "Wybierz profil PKK dostępny na aktualnie zalogowanym koncie.",
    "NETWORK": "Info-Kierowca nie odpowiada. Spróbujemy ponownie.",
    "RATE_LIMITED": "Limit zapytań Info-Kierowca. Czekamy przed kolejną próbą.",
    "UPSTREAM": "Portal odrzucił odczyt terminarza. Sprawdź kod HTTP w diagnostyce.",
    "SCHEMA": "Portal zwrócił nieznany format terminarza. Potrzebna aktualizacja adaptera.",
}


class Monitor:
    def __init__(self, store, sessions, push):
        self.store, self.sessions, self.push = store, sessions, push
        self.config, self.enabled = store.settings()
        self.revision = 0
        self.state = "NEEDS_LOGIN" if self.enabled else "PAUSED"
        self.message = "Monitoring czeka na zalogowanie." if self.enabled else "Ustaw filtry i uruchom monitoring."
        self.next_check = 0
        self.last_check = None
        self.error_status = None
        self.window_index = 0
        self.snapshots = {}
        self.task = None
        self.expiry_notified = False
        self.generation = sessions.generation
        self.failures = 0

    def save(self, config):
        self.config = config
        self.revision += 1
        self.window_index = 0
        self.snapshots = {}
        self.last_check = None
        self.store.save_settings(config, self.enabled)
        self.store.event("config", "Zapisano filtry monitorowania.")

    def toggle(self, enabled):
        self.enabled = enabled
        self.revision += 1
        self.store.save_settings(self.config, enabled)
        self.state = "WAITING" if enabled else "PAUSED"
        self.message = "Monitoring uruchomiony." if enabled else "Monitoring wstrzymany."
        self.store.event("monitor", self.message)

    def public(self):
        windows = self.config.windows(now_local().date())
        slots = sorted((s for snap in self.snapshots.values() for s in snap["slots"] if self.config.matches(s, now_local())), key=lambda s:s["start"])
        # Window starts don't overlap; keys protect against upstream duplicate entries.
        unique = list({s["key"]: s for s in slots}.values())
        return {"enabled": self.enabled, "state": self.state, "message": self.message,
                "next_check": max(self.next_check, self.store.cooldown(SCHEDULE_PATH)),
                "last_check": self.last_check, "http_status": self.error_status,
                "slots": unique, "windows_total": len(windows), "windows": list(self.snapshots.values()),
                "config": self.config.model_dump(mode="json")}

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
            self.snapshots = {}
            self.last_check = None
            self.window_index = 0
            self.store.event("session", "Nowa sesja gotowa. Zachowano ustawienia monitorowania.")
        if not self.enabled:
            return
        if not self.sessions.client:
            self.login_needed()
            return
        if self.config.profile_id not in self.sessions.profiles:
            self.state, self.message = "NEEDS_PROFILE", MESSAGES["NEEDS_PROFILE"]
            return
        if not self.sessions.warning_sent and time.time() - self.sessions.connected_at >= 50*60:
            self.sessions.warning_sent = True
            self.push.send_event("Sesja może niedługo wygasnąć", "Od logowania minęło 50 minut. Możesz potwierdzić nową sesję.", "session")
        windows = self.config.windows(now_local().date())
        if not windows:
            self.toggle(False)
            self.state, self.message = "FINISHED", "Wybrany zakres dat już minął. Ustaw nowy zakres."
            return
        due = max(self.next_check, self.store.cooldown(SCHEDULE_PATH))
        if time.time() < due:
            if self.state in {"NEEDS_LOGIN", "NEEDS_PROFILE", "WAITING"}:
                self.state, self.message = "WAITING", "Czekamy na zaplanowany odczyt."
            return
        config, revision = self.config, self.revision
        start, end = windows[self.window_index % len(windows)]
        self.state, self.message = "CHECKING", "Sprawdzamy wolne terminy…"
        try:
            raw = await self.sessions.schedule(config.profile_id, config.center_id, start)
            try:
                category = self.sessions.profiles[config.profile_id]["category"]
                slots = parse_schedule(raw, category)
            except (ValueError, TypeError, KeyError, OverflowError):
                raise PortalError("SCHEMA") from None
            if revision != self.revision or not self.enabled:
                return
            matches = [s for s in slots if config.matches(s, now_local()) and start <= datetime.fromisoformat(s["start"]).date() <= end]
            at = time.time()
            for s in matches:
                s["checked_at"] = at
            # Discard expired window snapshots when calendar date shifts.
            allowed_windows = {a.isoformat() for a,b in windows}
            self.snapshots = {k:v for k,v in self.snapshots.items() if k in allowed_windows}
            self.snapshots[start.isoformat()] = {"from": start.isoformat(), "to": end.isoformat(), "at": at, "slots": matches}
            self.last_check, self.error_status = at, None
            self.window_index = (self.window_index + 1) % len(windows)
            self.failures = 0
            self.state = "WATCHING"
            self.message = f"Odczyt zakończony. Pasujące terminy w sprawdzonym oknie: {len(matches)}."
            def payload(new):
                first = new[0]
                when = datetime.fromisoformat(first["start"]).strftime("%d.%m, %H:%M")
                return self.push.payload("Jest pasujący termin", f"{first['center_name']} · {when}. Nowe terminy: {len(new)}. Sprawdź dostępność w portalu.", "slots")
            new = self.store.record_matches(config.profile_id, matches, payload)
            self.store.event("match" if new else "check", f"{start} – {end}: {len(matches)} pasujących, {len(new)} nowych.")
            self.next_check = at + config.interval_seconds
        except PortalError as exc:
            if revision != self.revision:
                return
            self.state, self.message, self.error_status = exc.code, MESSAGES[exc.code], exc.status
            self.failures += 1
            self.next_check = max(time.time()+min(360*2**min(self.failures-1,3),3600), exc.until)
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
