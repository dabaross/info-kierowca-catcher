import asyncio
import json
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
import httpx
from pydantic import ValidationError
from poc.models import (
    CATEGORIES, MonitorConfig, parse_one_center_schedule,
    WARSAW, now_local,
)
from poc.store import Store
from poc.session import (
    Sessions, PortalError, SCHEDULE_PATH, LEGACY_SCHEDULE_PATH, ALLOWED,
    build_schedule_payload,
)
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
    return schedule_many([exam(at)])


def schedule_many(exams):
    by_date = {}
    for item in exams:
        at = item.get('practiceDateTime') or item.get('theoryDateTime')
        if at:
            day = datetime.fromisoformat(at).date().isoformat()
            by_date.setdefault(day, []).append(item)
    return {
        'startDatePointerForCalendar': '2026-10-12',
        'examCollectionForDay': [
            {'date': day, 'examCollections': items}
            for day, items in sorted(by_date.items())
        ],
    }


class Filters(unittest.TestCase):
    def test_monitor_interval_default_minimum_and_options(self):
        self.assertEqual(MonitorConfig().interval_seconds,1200)
        for interval in [900,1200,1800,3600]:
            with self.subTest(interval=interval):
                self.assertEqual(MonitorConfig(interval_seconds=interval).interval_seconds,interval)
        for interval in [899,360,600]:
            with self.subTest(interval=interval):
                with self.assertRaises(ValidationError):
                    MonitorConfig(interval_seconds=interval)

    def test_ranges_or_boundaries_gaps_weekdays_and_timezone(self):
        c=config(); now=datetime(2026,10,10,tzinfo=WARSAW)
        for at in ['2026-10-12T07:00:00+02:00','2026-10-12T09:00:00+02:00','2026-10-12T16:00:00+02:00','2026-10-12T16:00:00Z']:
            self.assertTrue(c.matches(slot(at),now),at)
        for at in ['2026-10-12T12:00:00+02:00','2026-10-12T18:01:00+02:00','2026-10-17T08:00:00+02:00']:
            self.assertFalse(c.matches(slot(at),now),at)
        self.assertFalse(c.matches({**slot('2026-10-12T08:00:00+02:00'),'center_id':1},now))

    def test_start_date_uses_earliest_interesting_future_date(self):
        c=config()
        self.assertEqual(c.start_date(date(2026,10,10)),date(2026,10,12))
        self.assertIsNone(c.start_date(date(2026,10,17)))
        long_range = MonitorConfig(ranges=[dict(
            date_from='2026-10-12', date_to='2026-11-30',
            weekdays=list(range(7)),
        )])
        self.assertEqual(
            long_range.start_date(date(2026,10,10)),
            date(2026,10,12),
        )
        data=c.model_dump(mode='json');data['ranges']=[]
        with self.assertRaises(ValidationError):MonitorConfig(**data)
        data=c.model_dump(mode='json');data['ranges'][1]['date_to']='2027-01-01'
        with self.assertRaises(ValidationError):MonitorConfig(**data)

    def test_one_center_calendar_parses_every_day_and_every_exam_collection(self):
        practical_by_day = {day: [] for day in range(10, 31)}
        for index in range(194):
            day = 10 + index % 21
            hour = 8 + index // 21
            practical_by_day[day].append({
                'practiceId': f'practice-{index}',
                'practiceDateTime': f'2026-11-{day:02d}T{hour:02d}:{index % 60:02d}:00+01:00',
                'placePracticeAmount': 1 + index % 4,
                'examType': 'Practice',
                'category': 'B',
                'organizationId': 43,
                'firstAvailable': False,
            })
        for index in range(126):
            day = 10 + index % 21
            practical_by_day[day].append({
                'practiceId': None, 'practiceDateTime': None,
                'placePracticeAmount': 0, 'theoryId': f'theory-{index}',
                'examType': 'Theoretical', 'category': 'B', 'organizationId': 43,
            })
        response = {
            'startDatePointerForCalendar': 'calendar-pointer',
            'examCollectionForDay': [{
                    'date': '2026-10-18',
                    'examCollections': [{
                        'theoryId': 'theory-only',
                        'theoryDateTime': '2026-10-18T08:00:00+02:00',
                        'placeTheoryAmount': 2,
                        'examType': 'Theoretical',
                        'category': 'B',
                        'organizationId': 43,
                    }],
                },
            ] + [{
                    'date': f'2026-11-{day:02d}',
                    'examCollections': collections,
                }
                for day, collections in practical_by_day.items()
            ],
        }
        slots = parse_one_center_schedule(response, 'B')
        self.assertEqual(len(slots), 194)
        self.assertEqual(len({slot['key'] for slot in slots}), 194)
        self.assertEqual(slots[0]['center_name'], 'PORD Gdańsk')
        self.assertEqual(slots[0]['center_id'], 43)
        self.assertEqual({slot['places'] for slot in slots}, {1, 2, 3, 4})
        self.assertTrue(all(slot['category'] == 'B' for slot in slots))
        config = MonitorConfig(
            center_id=43,
            ranges=[dict(
                date_from='2026-10-07', date_to='2026-11-30',
                time_from='05:30', time_to='23:00',
                weekdays=list(range(7)),
            )],
        )
        now = datetime(2026, 10, 10, 12, 0, tzinfo=WARSAW)
        self.assertEqual(
            len([slot for slot in slots if config.matches(slot, now)]), 194
        )
        duplicate = dict(response['examCollectionForDay'][1]['examCollections'][0])
        response['examCollectionForDay'][1]['examCollections'].append(duplicate)
        self.assertEqual(len(parse_one_center_schedule(response, 'B')), 194)
        self.assertEqual(parse_one_center_schedule(response, 'A'), [])

    def test_one_center_calendar_requires_confirmed_center_and_valid_practice(self):
        response = {
            'startDatePointerForCalendar': 'p',
            'examCollectionForDay': [{
                'date': '2026-11-10',
                'examCollections': [
                    exam('2026-11-10T08:00:00+01:00'),
                    exam('2026-11-10T08:30:00+01:00', practiceId=None),
                    exam('2026-11-10T09:00:00+01:00', placePracticeAmount=0,
                         practiceId='full'),
                    exam('2026-11-10T09:30:00+01:00', category='A',
                         practiceId='other-category'),
                    exam('2026-11-10T10:00:00+01:00', organizationId=42,
                         practiceId='other-center'),
                ],
            }],
        }
        slots = parse_one_center_schedule(response, 'B')
        self.assertEqual([slot['practice_id'] for slot in slots], ['one'])
        response['examCollectionForDay'][0]['examCollections'].append(
            exam('2026-11-10T10:30:00+01:00', organizationId=42,
                 practiceId='other-center')
        )
        self.assertEqual(
            [slot['practice_id'] for slot in parse_one_center_schedule(response, 'B')],
            ['one'],
        )
        for malformed in [
            {},
            {'startDatePointerForCalendar': 'p', 'examCollectionForDay': None},
            {'startDatePointerForCalendar': 'p', 'examCollectionForDay': [
                {'date': 'not-a-date', 'examCollections': []},
            ]},
        ]:
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                parse_one_center_schedule(malformed, 'B')
        mismatch = {
            'startDatePointerForCalendar': 'p',
            'examCollectionForDay': [{
                'date': '2026-11-10',
                'examCollections': [exam('2026-11-11T08:00:00+01:00')],
            }],
        }
        with self.assertRaisesRegex(ValueError, 'one_center_day_mismatch'):
            parse_one_center_schedule(mismatch, 'B')

    def test_one_center_slots_use_future_date_time_and_weekday_filters(self):
        response = {
            'startDatePointerForCalendar': 'p',
            'examCollectionForDay': [
                {'date': '2026-11-10', 'examCollections': [
                    exam('2026-11-10T08:00:00+01:00', practiceId='past'),
                ]},
                {'date': '2026-11-16', 'examCollections': [
                    exam('2026-11-16T08:00:00+01:00', practiceId='matching'),
                ]},
                {'date': '2026-11-14', 'examCollections': [
                    exam('2026-11-14T08:00:00+01:00', practiceId='weekend'),
                ]},
                {'date': '2026-11-17', 'examCollections': [
                    exam('2026-11-17T18:00:00+01:00', practiceId='late'),
                ]},
            ],
        }
        slots = parse_one_center_schedule(response, 'B')
        config = MonitorConfig(
            center_id=43,
            ranges=[dict(
                date_from='2026-11-10', date_to='2026-11-20',
                time_from='07:00', time_to='09:00',
                weekdays=[0, 1, 2, 3, 4],
            )],
        )
        now = datetime(2026, 11, 10, 8, 30, tzinfo=WARSAW)
        matches = [slot for slot in slots if config.matches(slot, now)]
        self.assertEqual([slot['practice_id'] for slot in matches], ['matching'])

    def test_filter_change_can_match_and_notify_previously_nonmatching_exam(self):
        tomorrow = now_local().date() + timedelta(days=1)
        exam_slot = {
            'key': '43:existing', 'center_id': 43, 'center_name': 'PORD Gdańsk',
            'start': tomorrow.isoformat() + 'T10:00:00+02:00',
            'places': 1, 'category': 'B', 'practice_id': 'existing',
        }
        early_filter = MonitorConfig(
            center_id=43,
            ranges=[dict(date_from=tomorrow, date_to=tomorrow,
                         time_from='07:00', time_to='09:00', weekdays=list(range(7)))],
        )
        later_filter = MonitorConfig(
            center_id=43,
            ranges=[dict(date_from=tomorrow, date_to=tomorrow,
                         time_from='09:00', time_to='11:00', weekdays=list(range(7)))],
        )
        now = datetime.combine(tomorrow, datetime.min.time(), WARSAW)
        self.assertFalse(early_filter.matches(exam_slot, now))
        matches = [slot for slot in [exam_slot] if later_filter.matches(slot, now)]
        self.assertEqual([slot['practice_id'] for slot in matches], ['existing'])
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            payload = lambda new: {'title': 'new', 'body': str(len(new))}
            earlier_matches = [
                slot for slot in [exam_slot] if early_filter.matches(slot, now)
            ]
            self.assertEqual(store.record_matches('profile', earlier_matches, payload), [])
            later_matches = [
                slot for slot in [exam_slot] if later_filter.matches(slot, now)
            ]
            self.assertEqual(
                [slot['practice_id'] for slot in store.record_matches(
                    'profile', later_matches, payload
                )],
                ['existing'],
            )
            store.close()

    def test_schedule_payload_is_isolated_and_schedule_path_allowed(self):
        self.assertEqual(
            SCHEDULE_PATH,
            '/bknd/exam/api/v1/Schedules/user/OneCenterExam',
        )
        self.assertIn(('POST', SCHEDULE_PATH), ALLOWED)
        payload=build_schedule_payload(
            {'number':'123456789','category':'B'}, date(2026, 11, 3)
        )
        self.assertEqual(payload, {
            'startDate':'2026-11-03','organizationId':[43],
            'category':5,'profileNumber':'123456789','profileType':'Pkk',
        })
        self.assertEqual(CATEGORIES.index('B'),5)

    def test_dst_winter_time_is_converted_to_warsaw(self):
        response = {
            'startDatePointerForCalendar': '2026-10-12',
            'examCollectionForDay': [{
                'date': '2026-10-25',
                'examCollections': [
                    exam('2026-10-25T03:15:00'),
                    exam('2026-10-25T01:15:00Z', practiceId='utc'),
                ],
            }],
        }
        slots = parse_one_center_schedule(response, 'B')
        self.assertEqual(slots[0]['start'], '2026-10-25T02:15:00+01:00')
        self.assertEqual(slots[1]['start'], '2026-10-25T03:15:00+01:00')

    def test_parsed_slot_outside_preferred_range_is_rejected(self):
        c = config()
        parsed = {
            **slot('2026-10-12T12:00:00+02:00'),
            'category': 'B',
            'practice_id': 'outside-range',
        }
        self.assertFalse(c.matches(parsed, datetime(2026,10,10,tzinfo=WARSAW)))

    def test_legacy_window_snapshots_migrate_to_latest_calendar(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory))
            store.db.execute("""
                CREATE TABLE schedule_snapshots(
                    owner TEXT, profile TEXT, window_start TEXT,
                    payload TEXT NOT NULL, at REAL NOT NULL,
                    PRIMARY KEY(owner,profile,window_start)
                )
            """)
            legacy_payloads = [
                {
                    'from': day, 'to': day, 'requested_start': day,
                    'calendar_dates': [day], 'slots': [slot(
                        '2026-11-10T08:00:00+01:00', key
                    )],
                }
                for day, key in [('2026-10-12', 'old'), ('2026-10-20', 'latest')]
            ]
            for index, payload in enumerate(legacy_payloads, start=1):
                store.db.execute(
                    'INSERT INTO schedule_snapshots VALUES (?,?,?,?,?)',
                    ('owner', 'profile:one-center', payload['requested_start'],
                     json.dumps(payload), index),
                )
            store.db.commit()
            store.close()

            migrated = Store(Path(directory))
            self.assertEqual(
                [saved['key'] for saved in migrated.schedule_calendar(
                    'profile:one-center'
                )['slots']],
                ['latest'],
            )
            self.assertIsNone(migrated.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='schedule_snapshots'"
            ).fetchone())
            migrated.close()


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

    async def test_legacy_interval_migration_preserves_other_settings(self):
        original=MonitorConfig(
            center_id=43,profile_id='saved-profile',interval_seconds=1200,
            ranges=[
                dict(date_from='2026-11-03',date_to='2026-11-06',
                     time_from='07:00',time_to='09:00',weekdays=[0,1,2,3,4]),
                dict(date_from='2026-11-03',date_to='2026-11-06',
                     time_from='16:00',time_to='18:00',weekdays=[0,1,2,3,4]),
            ],
        ).model_dump(mode='json')
        for old_interval, expected in [(360,1200),(600,1200),(900,900),
                                       (1800,1800),(3600,3600)]:
            with self.subTest(old_interval=old_interval):
                legacy={**original,'interval_seconds':old_interval}
                self.store.db.execute(
                    'UPDATE settings SET config=?,enabled=1 WHERE owner=?',
                    (json.dumps(legacy),'owner'),
                )
                self.store.db.commit()
                migrated,enabled=self.store.settings()
                self.assertTrue(enabled)
                self.assertEqual(migrated.interval_seconds,expected)
                self.assertEqual(len(migrated.ranges),2)
                self.assertEqual(migrated.profile_id,'saved-profile')
                persisted=json.loads(self.store.db.execute(
                    'SELECT config FROM settings WHERE owner=?',('owner',)
                ).fetchone()[0])
                self.assertEqual(persisted['interval_seconds'],expected)

    async def test_enabled_legacy_monitor_waits_one_new_interval_after_upgrade(self):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json=[])
        profile=await self.install(handler)
        tomorrow=now_local().date()+timedelta(days=1)
        legacy=MonitorConfig(
            center_id=43,profile_id=profile,
            ranges=[dict(date_from=tomorrow,date_to=tomorrow,weekdays=list(range(7)))],
        ).model_dump(mode='json')
        legacy['interval_seconds']=360
        self.store.db.execute(
            'UPDATE settings SET config=?,enabled=1 WHERE owner=?',
            (json.dumps(legacy),'owner'),
        )
        self.store.db.commit()
        monitor=Monitor(self.store,self.sessions,self.push)
        self.assertEqual(monitor.config.interval_seconds,1200)
        self.assertGreaterEqual(monitor.next_check,time.time()+1199)
        last_success=monitor.last_check
        await monitor.tick()
        self.assertEqual(monitor.state,'WAITING')
        self.assertIsNone(last_success)
        self.assertEqual(monitor.public()['last_check'],None)
        self.assertEqual(calls,[])
        restored=Monitor(self.store,self.sessions,self.push)
        await restored.tick()
        self.assertEqual(calls,[])

    async def test_inflight_schedule_keeps_next_due_across_monitor_restart(self):
        started,finish=asyncio.Event(),asyncio.Event()
        calls=[]
        async def handler(request):
            calls.append(request)
            started.set()
            await finish.wait()
            return httpx.Response(200,json=[])
        profile=await self.install(handler)
        tomorrow=now_local().date()+timedelta(days=1)
        monitor=Monitor(self.store,self.sessions,self.push)
        monitor.save(MonitorConfig(
            center_id=43,profile_id=profile,
            ranges=[dict(date_from=tomorrow,date_to=tomorrow,weekdays=list(range(7)))],
        ))
        monitor.toggle(True)
        in_flight=asyncio.create_task(monitor.tick())
        await started.wait()
        restored=Monitor(self.store,self.sessions,self.push)
        self.assertGreaterEqual(restored.next_check,time.time()+1199)
        await restored.tick()
        self.assertEqual(len(calls),1)
        finish.set()
        await in_flight
        self.assertEqual(len(calls),1)

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

    async def test_schedule_respects_persisted_legacy_endpoint_cooldown(self):
        calls=[]
        profile=await self.install(
            lambda request: (calls.append(request) or httpx.Response(200, json=schedule_many([])))
        )
        until=time.time()+900
        self.store.defer(LEGACY_SCHEDULE_PATH, until)
        with self.assertRaises(PortalError) as error:
            await self.sessions.schedule(profile,43,date.today())
        self.assertEqual(error.exception.code,'RATE_LIMITED')
        self.assertEqual(error.exception.until,until)
        self.assertEqual(calls,[])

    async def test_jwt_refresh_cadence_and_profile_probe_on_refresh_failure(self):
        from poc.session import PROFILE_PATH, REFRESH_INTERVAL_SECONDS, REFRESH_PATH
        requests=[]
        def handler(request):
            requests.append(request.url.path)
            if request.url.path==REFRESH_PATH:
                return httpx.Response(500)
            return httpx.Response(200,json=[
                {'pkkNumber':'123456789','categoryName':'B'}
            ])
        await self.install(handler)
        self.sessions.last_refresh=0
        await self.sessions.maintain()
        self.assertEqual(requests,[REFRESH_PATH,PROFILE_PATH])
        self.assertEqual(REFRESH_INTERVAL_SECONDS,480)
        await self.sessions.maintain()
        self.assertEqual(requests,[REFRESH_PATH,PROFILE_PATH])

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
        self.assertEqual(requests[0].method,'POST')
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

    async def test_monitor_does_not_query_for_non_b_profile(self):
        calls=[]
        profile=await self.install(
            lambda request: (calls.append(request) or httpx.Response(200,json=[]))
        )
        self.sessions.profiles[profile]['category']='A'
        tomorrow=now_local().date()+timedelta(days=1)
        monitor=Monitor(self.store,self.sessions,self.push)
        monitor.save(MonitorConfig(
            center_id=43,profile_id=profile,
            ranges=[dict(date_from=tomorrow,date_to=tomorrow,weekdays=list(range(7)))],
        ))
        monitor.toggle(True)
        await monitor.tick()
        self.assertEqual(monitor.state,'NEEDS_PROFILE')
        self.assertEqual(calls,[])

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
        calls=[]
        raw=schedule_many([
            exam(tomorrow.isoformat()+'T08:00:00'),
            exam(tomorrow.isoformat()+'T08:30:00'),
            exam(tomorrow.isoformat()+'T16:30:00',practiceId='two'),
            exam(tomorrow.isoformat()+'T12:00:00',practiceId='outside'),
            *[exam(tomorrow.isoformat()+'T08:00:00',practiceId=str(center_id),
                   organizationId=center_id) for center_id in [42,53,73,9]],
        ])
        def handler(request):
            if request.method=='POST':
                calls.append(request)
            return httpx.Response(200,json=raw)
        profile=await self.install(handler)
        monitor=Monitor(self.store,self.sessions,self.push)
        morning=dict(date_from=tomorrow,date_to=tomorrow,time_from='07:00',time_to='10:00',weekdays=list(range(7)))
        evening=dict(date_from=tomorrow,date_to=tomorrow,time_from='16:00',time_to='18:00',weekdays=list(range(7)))
        monitor.save(MonitorConfig(center_id=43,profile_id=profile,ranges=[morning,evening]))
        self.store.subscribe('test',{'endpoint':'https://fcm.googleapis.com/test'})
        monitor.toggle(True);await monitor.tick()
        self.assertEqual({s['practice_id'] for s in monitor.public()['slots']},{'one','two'})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)
        first_success=monitor.last_check
        first_next=monitor.next_check
        restored=Monitor(self.store,self.sessions,self.push)
        self.assertEqual(restored.last_check,first_success)
        self.assertEqual(restored.next_check,first_next)
        await restored.tick()
        self.assertEqual(len(calls),1)
        self.assertEqual(
            {slot['practice_id'] for slot in restored.public()['slots']},
            {'one', 'two'},
        )
        message=json.loads(self.store.db.execute('SELECT payload FROM outbox').fetchone()[0])
        self.assertIn('PORD Gdańsk',message['body'])
        self.assertIn('2 nowych terminów',message['body'])
        self.assertIn('08:30',message['body'])
        self.assertIn('16:30',message['body'])
        for name in ['PORD Gdynia','WORD Elbląg','PORD Chojnice','WORD Grudziądz']:
            self.assertNotIn(name,message['body'])
        monitor.next_check=0;self.store.db.execute('DELETE FROM limits');self.store.db.commit();await monitor.tick()
        self.assertEqual(len(calls),2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)
        with patch.object(self.push,'_deliver',return_value=503):await self.push.flush()
        self.assertEqual(self.store.db.execute('SELECT tries FROM outbox').fetchone()[0],1)
        self.store.db.execute('UPDATE outbox SET due=0');self.store.db.commit()
        with patch.object(self.push,'_deliver',return_value=410):await self.push.flush()
        self.assertEqual(self.store.subscriptions(),[])

    async def test_filter_change_relogin_and_restart_keep_slots_without_duplicate_alerts(self):
        tomorrow = now_local().date() + timedelta(days=1)
        raw = schedule_many([
            exam(tomorrow.isoformat() + 'T08:00:00+02:00', practiceId='morning'),
            exam(tomorrow.isoformat() + 'T12:00:00+02:00', practiceId='noon'),
        ])
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=raw)

        profile = await self.install(handler)
        monitor = Monitor(self.store, self.sessions, self.push)
        morning = dict(
            date_from=tomorrow, date_to=tomorrow, time_from='07:00',
            time_to='09:00', weekdays=list(range(7)),
        )
        noon = dict(
            date_from=tomorrow, date_to=tomorrow, time_from='11:00',
            time_to='13:00', weekdays=list(range(7)),
        )
        monitor.save(MonitorConfig(center_id=43, profile_id=profile, ranges=[morning]))
        self.store.subscribe('test', {'endpoint': 'https://fcm.googleapis.com/test'})
        monitor.toggle(True)
        await monitor.tick()
        self.assertEqual(
            [slot['practice_id'] for slot in monitor.public()['slots']],
            ['morning'],
        )
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)

        monitor.save(MonitorConfig(center_id=43, profile_id=profile, ranges=[noon]))
        self.assertEqual(
            [slot['practice_id'] for slot in monitor.public()['slots']],
            ['noon'],
        )
        monitor.next_check = 0
        self.store.db.execute('DELETE FROM limits')
        self.store.db.commit()
        await monitor.tick()
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 2)

        restored_store = Store(self.path)
        try:
            restored = Monitor(restored_store, self.sessions, self.push)
            self.assertEqual(
                [slot['practice_id'] for slot in restored.public()['slots']],
                ['noon'],
            )
            self.assertEqual(
                restored_store.record_matches(
                    profile, [parse_one_center_schedule(raw, 'B')[1]],
                    lambda new: {'title': 'duplicate', 'body': str(len(new))},
                ),
                [],
            )
            await self.install(handler)
            await restored.tick()
            self.assertEqual(
                [slot['practice_id'] for slot in restored.public()['slots']],
                ['noon'],
            )
            self.assertEqual(
                restored_store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],
                2,
            )
        finally:
            restored_store.close()

    async def test_one_center_monitor_keeps_results_beyond_old_twenty_day_window(self):
        tomorrow = now_local().date() + timedelta(days=1)
        last_day = tomorrow + timedelta(days=40)
        calendar = schedule_many([
            exam(last_day.isoformat() + 'T08:00:00+01:00', practiceId='late'),
        ])
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=calendar)

        profile = await self.install(handler)
        monitor = Monitor(self.store, self.sessions, self.push)
        monitor.save(MonitorConfig(
            center_id=43, profile_id=profile,
            ranges=[dict(
                date_from=tomorrow, date_to=last_day,
                time_from='05:30', time_to='23:00',
                weekdays=list(range(7)),
            )],
        ))
        monitor.toggle(True)
        await monitor.tick()
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [slot['practice_id'] for slot in monitor.public()['slots']],
            ['late'],
        )
        public = monitor.public()
        self.assertEqual(public['calendar']['requested_start'], tomorrow.isoformat())
        self.assertEqual(public['calendar']['calendar_dates'], [last_day.isoformat()])
        self.assertNotIn('windows', public)
        self.assertNotIn('windows_total', public)
        self.assertIn('Pełny zakres filtrów nie jest potwierdzony', monitor.message)
        stored = self.store.schedule_calendar(f'{profile}:one-center')
        self.assertEqual(
            [slot['practice_id'] for slot in stored['slots']],
            ['late'],
        )

    async def test_october_twelve_request_includes_november_ten_exam(self):
        requested = []
        response = {
            'startDatePointerForCalendar': '2026-10-12',
            'examCollectionForDay': [{
                'date': '2026-11-10',
                'examCollections': [
                    exam('2026-11-10T08:30:00+01:00', practiceId='november-ten'),
                ],
            }],
        }

        def handler(request):
            requested.append(request)
            return httpx.Response(200, json=response)

        profile = await self.install(handler)
        monitor = Monitor(self.store, self.sessions, self.push)
        monitor.save(MonitorConfig(
            center_id=43, profile_id=profile,
            ranges=[dict(
                date_from='2026-10-12', date_to='2026-11-30',
                time_from='05:30', time_to='23:00', weekdays=list(range(7)),
            )],
        ))
        monitor.toggle(True)
        with patch(
            'poc.monitor.now_local',
            return_value=datetime(2026, 10, 10, 22, 25, tzinfo=WARSAW),
        ):
            await monitor.tick()

        self.assertEqual(len(requested), 1)
        self.assertEqual(
            json.loads(requested[0].read())['startDate'],
            '2026-10-12',
        )
        self.assertEqual(
            [slot['practice_id'] for slot in monitor.public()['slots']],
            ['november-ten'],
        )

    async def test_successful_calendar_read_replaces_removed_portal_terms(self):
        tomorrow = now_local().date() + timedelta(days=1)
        responses = [
            schedule(tomorrow.isoformat() + 'T08:00:00+02:00'),
            schedule_many([]),
        ]

        def handler(request):
            return httpx.Response(200, json=responses.pop(0))

        profile = await self.install(handler)
        monitor = Monitor(self.store, self.sessions, self.push)
        monitor.save(MonitorConfig(
            center_id=43, profile_id=profile,
            ranges=[dict(
                date_from=tomorrow, date_to=tomorrow,
                time_from='05:30', time_to='23:00', weekdays=list(range(7)),
            )],
        ))
        monitor.toggle(True)
        await monitor.tick()
        self.assertEqual(len(monitor.public()['slots']), 1)

        self.store.db.execute(
            'UPDATE limits SET until=0 WHERE owner=? AND endpoint=?',
            ('owner', SCHEDULE_PATH),
        )
        self.store.db.commit()
        monitor.next_check = 0
        await monitor.tick()

        self.assertEqual(monitor.public()['slots'], [])
        self.assertEqual(monitor.public()['calendar']['slots'], [])
        self.assertEqual(
            self.store.schedule_calendar(f'{profile}:one-center')['slots'],
            [],
        )

    async def test_one_center_request_payload_matches_confirmed_request_shape(self):
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=schedule_many([]))
        profile=await self.install(handler)
        await self.sessions.schedule(profile,43,date(2026,10,12))
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0].method,'POST')
        self.assertEqual(
            requests[0].url,
            'https://info-kierowca.pl/bknd/exam/api/v1/Schedules/user/OneCenterExam',
        )
        self.assertEqual(requests[0].headers['content-type'],'application/json')
        self.assertEqual(
            json.loads(requests[0].read()),
            {
                'startDate':'2026-10-12','organizationId':[43],
                'category':5,'profileNumber':'123456789','profileType':'Pkk',
            },
        )

    async def test_saved_calendar_remains_visible_when_filters_change_and_after_restart(self):
        tomorrow = now_local().date() + timedelta(days=1)
        exam_at = f'{tomorrow.isoformat()}T10:00:00'
        profile = await self.install(
            lambda request: httpx.Response(200, json=schedule(exam_at))
        )
        monitor = Monitor(self.store, self.sessions, self.push)
        monitor.save(MonitorConfig(
            center_id=43, profile_id=profile,
            ranges=[dict(
                date_from=tomorrow, date_to=tomorrow,
                time_from='07:00', time_to='09:00', weekdays=list(range(7)),
            )],
        ))
        monitor.toggle(True)
        await monitor.tick()
        self.assertEqual(monitor.public()['slots'], [])

        monitor.save(MonitorConfig(
            center_id=43, profile_id=profile,
            ranges=[dict(
                date_from=tomorrow, date_to=tomorrow,
                time_from='09:00', time_to='11:00', weekdays=list(range(7)),
            )],
        ))
        self.assertEqual(
            [slot['practice_id'] for slot in monitor.public()['slots']],
            ['one'],
        )
        restored = Monitor(self.store, self.sessions, self.push)
        self.assertEqual(
            [slot['practice_id'] for slot in restored.public()['slots']],
            ['one'],
        )

    async def test_successful_empty_expired_429_and_network_states(self):
        tomorrow=now_local().date()+timedelta(days=1)
        def new_monitor(profile):
            monitor=Monitor(self.store,self.sessions,self.push)
            monitor.save(MonitorConfig(
                center_id=43,profile_id=profile,
                ranges=[dict(date_from=tomorrow,date_to=tomorrow,weekdays=list(range(7)))],
            ))
            monitor.toggle(True)
            return monitor

        profile=await self.install(
            lambda request:httpx.Response(200,json=schedule_many([]))
        )
        monitor=new_monitor(profile)
        await monitor.tick()
        self.assertEqual(monitor.state,'WATCHING')
        self.assertEqual(monitor.public()['slots'],[])
        self.assertIsNotNone(monitor.last_check)
        self.assertGreaterEqual(monitor.next_check,monitor.last_check+1200)

        self.store.db.execute('DELETE FROM limits');self.store.db.commit()
        profile=await self.install(lambda request:httpx.Response(401))
        monitor=new_monitor(profile)
        await monitor.tick()
        self.assertEqual(monitor.state,'NEEDS_LOGIN')
        self.assertEqual(monitor.public()['http_status'],401)

        self.store.db.execute('DELETE FROM limits');self.store.db.commit()
        reset=time.time()+1500
        profile=await self.install(lambda request:httpx.Response(
            429,headers={'retry-after':'900','x-ratelimit-reset':str(reset)}
        ))
        monitor=new_monitor(profile)
        await monitor.tick()
        self.assertEqual(monitor.state,'RATE_LIMITED')
        self.assertEqual(monitor.public()['http_status'],429)
        self.assertGreaterEqual(monitor.public()['next_check'],reset)

        self.store.db.execute('DELETE FROM limits');self.store.db.commit()
        def fail(request):
            raise httpx.ConnectError('network')
        profile=await self.install(fail)
        monitor=new_monitor(profile)
        await monitor.tick()
        self.assertEqual(monitor.state,'NETWORK')
        self.assertGreater(monitor.public()['next_check'],time.time())

    async def test_http_400_blocks_automatic_retries_and_persists_safe_diagnostic(self):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(400,json={
                'errorCode':'INVALID_ORGANIZATION',
                'message':'PKK-PRIVATE-123456789 token-private',
                'debug':'cookie-private',
            })
        profile=await self.install(handler)
        tomorrow=now_local().date()+timedelta(days=1)
        monitor=Monitor(self.store,self.sessions,self.push)
        monitor.save(MonitorConfig(
            center_id=43,profile_id=profile,
            ranges=[dict(date_from=tomorrow,date_to=tomorrow,weekdays=list(range(7)))],
        ))
        monitor.toggle(True)
        await monitor.tick()
        self.assertEqual(len(calls),1)
        self.assertEqual(monitor.state,'HTTP_400')
        self.assertEqual(monitor.public()['http_status'],400)
        self.assertEqual(monitor.public()['diagnostic'],'INVALID_ORGANIZATION')
        public=str(monitor.public())
        events=str(self.store.events())
        for private in ['PKK-PRIVATE-123456789','token-private','cookie-private']:
            self.assertNotIn(private,public)
            self.assertNotIn(private,events)
        await monitor.tick()
        restored=Monitor(self.store,self.sessions,self.push)
        await restored.tick()
        self.assertEqual(len(calls),1)
        self.assertEqual(restored.state,'HTTP_400')
        restored.save(MonitorConfig(
            center_id=43,profile_id=profile,
            ranges=[dict(date_from=tomorrow,date_to=tomorrow,weekdays=list(range(7)))],
        ))
        self.assertIsNone(self.store.schedule_block())

    async def test_saved_other_center_requires_gdansk_without_deleting_settings(self):
        legacy=MonitorConfig(center_id=42,profile_id='legacy')
        self.store.save_settings(legacy,True)
        monitor=Monitor(self.store,self.sessions,self.push)
        await monitor.tick()
        self.assertEqual(monitor.state,'NEEDS_CENTER')
        self.assertEqual(self.store.settings()[0].center_id,42)
        self.assertEqual(monitor.public()['slots'],[])

    async def test_pause_discards_inflight_result(self):
        started,finish=asyncio.Event(),asyncio.Event();tomorrow=now_local().date()+timedelta(days=1)
        async def handler(request):started.set();await finish.wait();return httpx.Response(200,json=schedule(tomorrow.isoformat()+'T08:00:00'))
        profile=await self.install(handler)
        monitor=Monitor(self.store,self.sessions,self.push)
        monitor.save(MonitorConfig(center_id=43,profile_id=profile,ranges=[dict(date_from=tomorrow,date_to=tomorrow,weekdays=list(range(7)))]));monitor.toggle(True)
        task=asyncio.create_task(monitor.tick());await started.wait();monitor.toggle(False);finish.set();await task
        self.assertEqual(monitor.state,'PAUSED');self.assertEqual(monitor.public()['slots'],[])
