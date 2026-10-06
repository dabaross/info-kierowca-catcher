import os
import unittest
from unittest.mock import patch

import httpx
from poc.core import Attempt
from poc.server import create_app


class StubController:
    attempt = None
    starts = 0

    async def start(self):
        self.starts += 1
        self.attempt = Attempt("stub")
        return self.attempt

    async def cancel(self):
        self.attempt = None


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {"PANEL_PASSWORD": "a" * 32, "PUBLIC_ORIGIN": "https://poc.example"})
        self.env.start()
        self.controller = StubController()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.controller)),
                                      base_url="https://poc.example")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.env.stop()

    async def test_status_requires_auth_and_is_not_cached(self):
        self.assertEqual((await self.client.get("/api/status")).status_code, 401)
        result = await self.client.get("/api/status", auth=("owner", "a" * 32))
        self.assertEqual(result.json()["state"], "IDLE")
        self.assertEqual(result.headers["cache-control"], "no-store")

    async def test_cross_origin_cannot_start_browser(self):
        auth = ("owner", "a" * 32)
        bad = await self.client.post("/api/start", auth=auth,
                                     headers={"Origin": "https://evil.example", "X-Poc-Action": "1"})
        self.assertEqual(bad.status_code, 403)
        self.assertEqual(self.controller.starts, 0)
        good = await self.client.post("/api/start", auth=auth,
                                      headers={"Origin": "https://poc.example", "X-Poc-Action": "1"})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(self.controller.starts, 1)

    async def test_unknown_host_rejected(self):
        result = await self.client.get("/api/status", headers={"Host": "evil.example"})
        self.assertEqual(result.status_code, 400)
