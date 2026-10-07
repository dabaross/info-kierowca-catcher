# Łap termin — prywatny monitor egzaminów

Wersja 1.0: działający wcześniej proces logowania mObywatel na iPhonie → Chromium na VPS → klient HTTP, rozszerzony o monitoring egzaminów praktycznych, responsywny panel i Web Push. Brak automatycznej rezerwacji, zmiany rezerwacji, płatności oraz automatycznego potwierdzania tożsamości.

## Aktualizacja istniejącego VPS

Pobierz `info-kierowca-app.zip` do `~/Downloads` na komputerze. W terminalu komputera:

```bash
scp -i ~/Downloads/ssh-key-2026-10-06.key ~/Downloads/info-kierowca-app.zip ubuntu@130.61.102.33:/home/ubuntu/
ssh -i ~/Downloads/ssh-key-2026-10-06.key ubuntu@130.61.102.33
```

Następnie na Ubuntu:

```bash
cd /home/ubuntu
unzip -o info-kierowca-app.zip
cd info-kierowca-app
bash update.sh
```

Skrypt kopiuje konfigurację ze starego `/home/ubuntu/info-login-poc/.env`, jeżeli nowy folder jeszcze jej nie ma. Buduje obraz przed zmianą kontenerów i uruchamia ten sam projekt Compose `info-login-poc`, dzięki czemu zachowuje wolumeny Caddy. Stare źródła zostają w poprzednim folderze. Nie używaj `docker compose down -v`, bo usuwa dane.

Adres pozostaje **https://130-61-102-33.sslip.io**. Hasło pozostaje takie jak ustawione wcześniej. Teraz wpisuje się je w formularzu panelu, bez nazwy użytkownika. Otwórz stronę ponownie lub odśwież ją po aktualizacji. Trzeba ponownie potwierdzić sesję Info-Kierowca, ponieważ sesje portalu celowo żyją tylko w pamięci.

Jeżeli nie masz `unzip`: `sudo apt-get install -y unzip`.

### Pierwsze uruchomienie

1. Połącz konto przyciskiem „Zaloguj przez mObywatela”. Gdy pojawi się odnośnik, otwórz mObywatela, potwierdź i wróć do panelu. To odnośnik rzeczywiście przechwycony z oficjalnej strony logowania; aplikacja nie konstruuje własnego żądania uwierzytelnienia.
2. Po potwierdzeniu wybierz profil PKK i ośrodek egzaminacyjny.
3. Dodaj przedziały dat, godzin i dni tygodnia. Przykład: pon.–pt. 07:00–09:00 oraz pon.–pt. 16:00–18:00. Mogą mieć różne daty. Zapisz i uruchom monitoring.
4. Na iPhonie w Safari: Udostępnij → Dodaj do ekranu początkowego. Otwórz aplikację z ikony, zaloguj się, włącz powiadomienia i wyślij test. Web Push wymaga iOS 16.4+ i aplikacji dodanej do ekranu początkowego. Zwykła karta Safari nadal pozwala obsługiwać panel i logowanie.
5. Sprawdź pierwszy rzeczywisty odczyt terminarza i test powiadomienia na telefonie. Nie trzeba trzymać panelu otwartego, ale sesję należy okresowo odnawiać.

## Zasady dopasowania i odczytu

- Jeden ośrodek i jeden profil PKK naraz; maksymalnie 10 przedziałów. Całość mieści się w okresie 60 dni.
- Termin spełnia **dowolny jeden** kompletny przedział: data ORAZ dzień tygodnia ORAZ godzina. Granice włącznie. Godziny w `Europe/Warsaw`, z obsługą zmiany czasu. Zakres przez północ trzeba rozdzielić na dwa przedziały.
- Tylko przyszłe egzaminy praktyczne z wolnymi miejscami. Profil i kategoria pochodzą z zalogowanego konta. Lista ośrodków jest lokalnym katalogiem referencyjnym i może wymagać aktualizacji.
- Nakładające się przedziały nie dublują wyników ani powiadomień. Termin identyfikowany jest przez ośrodek i identyfikator egzaminu. Ten sam termin dla tego samego profilu nie wywołuje ponownego alertu przez 60 dni, także po restarcie lub zmianie filtrów.
- Domyślnie jeden odczyt co 6 minut. To zachowawcze ustawienie aplikacji, nie gwarantowany oficjalny limit. Nie pozwalamy zejść poniżej 6 minut. Odpowiedź 429 i nagłówki limitu przedłużają oczekiwanie. Limity są zapisane w bazie i przeżywają restart oraz klikanie start/stop.
- Odczyt API obejmuje okna do 20 dni, sprawdzane kolejno. Przy 3 oknach i odstępie 6 minut pełny cykl trwa około 18 minut. Przerwy między przedziałami mogą być objęte odczytem, lecz są odfiltrowane z wyników.
- Każdy wynik i okno pokazują czas ostatniego odczytu. Brak odczytu lub błąd nie oznaczają braku terminów. Wyniki są migawką, bez gwarancji dostępności w momencie rezerwacji.
- Lista profili i odczyt terminarza używają ścieżek przechwyconych z oficjalnego panelu. POST `MultipleCentersExams` wysyła datę początkową, wybrany ośrodek, indeks kategorii, numer PKK i typ profilu `Pkk`. Odpowiedź wieloośrodkowa jest filtrowana do praktycznych terminów z wolnymi miejscami i kategorią zgodną z profilem. Aplikacja nie rezerwuje terminów automatycznie.
- Przed wygaśnięciem sesji pokazujemy czas od logowania i wysyłamy przypomnienie po 50 minutach. Nie udajemy znajomości dokładnego czasu wygaśnięcia. Odświeżanie JWT nie gwarantuje przedłużenia całej sesji.

## Sesja i powiadomienia

Nowe logowanie nie usuwa poprzedniej sesji. Dopiero po zweryfikowanym odczycie profilu klient HTTP jest podmieniany pod blokadą. Trwający odczyt może się zakończyć. Nieudana/anulowana próba nie wyłącza działającego klienta. Filtry oraz preferencja start/stop zostają zachowane. Utrata sesji wstrzymuje odczyty; kolejne poprawne logowanie pozwala wrócić do pracy z zachowaniem limitów API.

Restart aplikacji wymaga logowania do portalu od nowa. Cookies i pełne numery PKK są tylko w RAM. Panel widzi maskę PKK i niejawny identyfikator HMAC. Baza zawiera ustawienia, historię, zaszyfrowane subskrypcje push, kolejkę wiadomości, limity i skróty sesji panelu.

Web Push: trwały klucz VAPID, maks. 10 urządzeń, powiadomienia o nowych terminach i potrzebie logowania. Kolejka ponawia błędy wysyłki; wiadomości wygasają po 15 minutach. Odpowiedzi 404/410 usuwają nieaktualną subskrypcję. „Przyjęta przez serwer push” nie dowodzi wyświetlenia na telefonie — potwierdź test samodzielnie. Powiadomienie nie zawiera PKK ani danych osobowych, ale podaje ośrodek i termin; może być widoczne na ekranie blokady. Wylogowanie z panelu nie wyłącza monitora ani wcześniej włączonych powiadomień. Służą do tego osobne przyciski.

## Stos i infrastruktura

Python 3.12, FastAPI/Uvicorn (jeden proces), httpx, Playwright/Chromium, SQLite w WAL, pywebpush i cryptography. HTML/CSS/JS bez bundlera i zewnętrznych fontów. Manifest i service worker obsługują instalację/push; prywatne strony i API nie są cache'owane offline. Caddy zapewnia HTTPS. Docker Compose, trwałe wolumeny `app_data`, `caddy_data`, `caddy_config`.

Docelowo Ubuntu 24.04 ARM64 na Oracle A1 1 OCPU/6 GB RAM. Obraz budowany na serwerze pobiera Chromium właściwej architektury. Nie dodano płatnych usług, baz ani zewnętrznego SaaS. Opłaty i kwalifikację Always Free trzeba kontrolować w OCI; aplikacja sama nie zmienia zasobów chmury. Używany darmowy adres sslip.io zależy od dostępności zewnętrznego DNS.

Publiczne porty: 80 i 443. SSH 22 najlepiej ograniczyć do własnego IP. Port 8000 jest opublikowany wyłącznie na localhost. Proces aplikacji działa jako użytkownik 10001. Trwałe dane w `/app/data`; nie zmieniaj nazwy projektu Compose przy aktualizacjach.

Konfiguracja `.env`:

```dotenv
PUBLIC_ORIGIN=https://130-61-102-33.sslip.io
DOMAIN=130-61-102-33.sslip.io
PANEL_PASSWORD=tu-wpisz-wlasne-haslo
HANDOFF_SCHEMES=mobywatel
HEADED=0
```

Hasło ma minimum 8 znaków. Tymczasowe `admin123` nadal jest obsługiwane, ale jest łatwe do odgadnięcia na publicznym serwerze. Zmiana `PANEL_PASSWORD` i restart aplikacji unieważnia sesje panelu. Żadne hasło nie jest dołączone do paczki.

Nowa instalacja: utwórz powyższy `.env`, a następnie `docker compose -p info-login-poc --profile public up -d --build --wait`. Uruchomienie lokalne: venv, `pip install -r requirements.txt`, `python -m playwright install --with-deps chromium`, `python setup_local.py`, `python run.py`. Otwórz `http://localhost:8000`. Do logowania na telefonie potrzebny jest dostępny z telefonu adres HTTPS.

## Obsługa, kopie i cofnięcie aktualizacji

Z folderu nowej aplikacji:

```bash
docker compose -p info-login-poc ps
docker compose -p info-login-poc logs --tail=100 app
docker compose -p info-login-poc --profile public restart app
bash backup.sh
```

Kopia zawiera spójną bazę SQLite, `storage.key`, `vapid.pem` i konfigurację; traktuj ją jako poufną. Klucze są niezbędne do odczytu subskrypcji i zachowania ich ważności. Kopie zapisują się w `~/catcher-backups`. Nie zawierają sesji portalu. W razie odtwarzania zatrzymaj app, skopiuj pliki `data` do jej wolumenu, ustaw właściciela 10001:10001 i uprawnienia katalogu 700/plików 600, przywróć `.env`, uruchom app i zaloguj się do portalu. Nie publikuj bazy, kluczy ani konfiguracji w repozytorium.

Cofnięcie do starego PoC (jeśli jego folder istnieje):

```bash
cd /home/ubuntu/info-login-poc
docker compose -p info-login-poc --profile public up -d --build
```

Nowy wolumen danych pozostanie dostępny, ale stary PoC go nie wykorzysta. Po cofnięciu również trzeba zalogować się do portalu.

## Testy i granice weryfikacji

```bash
python -m unittest discover -s tests -v
```

Testy używają mocków API i push: nie tworzą rezerwacji, nie logują się do mObywatela i nie wysyłają rzeczywistych powiadomień. Obejmują wiele przedziałów, granice godzin, zmianę czasu, błędny schemat, limity i ich trwałość, rozróżnienie utraty sesji od awarii sieci, podmianę klienta podczas odczytu, zatrzymanie monitora, deduplikację, kolejkę powiadomień, uwierzytelnianie panelu, ochronę Origin i zapis konfiguracji.

Weryfikacja wydania: 15 testów Python — PASS; test interakcji DOM panelu w jsdom — PASS; start Uvicorn, healthcheck oraz udostępnianie plików PWA — PASS. Opcjonalny test DOM: `npm install --no-save jsdom`, następnie `node tests/ui-smoke.cjs` (Node jest potrzebny tylko do tego testu). Podgląd graficzny w Chromium nie został wykonany: środowisko testowe nie pobrało poprawnego archiwum przeglądarki. Obrazu Docker nie zbudowano lokalnie, ponieważ brak demona Docker; skrypt aktualizacji buduje go na VPS przed zmianą działających kontenerów.

Dotychczasowy proces logowania został potwierdzony przez właściciela na VPS. Nowe pobieranie terminarza i dostarczenie push wymagają próby na zalogowanym koncie i telefonie po wdrożeniu. Nie deklarujemy przetestowania tego konta ani obrazu ARM64 w środowisku developerskim. API Info-Kierowca jest nieudokumentowaną integracją i może się zmienić; błąd `SCHEMA` wymaga aktualizacji adaptera, `UPSTREAM` sprawdzenia diagnostyki. Nie przyspieszaj zapytań w odpowiedzi na błąd 429.

## Architektura i dalsza rozbudowa

`browser.py`: tylko logowanie; `session.py`: klient API, odnowienie, blokada podmiany; `models.py`: filtry i adapter odpowiedzi; `monitor.py`: harmonogram, okna i deduplikacja; `notify.py`: kolejka push; `store.py`: dane SQLite; `server.py`: panel i API; `static/`: responsywny interfejs PWA.

To aplikacja **jednego właściciela**, nie gotowy publiczny SaaS. Wiersze są przypisane do właściciela, ale przyszłe udostępnienie wielu osobom wymaga prawdziwych kont, izolowanych menedżerów sesji i workerów, ograniczeń per konto, bezpiecznego zarządzania sekretami, polityki retencji oraz przeglądu warunków integracji. Nie uruchamiaj kilku workerów Uvicorn ani replik — zdublowałyby monitoring. Konta, PostgreSQL i Redis nie są potrzebne do tej wersji.

## Źródła referencyjne

- https://github.com/losipiuk/examcatch — struktura odczytu terminarza i reakcje na limity, nie źródło prawdy o naszym UX logowania.
- https://github.com/WioN780/auto-book-info-kierowca — odnowienie JWT i okna terminarza; nie skopiowano funkcji rezerwacji.
- https://github.com/FilipNowakowicz/info-kierowca-notifier — katalog ośrodków wykorzystany na MIT, patrz `THIRD_PARTY_NOTICES.md`.
- https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/ — wymagania Web Push na iOS.

Kod integracji jest niezależny; repozytoria posłużyły jako referencje endpointów i struktur. Nie założono, że działanie dowolnego z nich gwarantuje zgodność z obecną wersją portalu.
