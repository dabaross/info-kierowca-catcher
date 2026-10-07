import asyncio
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import httpx
from pydantic import ValidationError
from poc.models import CATEGORIES, MonitorConfig, parse_schedule, WARSAW, now_local
from poc.store import Store
from poc.session import Sessions, PortalError, SCHEDULE_PATH, ALLOWED, build_schedule_payload
from poc.monitor import Monitor
from poc.notify import Push


def config():
    return MonitorConfig(center_id=43,ranges=[dict(date_from='2026-10-12',date_to='2026-10-16',time_from='07:00',time_to='09:00',weekdays=[0,1,2,3,4]),dict(date_from='2026-10-12',date_to='2026-10-16',time_from='16:00',time_to='18:00',weekdays=[0,1,2,3,4])])


def slot(at, key='slot'):
    return {'key':key,'start':at,'center_id':43,'center_name':'Test','places':1}


def exam(at, **changes):
    return {'practiceId':'one','practiceDateTime':at,'placePracticeAmount':1,
            'theoryId':None,'theoryDateTime':None,
            'examType':'Practice','category':'B','organizationId':43,
            'organizationName':'PORD Gdańsk',**changes}


def schedule(at):
    return [{'wordId':43,'wordName':'PORD Gdańsk','examCollectionForDay':[exam(at)]}]


def schedule_many(exams):
    return [{'wordId':43,'wordName':'PORD Gdańsk','examCollectionForDay':exams}]


class Filters(unittest.TestCase):
    def test_ranges_or_boundaries_gaps_weekdays_and_timezone(self):
        c=config(); now=datetime(2026,10,10,tzinfo=WARSAW)
        for at in ['2026-10-12T07:00:00+02:00','2026-10-12T09:00:00+02:00','2026-10-12T16:00:00+02:00','2026-10-12T16:00:00Z']:
            self.assertTrue(c.matches(slot(at),now),at)
        for at in ['2026-10-12T12:00:00+02:00','2026-10-12T18:01:00+02:00','2026-10-17T08:00:00+02:00']:
            self.assertFalse(c.matches(slot(at),now),at)
        self.assertFalse(c.matches({**slot('2026-10-12T08:00:00+02:00'),'center_id':1},now))

    def test_windows_union_and_validation(self):
        c=config();self.assertEqual(c.windows(date(2026,10,10)),[(date(2026,10,12),date(2026,10,16))])
        self.assertEqual(c.windows(date(2026,10,17)),[])
        data=c.model_dump(mode='json');data['ranges']=[]
        with self.assertRaises(ValidationError):MonitorConfig(**data)
        data=c.model_dump(mode='json');data['ranges'][1]['date_to']='2027-01-01'
        with self.assertRaises(ValidationError):MonitorConfig(**data)

    def test_multiple_centers_practice_filters_deduplicates_and_dst(self):
        raw = [
            {'wordId':43,'wordName':'PORD Gdańsk','examCollectionForDay':[
                exam('2026-10-25T08:00:00Z'),
                exam('2026-10-25T08:30:00Z',examType='Theory',practiceId='theory'),
                exam('2026-10-25T09:00:00Z',practiceId=None),
                exam('2026-10-25T09:30:00Z',placePracticeAmount=0,practiceId='full'),
                exam('2026-10-25T10:00:00Z',category='A',practiceId='other-category'),
                exam('2026-10-25T08:00:00Z'),
            ]},
            {'wordId':44,'wordName':'Other center','examCollectionForDay':[
                exam('2026-10-25T11:00:00Z',practiceId='other-center')
            ]},
        ]
        slots = parse_schedule(raw, 'b')
        self.assertEqual(len(slots), 2)
        self.assertEqual(slots[0], {
            'key':'43:one','center_id':43,'center_name':'PORD Gdańsk',
            'start':'2026-10-25T09:00:00+01:00','places':1,
            'category':'B','practice_id':'one',
        })
        self.assertEqual(slots[1]['key'], '44:other-center')
        for malformed in [{}, {'examCollectionForDay':[]}, [None],
                          [{'wordId':43,'wordName':'PORD','examCollectionForDay':None}]]:
            with self.assertRaises(ValueError):
                parse_schedule(malformed, 'B')

    def test_schedule_payload_is_isolated_and_schedule_path_allowed(self):
        self.assertEqual(SCHEDULE_PATH, '/bknd/exam/api/v1/Schedules/user/MultipleCentersExams')
        self.assertIn(('POST', SCHEDULE_PATH), ALLOWED)
        payload=build_schedule_payload(
            {'number':'123456789','category':'B'}, [43, 44], date(2026, 11, 3)
        )
        self.assertEqual(payload, {
            'startDate':'2026-11-03','organizationId':[43,44],
            'category':5,'profileNumber':'123456789','profileType':'Pkk',
        })
        self.assertEqual(CATEGORIES.index('B'),5)

    def test_parsed_slot_outside_preferred_range_is_rejected(self):
        c = config()
        parsed = parse_schedule(schedule('2026-10-12T12:00:00+02:00'), 'B')[0]
        self.assertFalse(c.matches(parsed, datetime(2026,10,10,tzinfo=WARSAW)))


class Integration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.store=Store(self.path);self.sessions=Sessions(self.store)
        self.push=Push(self.store,self.path,'https://example.com')

    async def asyncTearDown(self):
        await self.sessions.close();self.store.close();self.tmp.cleanup()

    async def install(self, handler):
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await self.sessions.install(client,[{'pkkNumber':'123456789','categoryName':'B'}])
        return next(iter(self.sessions.profiles))

    async def test_429_persists_and_never_calls_twice(self):
        calls=[]
        reset=time.time()+1200
        def handler(request):
            calls.append(request)
            return httpx.Response(429,headers={'retry-after':'900','x-ratelimit-reset':str(reset)})
        profile=await self.install(handler)
        for _ in range(2):
            with self.assertRaises(PortalError) as e:await self.sessions.schedule(profile,43,date.today())
            self.assertEqual(e.exception.code,'RATE_LIMITED')
        self.assertEqual(len(calls),1)
        self.assertGreaterEqual(self.store.cooldown(SCHEDULE_PATH),reset)
        other=Store(self.path);self.assertGreaterEqual(other.cooldown(SCHEDULE_PATH),reset);other.close()

    async def test_expired_vs_network(self):
        profile=await self.install(lambda request:httpx.Response(401))
        with self.assertRaises(PortalError) as e:await self.sessions.schedule(profile,43,date.today())
        self.assertEqual(e.exception.code,'NEEDS_LOGIN');self.assertIsNone(self.sessions.client)
        self.store.db.execute('DELETE FROM limits');self.store.db.commit()
        def fail(request):raise httpx.ConnectError('network')
        profile=await self.install(fail)
        with self.assertRaises(PortalError) as e:await self.sessions.schedule(profile,43,date.today())
        self.assertEqual(e.exception.code,'NETWORK');self.assertIsNotNone(self.sessions.client)

    async def test_403_requires_login(self):
        profile=await self.install(lambda request:httpx.Response(403))
        with self.assertRaises(PortalError) as e:
            await self.sessions.schedule(profile,43,date.today())
        self.assertEqual(e.exception.code,'NEEDS_LOGIN')
        self.assertIsNone(self.sessions.client)

    async def test_confirmed_payload_and_204_refresh(self):
        requests=[]
        def handler(request):
            if request.url.path.endswith('/jwt/refresh'):
                return httpx.Response(204)
            requests.append(request)
            return httpx.Response(200,json=schedule('2026-11-03T16:00:00'))
        profile=await self.install(handler)
        self.sessions.last_refresh=0
        await self.sessions.schedule(profile,43,date(2026,11,3))
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0].url.path,SCHEDULE_PATH)
        self.assertEqual(requests[0].read(),b'{"startDate":"2026-11-03","organizationId":[43],"category":5,"profileNumber":"123456789","profileType":"Pkk"}')

    async def test_invalid_schedule_schema_is_reported_without_expiring_session(self):
        profile=await self.install(lambda request:httpx.Response(200,json={'unexpected':'shape'}))
        tomorrow=now_local().date()+timedelta(days=1)
        monitor=Monitor(self.store,self.sessions,self.push)
        monitor.save(MonitorConfig(
            center_id=43,profile_id=profile,
            ranges=[dict(date_from=tomorrow,date_to=tomorrow,weekdays=list(range(7)))],
        ))
        monitor.toggle(True)
        await monitor.tick()
        self.assertEqual(monitor.state,'SCHEMA')
        self.assertIsNotNone(self.sessions.client)
        self.assertEqual(monitor.public()['slots'],[])

    async def test_profiles_are_category_filtered_and_private_fields_are_not_public(self):
        client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request:httpx.Response(200)
        ))
        await self.sessions.install(client,[
            {'pkkNumber':'12345678901234','categoryName':'b','firstName':'Jan',
             'lastName':'Kowalski','pesel':'12345678901','birthDate':'2000-01-01'},
            {'pkkNumber':'987654321','categoryName':'unsupported'},
        ])
        public=self.sessions.public()
        self.assertEqual(len(public['profiles']),1)
        self.assertEqual(public['profiles'][0]['label'],'Kategoria B · PKK …1234')
        serialized=str(public)
        for private in ['12345678901234','Jan','Kowalski','12345678901','2000-01-01']:
            self.assertNotIn(private,serialized)

    async def test_hot_swap_during_request(self):
        started,finish=asyncio.Event(),asyncio.Event()
        async def handler(request):started.set();await finish.wait();return httpx.Response(200,json=[])
        profile=await self.install(handler);old=self.sessions.client
        request=asyncio.create_task(self.sessions.schedule(profile,43,date.today()));await started.wait()
        replacement=asyncio.create_task(self.install(lambda r:httpx.Response(200,json={})))
        await asyncio.sleep(0);self.assertFalse(old.is_closed)
        finish.set();await request;await replacement
        self.assertTrue(old.is_closed);self.assertEqual(profile,next(iter(self.sessions.profiles)))

    async def test_monitor_dedup_and_push_queue(self):
        tomorrow=now_local().date()+timedelta(days=1)
        raw=schedule_many([
            exam(tomorrow.isoformat()+'T08:00:00'),
            exam(tomorrow.isoformat()+'T08:30:00'),
            exam(tomorrow.isoformat()+'T16:30:00',practiceId='two'),
            exam(tomorrow.isoformat()+'T12:00:00',practiceId='outside'),
        ])
        profile=await self.install(lambda r:httpx.Response(200,json=raw))
        monitor=Monitor(self.store,self.sessions,self.push)
        morning=dict(date_from=tomorrow,date_to=tomorrow,time_from='07:00',time_to='10:00',weekdays=list(range(7)))
        evening=dict(date_from=tomorrow,date_to=tomorrow,time_from='16:00',time_to='18:00',weekdays=list(range(7)))
        monitor.save(MonitorConfig(center_id=43,profile_id=profile,ranges=[morning,evening]))
        self.store.subscribe('test',{'endpoint':'https://fcm.googleapis.com/test'})
        monitor.toggle(True);await monitor.tick()
        self.assertEqual({s['practice_id'] for s in monitor.public()['slots']},{'one','two'})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)
        monitor.next_check=0;self.store.db.execute('DELETE FROM limits');self.store.db.commit();await monitor.tick()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)
        with patch.object(self.push,'_deliver',return_value=503):await self.push.flush()
        self.assertEqual(self.store.db.execute('SELECT tries FROM outbox').fetchone()[0],1)
        self.store.db.execute('UPDATE outbox SET due=0');self.store.db.commit()
        with patch.object(self.push,'_deliver',return_value=410):await self.push.flush()
        self.assertEqual(self.store.subscriptions(),[])

    async def test_pause_discards_inflight_result(self):
        started,finish=asyncio.Event(),asyncio.Event();tomorrow=now_local().date()+timedelta(days=1)
        async def handler(request):started.set();await finish.wait();return httpx.Response(200,json=schedule(tomorrow.isoformat()+'T08:00:00'))
        profile=await self.install(handler)
        monitor=Monitor(self.store,self.sessions,self.push)
        monitor.save(MonitorConfig(center_id=43,profile_id=profile,ranges=[dict(date_from=tomorrow,date_to=tomorrow,weekdays=list(range(7)))]));monitor.toggle(True)
        task=asyncio.create_task(monitor.tick());await started.wait();monitor.toggle(False);finish.set();await task
        self.assertEqual(monitor.state,'PAUSED');self.assertEqual(monitor.public()['slots'],[])
