from __future__ import annotations

import asyncio
import re
import secrets
import time
from contextlib import suppress
from datetime import datetime, timezone

import httpx
from playwright.async_api import async_playwright, Error as BrowserError

from .core import (
    AUTH_HOST, INFO_ORIGIN, PROFILE_PATH, Attempt, accepted_handoff,
    host, portal_cookies, profile_response_valid,
)


class LoginController:
    """One owner, one login at a time; ephemeral browser, HTTP cookies in RAM."""

    def __init__(self, schemes: set[str], headed: bool = False, sessions=None):
        self.sessions = sessions
        self.schemes = schemes
        self.headed = headed
        self.attempt: Attempt | None = None
        self.task: asyncio.Task | None = None
        self.client: httpx.AsyncClient | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> Attempt:
        async with self.lock:
            if self.task and not self.task.done():
                return self.attempt
            if not self.sessions:
                await self._clear_client()
            attempt = Attempt(id=secrets.token_urlsafe(18))
            self.attempt = attempt
            self.task = asyncio.create_task(self._run(attempt))
            return attempt

    async def cancel(self) -> None:
        async with self.lock:
            if self.task and not self.task.done():
                self.task.cancel()
                with suppress(asyncio.CancelledError):
                    await self.task
            await self._clear_client()
            if self.attempt:
                self.attempt.state = "STOPPED"
                self.attempt.message = "Próba logowania zakończona."
                self.attempt.handoff_url = None
                self.attempt.finished = True

    async def _clear_client(self):
        if self.client:
            await self.client.aclose()
            self.client = None

    def _observe(self, attempt: Attempt, url: str, source: str, method: str):
        if attempt.finished or attempt.handoff_url:
            return
        if accepted_handoff(url, source, self.schemes):
            attempt.handoff_url = url
            attempt.state = "WAITING_FOR_CONFIRMATION"
            attempt.message = "Otwórz mObywatela i potwierdź logowanie, następnie wróć tutaj."
            attempt.diagnostics["handoff_source"] = method

    async def _run(self, attempt: Attempt):
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=not self.headed)
                try:
                    device = dict(pw.devices["iPhone 13"])
                    device.pop("default_browser_type", None)
                    context = await browser.new_context(**device, locale="pl-PL", timezone_id="Europe/Warsaw")

                    # The prototype cannot submit reservations or start payments.
                    async def guard(route):
                        path = route.request.url.split("?", 1)[0].lower()
                        if any(part in path for part in (
                            "/reservations/create", "/reservations/confirm",
                            "/reservations/reschedule", "/reservations/cancel", "/payments/init",
                        )):
                            await route.abort()
                        else:
                            await route.continue_()
                    await context.route("**/*", guard)
                    page = await context.new_page()
                    page.set_default_timeout(12000)
                    cdp = await context.new_cdp_session(page)
                    await cdp.send("Page.enable")
                    # A custom-scheme redirect may not become a Playwright HTTP request.
                    cdp.on("Page.frameRequestedNavigation", lambda event: self._observe(
                        attempt, event.get("url", ""), page.url, "official_page_navigation",
                    ))
                    attempt.state = "OPENING_LOGIN"
                    attempt.message = "Otwieramy oficjalny proces logowania."
                    await page.goto(INFO_ORIGIN + "/login", wait_until="domcontentloaded", timeout=45000)
                    await self._follow_official_login(page)
                    attempt.diagnostics["stage_host"] = host(page.url)
                    attempt.state = "WAITING_FOR_CONFIRMATION" if attempt.handoff_url else "FINDING_APP_LINK"
                    if not attempt.handoff_url:
                        attempt.message = "Szukamy odnośnika do mObywatela w oficjalnej stronie logowania."
                    deadline = time.monotonic() + 300
                    while time.monotonic() < deadline:
                        current_host = host(page.url)
                        attempt.diagnostics["stage_host"] = current_host
                        if current_host == "info-kierowca.pl" and "/login" not in page.url:
                            attempt.state = "VERIFYING"
                            attempt.message = "Sprawdzamy dostęp do profilu z serwera."
                            await self._verify(attempt, context)
                            return
                        if current_host == AUTH_HOST:
                            # Never derive a new URI from the token; use actual page links only.
                            # Confirmation may redirect this page while Playwright evaluates the DOM.
                            # That transient condition is not an authentication failure.
                            urls = []
                            with suppress(BrowserError):
                                urls = await page.locator("a[href]").evaluate_all("els => els.map(e => e.href)")
                            for url in urls:
                                self._observe(attempt, url, page.url, "official_page_anchor")
                            if not attempt.handoff_url:
                                attempt.message = (
                                    "Strona rządowa jest otwarta, ale nie wykryto obsługiwanego linku do aplikacji. "
                                    "Ten wariant wymaga dalszego rozpoznania."
                                )
                        await asyncio.sleep(1)
                    attempt.state = "EXPIRED"
                    attempt.message = "Minął czas próby. Uruchom logowanie ponownie."
                finally:
                    await browser.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt.state = "ERROR"
            # Browser exceptions can include authentication URLs. Do not log their body.
            attempt.message = "Nie udało się ukończyć próby. Sprawdź etap i uruchom ją ponownie."
            attempt.diagnostics["error_type"] = type(exc).__name__
        finally:
            attempt.handoff_url = None
            attempt.finished = True

    async def _follow_official_login(self, page):
        for _ in range(25):
            current = host(page.url)
            if current == AUTH_HOST:
                return
            if current == "info-kierowca.pl":
                if "/login" not in page.url:
                    return
                with suppress(BrowserError):
                    await page.get_by_text("ODRZUĆ WSZYSTKIE", exact=True).first.click(timeout=1200)
                await page.get_by_text("login.gov.pl", exact=False).first.click(timeout=12000)
            elif current == "login.gov.pl":
                name = re.compile("Aplikacja mObywatel", re.I)
                choice = page.get_by_role("link", name=name).or_(page.get_by_role("button", name=name))
                await choice.first.click(timeout=12000)
            else:
                # SAML redirects may briefly visit another official page; never fill credentials.
                await asyncio.sleep(1)
                continue
            await asyncio.sleep(1)
        raise RuntimeError("official_login_stage_not_reached")

    async def _verify(self, attempt: Attempt, context):
        response = await context.request.get(INFO_ORIGIN + PROFILE_PATH, timeout=15000, max_redirects=0)
        attempt.diagnostics["browser_profile_status"] = response.status
        try:
            valid = response.status == 200 and profile_response_valid(await response.json())
        except ValueError:
            valid = False
        if not valid:
            attempt.state = "UNVERIFIED"
            attempt.message = "Przekierowanie nastąpiło, ale API nie potwierdziło zalogowanej sesji."
            return
        attempt.browser_verified = True
        cookies = portal_cookies(await context.cookies())
        user_agent = await context.pages[0].evaluate("navigator.userAgent")
        # Negative control: a public/empty response must not count as authentication.
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=15,
                                         headers={"User-Agent": user_agent}) as anonymous:
                control = await anonymous.get(INFO_ORIGIN + PROFILE_PATH)
            attempt.diagnostics["anonymous_profile_status"] = control.status_code
            if control.status_code not in {401, 403, 302, 303, 307, 308}:
                attempt.state = "UNVERIFIED"
                attempt.message = "Odczyt profilu wymaga dodatkowego dowodu: próba bez cookies nie zwróciła jednoznacznej odmowy dostępu."
                return
        except httpx.HTTPError:
            attempt.state = "UNVERIFIED"
            attempt.message = "Nie udało się wykonać kontrolnego odczytu bez sesji. Powtórz próbę."
            return
        client = httpx.AsyncClient(
            cookies=cookies, follow_redirects=False, timeout=15,
            headers={"User-Agent": user_agent, "Accept": "application/json", "Referer": INFO_ORIGIN + "/"},
        )
        try:
            result = await client.get(INFO_ORIGIN + PROFILE_PATH)
            attempt.diagnostics["http_profile_status"] = result.status_code
            try:
                valid = result.status_code == 200 and profile_response_valid(result.json())
            except ValueError:
                valid = False
            if not valid:
                attempt.state = "BROWSER_ONLY"
                attempt.message = "Sesja działała w przeglądarce serwera; eksport do klienta HTTP wymaga poprawy."
                return
            attempt.http_verified = True
            attempt.verified_at = datetime.now(timezone.utc).isoformat()
            attempt.state = "VERIFIED"
            attempt.message = "Test udany: serwer odczytał profil także niezależnym klientem HTTP."
            if self.sessions:
                await self.sessions.install(client, result.json())
                attempt.message = "Sesja aktywna. Możesz monitorować terminy."
            else:
                self.client = client
            client = None
        except httpx.HTTPError:
            attempt.state = "BROWSER_ONLY"
            attempt.message = "Profil był dostępny w przeglądarce serwera; połączenie klienta HTTP nie powiodło się."
        finally:
            if client:
                await client.aclose()
