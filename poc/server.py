import hashlib
import os
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from .browser import LoginController
from .models import CENTERS, MonitorConfig, PORD_GDANSK_ID
from .monitor import Monitor
from .notify import Push, subscription_id, validate_subscription
from .session import Sessions
from .store import Store

ROOT = Path(__file__).parent / 'static'


def create_app(controller=None):
    password = os.environ.get('PANEL_PASSWORD', '')
    origin = os.environ.get('PUBLIC_ORIGIN', 'http://localhost:8000').rstrip('/')
    parsed = urlsplit(origin)
    if len(password) < 8:
        raise RuntimeError('PANEL_PASSWORD requires at least 8 characters')
    if (parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username
            or parsed.password or parsed.path or parsed.query or parsed.fragment
            or (parsed.scheme != 'https' and parsed.hostname not in {'localhost', '127.0.0.1'})):
        raise RuntimeError('PUBLIC_ORIGIN must be an HTTPS origin (HTTP allowed on localhost)')
    directory = Path(os.environ.get('DATA_DIR', 'data'))
    store = Store(directory)
    sessions = Sessions(store)
    push = Push(store, directory, origin)
    monitor = Monitor(store, sessions, push)
    controller = controller or LoginController(
        schemes=set(os.environ.get('HANDOFF_SCHEMES', 'mobywatel').lower().split(',')),
        headed=os.environ.get('HEADED', '0') == '1', sessions=sessions)
    failures = deque(maxlen=30)
    secure = parsed.scheme == 'https'
    cookie = '__Host-catcher' if secure else 'catcher'
    version = hashlib.sha256(password.encode()).hexdigest()

    @asynccontextmanager
    async def lifespan(app):
        push.start()
        monitor.start()
        try:
            yield
        finally:
            await monitor.close()
            await controller.cancel()
            await sessions.close()
            await push.close()
            store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.sessions, app.state.monitor = store, sessions, monitor
    app.state.controller, app.state.push = controller, push

    @app.middleware('http')
    async def headers(request, call_next):
        if request.url.path != '/healthz' and request.headers.get('host', '').lower() != parsed.netloc.lower():
            return Response(status_code=400)
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if request.headers.get('origin') != origin or request.headers.get('x-catcher-action') != '1':
                return JSONResponse({'detail': 'Nieprawidłowe źródło żądania.'}, status_code=403)
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > 32768:
                    return Response(status_code=413)
            request._body = bytes(content)
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
            'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    async def auth(request: Request):
        token = hashlib.sha256(request.cookies.get(cookie, '').encode()).hexdigest()
        row = store.db.execute('SELECT 1 FROM panel_sessions WHERE token=? AND owner=? AND expires>? AND password_version=?', (token, store.owner, time.time(), version)).fetchone()
        if not row:
            raise HTTPException(401, 'Zaloguj się do panelu.')

    async def body(request):
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except ValueError:
            raise HTTPException(400, 'Nieprawidłowe dane.') from None

    @app.get('/healthz')
    async def health():
        return {'ok': True}

    @app.get('/')
    async def index():
        return FileResponse(ROOT / 'index.html')

    @app.get('/sw.js')
    async def sw():
        return FileResponse(ROOT / 'sw.js', media_type='application/javascript')

    @app.post('/api/auth/login')
    async def login(request: Request):
        now = time.time()
        while failures and failures[0] < now - 60:
            failures.popleft()
        if len(failures) >= 10:
            raise HTTPException(429, 'Za dużo prób. Zaczekaj minutę.')
        data = await body(request)
        if not isinstance(data.get('password'), str) or not secrets.compare_digest(data['password'].encode(), password.encode()):
            failures.append(now)
            raise HTTPException(401, 'Nieprawidłowe hasło.')
        token = secrets.token_urlsafe(32)
        with store.db:
            store.db.execute('DELETE FROM panel_sessions WHERE expires<? OR password_version!=?', (now, version))
            store.db.execute('INSERT INTO panel_sessions VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(), store.owner, now+30*86400, version))
        response = JSONResponse({'ok': True})
        response.set_cookie(cookie, token, max_age=30*86400, httponly=True, secure=secure, samesite='strict', path='/')
        return response

    @app.post('/api/auth/logout', dependencies=[Depends(auth)])
    async def logout(request: Request):
        with store.db:
            store.db.execute('DELETE FROM panel_sessions WHERE token=?', (hashlib.sha256(request.cookies.get(cookie, '').encode()).hexdigest(),))
        response = JSONResponse({'ok': True})
        response.delete_cookie(cookie, path='/', secure=secure, httponly=True, samesite='strict')
        return response

    @app.get('/api/status', dependencies=[Depends(auth)])
    async def status():
        return {'session': sessions.public(), 'login': controller.attempt.public() if controller.attempt else None,
                'monitor': monitor.public(), 'events': store.events(),
                'push': {'public_key': push.public_key, 'devices': len(store.subscriptions()), 'last_result': push.last_result}}

    @app.get('/api/centers', dependencies=[Depends(auth)])
    async def centers():
        return [center for center in CENTERS if center['id'] == PORD_GDANSK_ID]

    @app.post('/api/login/start', dependencies=[Depends(auth)])
    async def start_login():
        return (await controller.start()).public()

    @app.post('/api/login/cancel', dependencies=[Depends(auth)])
    async def cancel_login():
        await controller.cancel()
        return {'ok': True}

    @app.post('/api/session/disconnect', dependencies=[Depends(auth)])
    async def disconnect():
        await controller.cancel()
        monitor.toggle(False)
        await sessions.close()
        return {'ok': True}

    @app.put('/api/config', dependencies=[Depends(auth)])
    async def config(request: Request):
        try:
            config = MonitorConfig.model_validate(await body(request))
        except ValidationError as exc:
            raise HTTPException(422, '; '.join(e['msg'] for e in exc.errors(include_input=False))[:600]) from None
        if config.center_id != PORD_GDANSK_ID:
            raise HTTPException(422, 'Ta wersja monitoruje wyłącznie PORD Gdańsk (ID 43). Wybierz Gdańsk i zapisz konfigurację.')
        if sessions.client and config.profile_id not in sessions.profiles:
            raise HTTPException(422, 'Wybierz aktualny profil PKK.')
        monitor.save(config)
        return config.model_dump(mode='json')

    @app.post('/api/monitor/{action}', dependencies=[Depends(auth)])
    async def toggle(action: str):
        if action not in {'start', 'stop'}:
            raise HTTPException(404)
        monitor.toggle(action == 'start')
        return {'ok': True}

    @app.post('/api/push/subscribe', dependencies=[Depends(auth)])
    async def subscribe(request: Request):
        try:
            data = validate_subscription(await body(request))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(422, 'Nieprawidłowa lub nieobsługiwana subskrypcja Web Push.') from None
        if len(store.subscriptions()) >= 10 and subscription_id(data) not in dict(store.subscriptions()):
            raise HTTPException(422, 'Limit 10 urządzeń.')
        store.subscribe(subscription_id(data), data)
        return {'ok': True}

    @app.post('/api/push/unsubscribe', dependencies=[Depends(auth)])
    async def unsubscribe(request: Request):
        data = await body(request)
        store.unsubscribe(hashlib.sha256(str(data.get('endpoint', '')).encode()).hexdigest())
        return {'ok': True}

    @app.post('/api/push/test', dependencies=[Depends(auth)])
    async def test_push():
        if store.cooldown('push-test') > time.time():
            raise HTTPException(429, 'Zaczekaj minutę przed kolejnym testem.')
        if not store.subscriptions():
            raise HTTPException(422, 'Najpierw włącz powiadomienia.')
        store.defer('push-test', time.time()+60)
        push.send_event('Powiadomienia działają', 'To jest test z Twojej aplikacji. Monitoring działa na serwerze także po zamknięciu panelu.', 'test')
        return {'ok': True}

    app.mount('/static', StaticFiles(directory=ROOT), name='static')
    return app
