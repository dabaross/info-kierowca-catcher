from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from .browser import LoginController

ROOT = Path(__file__).parent / "static"
basic = HTTPBasic(auto_error=False)


def create_app(controller=None):
    password = os.environ.get("PANEL_PASSWORD", "")
    origin = os.environ.get("PUBLIC_ORIGIN", "http://localhost:8000").rstrip("/")
    if len(password) < 24:
        raise RuntimeError("PANEL_PASSWORD must have at least 24 characters; run setup_local.py")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise RuntimeError("PUBLIC_ORIGIN must be an HTTP(S) origin without credentials")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise RuntimeError("Remote panel access requires an HTTPS PUBLIC_ORIGIN")
    if parsed.path or parsed.query or parsed.fragment or not parsed.netloc:
        raise RuntimeError("PUBLIC_ORIGIN must be an origin only")
    controller = controller or LoginController(
        schemes=set(os.environ.get("HANDOFF_SCHEMES", "mobywatel").lower().split(",")),
        headed=os.environ.get("HEADED", "0") == "1",
    )
    failures = defaultdict(deque)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await controller.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.controller = controller

    @app.middleware("http")
    async def headers(request: Request, call_next):
        if request.headers.get("host", "").lower() != parsed.netloc.lower():
            return Response(status_code=400)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        return response

    async def authenticate(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)):
        # One server/owner PoC. Failed authentication is bounded globally, not a spoofable proxy IP.
        bucket = failures["panel"]
        now = time.monotonic()
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        if len(bucket) >= 10:
            raise HTTPException(429, "Spróbuj za minutę.")
        valid = credentials and secrets.compare_digest(credentials.username.encode(), b"owner")
        valid = bool(valid and secrets.compare_digest(credentials.password.encode(), password.encode()))
        if not valid:
            bucket.append(now)
            raise HTTPException(401, "Zaloguj się do panelu", headers={"WWW-Authenticate": 'Basic realm="Login PoC"'})

    async def authorize_write(request: Request, _=Depends(authenticate)):
        if request.headers.get("origin") != origin or request.headers.get("x-poc-action") != "1":
            raise HTTPException(403, "Nieprawidłowe źródło żądania.")

    @app.get("/", dependencies=[Depends(authenticate)])
    async def index():
        return FileResponse(ROOT / "index.html")

    @app.get("/api/status", dependencies=[Depends(authenticate)])
    async def status():
        return controller.attempt.public() if controller.attempt else {"state": "IDLE", "message": "Gotowy do próby logowania."}

    @app.post("/api/start", dependencies=[Depends(authorize_write)])
    async def start():
        return (await controller.start()).public()

    @app.post("/api/stop", dependencies=[Depends(authorize_write)])
    async def stop():
        await controller.cancel()
        return {"stopped": True}

    app.mount("/static", StaticFiles(directory=ROOT), name="static")
    return app
