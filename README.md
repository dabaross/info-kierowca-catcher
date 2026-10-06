# Info-Kierowca — prototyp logowania z iPhone'a

Cel: użytkownik potwierdza w mObywatelu żądanie rozpoczęte przez Chromium
na serwerze. Następnie serwer sprawdza sesję Info-Kierowca przez odczyt API.

**To prototyp do weryfikacji, nie potwierdzona integracja.** Nie wykonano jeszcze
testu z prawdziwym iPhonem i kontem Info-Kierowca. Sukcesem jest dopiero wynik
`VERIFIED` po rzeczywistym potwierdzeniu w aplikacji. Dostarczenie kodu nie dowodzi,
że mobilny mechanizm uwierzytelniania pozwala na taki podział urządzeń.

## Co jest zaimplementowane

- FastAPI, Playwright/Chromium, HTTPX i responsywny panel HTML/CSS/JS.
- Jeden właściciel, jedna równoległa próba logowania, chroniony panel.
- Uruchomienie oficjalnego logowania przez login.gov.pl i wybór mObywatela.
- Próba wykrycia rzeczywistego odnośnika do aplikacji w stronie rządowej lub
  zdarzeniu nawigacji Chromium; przycisk otwarcia tego odnośnika na iPhonie.
- Sprawdzenie odpowiedzi API profilu w kontekście przeglądarki i przez HTTPX
  po przeniesieniu cookies należących do Info-Kierowca. Treść profilu nie trafia do UI.
- Anulowanie próby, usunięcie lokalnej sesji, czas próby ograniczony do około 5 minut
  od przygotowania strony rządowej. To limit aplikacji, a nie obietnica ważności żądania.
- Cookies HTTP w pamięci procesu; przeglądarka używa tymczasowego kontekstu.
  Playwright może tworzyć tymczasowe pliki profilu w systemie. Brak eksportu sesji na dysk.

Nie ma jeszcze monitorowania terminów, powiadomień, odświeżania JWT,
automatycznej rezerwacji ani instalowalnej PWA. Panel nie wymaga Reacta ani bazy danych
na tym etapie. Przyszły monitor może korzystać z potwierdzonego klienta HTTP.

## Najważniejsze niewiadome

1. Filtr `HANDOFF_SCHEMES=mobywatel` jest hipotezą techniczną do testu, a nie
   potwierdzonym formatem integracji. Kod nie konstruuje linku z tokenu.
   Jeżeli oficjalna strona używa innego schematu, trzeba go zidentyfikować i dopiero
   wtedy zmienić filtr. HTTPS Universal Links nie są obecnie obsługiwane.
2. Chromium z emulacją iPhone'a nie jest Safari. Strona może wybrać inne zachowanie,
   wymagać gestu użytkownika, nowego okna lub powrotu przez lokalną przeglądarkę.
   Obecny detektor może nie wykryć takiego wariantu; wymaga to dalszej pracy.
3. Oficjalny link może być związany z przeglądarką/urządzeniem. Jeśli potwierdzenie
   na telefonie nie kończy sesji na VPS, nie wolno zgłaszać sukcesu.
4. Walidacja profilu zakłada endpoint i strukturę zaobserwowane w repozytoriach.
   Test na rzeczywistym koncie musi sprawdzić, czy są nadal aktualne. Kod wykonuje
   dodatkowo odczyt bez cookies: bez odmowy dostępu lub przekierowania nie uznaje
   sesji za potwierdzoną. Lista pusta jest dopuszczona jako konto bez profili.

## Uruchomienie lokalne — Python 3.12

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
python3 setup_local.py
.venv/bin/python run.py
```

Instalacja systemowych zależności Chromium może wymagać uprawnień administratora.
Otwórz `http://localhost:8000`. Login to `owner`, hasło znajduje się w lokalnym `.env`.
`setup_local.py` nie nadpisuje istniejącej konfiguracji. Nie udostępniaj `.env`.
Do diagnozy na komputerze z pulpitem można ustawić `HEADED=1`.

Localhost sprawdza panel na tym samym komputerze. Do testu iPhone → serwer
potrzebujesz adresu HTTPS osiągalnego przez telefon, np. VPS z domeną.
Nie używaj adresu localhost w telefonie do połączenia z komputerem.

## VPS — Docker Compose i HTTPS

Praktyczny punkt startowy: Linux, 2 vCPU, 2 GB RAM, Docker z Compose, domena
ze wskazaniem DNS na VPS, porty 80/443 dostępne dla Caddy. To szacunek dla jednego
procesu Chromium, nie zmierzony wymóg. Sprawdź zużycie pamięci na swoim VPS.

```bash
python3 setup_local.py
```

W `.env` pozostaw wygenerowane hasło i ustaw:

```dotenv
PUBLIC_ORIGIN=https://login.twoja-domena.pl
DOMAIN=login.twoja-domena.pl
HANDOFF_SCHEMES=mobywatel
HEADED=0
```

Następnie:

```bash
docker compose --profile public up --build -d
```

Caddy zapewnia HTTPS; aplikacja ma jeden proces i jest wystawiona lokalnie na 8000.
Nie zwiększaj liczby workerów ani replik — stan tego prototypu jest w pamięci.
Restart usuwa sesję. Panel wykorzystuje HTTP Basic przez HTTPS, a operacje zapisu
sprawdzają Origin i dodatkowy nagłówek. To zabezpieczenie prywatnego PoC, nie system
kont dla przyszłej publicznej aplikacji. Nie włączaj logowania nagłówków ani pełnych
odpowiedzi zawierających link do logowania. Nie udostępniaj CDP/VNC publicznie.

Kontener i konfiguracja VPS są przygotowane, ale nie zostały uruchomione w środowisku
autora tego prototypu. Obraz Chromium wymaga sieci do pobrania zależności.

## Test akceptacyjny z iPhonem

1. Otwórz panel po HTTPS w Safari, zaloguj się jako `owner`.
2. Wybierz „Połącz z Info-Kierowca”. To uruchamia przeglądarkę na serwerze.
3. Jeśli pojawi się „Otwórz mObywatela”, dotknij go, potwierdź żądanie w aplikacji
   i wróć do panelu. Nigdy nie przekazuj nam PIN-u do mObywatela ani danych logowania.
4. Oczekiwany wynik: dwa potwierdzone odczyty profilu i `VERIFIED`.
   Zweryfikuj także negatywną próbę bez potwierdzenia — nie może zakończyć się sukcesem.
5. Jeżeli nie pojawi się przycisk lub sesja nie zadziała, zapisz wyłącznie stan,
   etap i kod HTTP z sekcji diagnostycznej. Nie wysyłaj pełnych linków, cookies ani tokenów.
6. Po teście zakończ próbę, aby usunąć lokalny klient sesji.

`BROWSER_ONLY` oznacza udany odczyt w przeglądarce serwera, lecz nieudany eksport
do HTTPX. `UNVERIFIED` oznacza niepotwierdzony dostęp do API. `VERIFIED` to wynik
konkretnego odczytu, nie bieżący pomiar ważności sesji. Po próbie przeglądarka jest
zamykana. Nie wdrożono jeszcze odnawiania ani podmiany aktywnej sesji monitora.

## Testy automatyczne

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Testy nie kontaktują się z portalem. Sprawdzają filtrowanie odnośników,
separację cookies, walidację odpowiedzi, dostęp do panelu i ochronę operacji zapisu.
Nie zastępują testu na telefonie.

## Kolejny etap po potwierdzeniu logowania

- Odczyt ośrodków, profilu/kategorii i terminów z API.
- Filtry: wybrany ośrodek, zakres dat, wybrane dni tygodnia i godziny w Europe/Warsaw.
- Ostrożna częstotliwość odpytywania, backoff po 429, brak powielania alertów.
- Jeden wybrany kanał powiadomień; zgoda na powiadomienia i obsługa niedostarczenia.
- Wykrywanie utraty sesji, komunikat o ponownym potwierdzeniu i podmiana sesji
  bez utraty konfiguracji monitorowania. Dokładny czas sesji ustalamy z obserwacji.
- Oddzielenie sesji, konfiguracji i subskrypcji według użytkownika przed rozbudową.

## Referencje techniczne

- https://github.com/losipiuk/examcatch — przykład logowania i odczytu profilu API.
- https://github.com/WioN780/auto-book-info-kierowca — odnawianie sesji i pozostałe API.
- https://github.com/FilipNowakowicz/info-kierowca-notifier — inspiracja monitorem.
- https://playwright.dev/python/docs/emulation
- https://playwright.dev/python/docs/api/class-cdpsession

Repozytoria są materiałem porównawczym. Ten kod jest niezależnym prototypem,
nie przeniesioną aplikacją ExamCatch. Format logowania i zachowanie portalu
muszą zostać zweryfikowane na żywo.
