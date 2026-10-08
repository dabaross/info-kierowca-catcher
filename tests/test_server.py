import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from poc.core import Attempt
from poc.server import create_app


class StubController:
    attempt = None
    starts = 0
    async def start(self):
        self.starts += 1
        self.attempt = Attempt('stub')
        return self.attempt
    async def cancel(self):
        self.attempt = None


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'PANEL_PASSWORD':'admin123','PUBLIC_ORIGIN':'https://poc.example','DATA_DIR':self.directory.name})
        self.env.start()
        self.controller = StubController()
        self.app = create_app(self.controller)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='https://poc.example')
        self.headers = {'Origin':'https://poc.example','X-Catcher-Action':'1'}

    async def asyncTearDown(self):
        await self.client.aclose()
        self.app.state.store.close()
        self.env.stop()
        self.directory.cleanup()

    async def login(self):
        result = await self.client.post('/api/auth/login',json={'password':'admin123'},headers=self.headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertIn('HttpOnly',result.headers['set-cookie'])
        self.assertIn('Secure',result.headers['set-cookie'])

    async def test_auth_and_logout(self):
        self.assertEqual((await self.client.get('/api/status')).status_code,401)
        await self.login()
        result = await self.client.get('/api/status')
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.headers['cache-control'],'no-store')
        await self.client.post('/api/auth/logout',headers=self.headers)
        self.assertEqual((await self.client.get('/api/status')).status_code,401)

    async def test_csrf_and_size(self):
        await self.login()
        result = await self.client.post('/api/login/start',headers={'Origin':'https://evil.example','X-Catcher-Action':'1'})
        self.assertEqual(result.status_code,403)
        self.assertEqual(self.controller.starts,0)
        self.assertEqual((await self.client.post('/api/login/start',headers=self.headers)).status_code,200)
        self.assertEqual(self.controller.starts,1)
        self.assertEqual((await self.client.post('/api/auth/login',headers=self.headers,content='x'*33000)).status_code,413)

    async def test_save_multiple_ranges_restart_and_validation(self):
        await self.login()
        centers=await self.client.get('/api/centers')
        self.assertEqual([c['id'] for c in centers.json()],[43])
        config=(await self.client.get('/api/status')).json()['monitor']['config']
        status=(await self.client.get('/api/status')).json()
        self.assertEqual(config['interval_seconds'],1200)
        self.assertEqual(status['monitor']['request_policy']['jwt_refresh_min_interval_seconds'],480)
        self.assertIn('logowaniu',status['monitor']['request_policy']['profile_check'])
        config['ranges'].append({**config['ranges'][0],'time_from':'18:00','time_to':'20:00'})
        result=await self.client.put('/api/config',headers=self.headers,json=config)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(len(self.app.state.store.settings()[0].ranges),2)
        config['center_id']=42
        result=await self.client.put('/api/config',headers=self.headers,json=config)
        self.assertEqual(result.status_code,422)
        self.assertIn('PORD Gdańsk',result.json()['detail'])
        self.assertEqual(self.app.state.store.settings()[0].center_id,43)
        for interval in [900,1200,1800,3600]:
            config['center_id']=43
            config['interval_seconds']=interval
            result=await self.client.put('/api/config',headers=self.headers,json=config)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['interval_seconds'],interval)
        for interval in [899,360,600]:
            config['interval_seconds']=interval
            result=await self.client.put('/api/config',headers=self.headers,json=config)
            self.assertEqual(result.status_code,422)
        config['ranges'][1]['time_to']='06:00'
        self.assertEqual((await self.client.put('/api/config',headers=self.headers,json=config)).status_code,422)
        self.assertEqual((await self.client.get('/api/status',headers={'Host':'evil.example'})).status_code,400)

    async def test_bad_password_and_arbitrary_push_endpoint(self):
        r=await self.client.post('/api/auth/login',headers=self.headers,json={'password':'wrong'})
        self.assertEqual(r.status_code,401)
        await self.login()
        r=await self.client.post('/api/push/subscribe',headers=self.headers,json={'endpoint':'http://169.254.169.254/metadata'})
        self.assertEqual(r.status_code,422)
