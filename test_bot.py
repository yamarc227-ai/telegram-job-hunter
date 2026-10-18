import asyncio
import os
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import main
import sources
from sources import Job, matches, salary_ceiling


class Filters(unittest.TestCase):
    def setUp(self):
        self.job = Job('Test', 'Продуктовый аналитик', 'Компания', 'https://example.com/jobs/1',
                       'Команда ищет аналитика для развития продукта. Удалённая работа.', '250K — 300K ₽', remote=True)

    def test_salary_and_role(self):
        self.assertTrue(matches(self.job))
        self.assertFalse(matches(replace(self.job, salary='до 200 000 ₽')))
        self.assertTrue(matches(replace(self.job, salary='от 200 000 RUB')))
        self.assertTrue(matches(replace(self.job, salary='Не указана')))
        self.assertFalse(matches(replace(self.job, title='Системный аналитик')))
        self.assertFalse(matches(replace(self.job, remote=False)))
        self.assertTrue(matches(replace(self.job, description='English-speaking team only')))
        self.assertEqual(salary_ceiling('от 250 000 до 270 000 ₽'), 270000)
        self.assertEqual(salary_ceiling('250K — 300K ₽'), 300000)
        self.assertEqual(sources.remote_scope('Global (Remote)'), 'worldwide')
        self.assertEqual(sources.remote_scope('Remote - US/Canada'), 'restricted')
        self.assertEqual(sources.remote_scope('Remote - Europe Only'), 'restricted')
        self.assertEqual(sources.remote_scope('Remote'), 'unverified')
        self.assertFalse(matches(replace(self.job, remote_scope='restricted')))
        self.assertIsNone(salary_ceiling('от 200000 RUB до налогов'))


    def test_broad_analyst_roles_are_included(self):
        roles = [
            'Аналитик', 'Аналитик по продажам', 'Финансовый аналитик',
            'Маркетинговый аналитик', 'BI Analyst', 'Revenue Analyst',
            'Operations Analyst', 'Growth Insights', 'Business Intelligence',
            'Data Analyst', 'Game Analytics Analyst'
        ]
        for title in roles:
            with self.subTest(title=title):
                self.assertTrue(matches(replace(self.job, title=title, description='SQL, dashboards, metrics')),
                                title)

    def test_irrelevant_roles_still_excluded(self):
        for title in ['Системный аналитик', 'Analytics Engineer',
                      'Data Engineer', 'ETL Analyst', 'DWH Analyst']:
            with self.subTest(title=title):
                self.assertFalse(matches(replace(self.job, title=title)))
        self.assertFalse(matches(replace(self.job, company='Большой Банк')))
        self.assertFalse(matches(replace(self.job, salary='до 220 000 ₽')))
        self.assertFalse(matches(replace(self.job, remote_scope='restricted')))
        self.assertFalse(matches(replace(self.job, title='Project Manager')))

    def test_recent_publication_ranking(self):
        old = replace(self.job, published='Tue, 13 Oct 2026 12:00:00 +0300')
        new = replace(self.job, published='Sun, 18 Oct 2026 12:00:00 +0300')
        self.assertGreater(sources.rank(new), sources.rank(old))


class Behavior(unittest.IsolatedAsyncioTestCase):
    async def test_hh_missing_token_does_not_request(self):
        client = AsyncMock()
        with patch.dict(os.environ, {'HH_ACCESS_TOKEN': '', 'HH_MODE': 'api'}):
            with self.assertRaisesRegex(sources.SourceUnavailable, 'HH_ACCESS_TOKEN'):
                await sources.hh(client)
        client.get.assert_not_awaited()

    async def test_hh_token_is_scoped_and_errors_are_safe(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.host == 'api.hh.ru':
                self.assertEqual(request.headers['Authorization'], 'Bearer test-secret')
                return httpx.Response(200, json={'items': []})
            self.assertNotIn('Authorization', request.headers)
            return httpx.Response(200)
        with patch.dict(os.environ, {'HH_ACCESS_TOKEN': 'test-secret', 'HH_MODE': 'api'}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                self.assertEqual(await sources.hh(client), [])
                await client.get('https://example.com')
            async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(403, json={'errors': [{'value': 'token_expired'}], 'secret': 'test-secret'})
            )) as client:
                with self.assertRaisesRegex(sources.SourceUnavailable, 'недействителен') as caught:
                    await sources.hh(client)
                self.assertNotIn('test-secret', str(caught.exception))

    async def test_credentials_change_refreshes_cache(self):
        result = ([], {'Test': {'ok': True, 'total': 0, 'matched': 0}})
        with patch.object(main, 'collect', AsyncMock(return_value=result)) as collect:
            with patch.dict(os.environ, {'HH_ACCESS_TOKEN': ''}):
                await main.catalogue()
            with patch.dict(os.environ, {'HH_ACCESS_TOKEN': 'new-test-token'}):
                await main.catalogue()
            self.assertEqual(collect.await_count, 2)

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = main.DB, main.DATA_DIR, main.ALLOWED_CHAT_ID
        main.DATA_DIR = Path(self.tmp.name)
        main.DB = main.DATA_DIR / 'test.sqlite3'
        main.ALLOWED_CHAT_ID = '123'
        main.init_db()
        self.job = Job('Test', 'Product Analyst', 'Company', 'https://example.com/jobs/1',
                       'Продуктовая аналитика, удалённая работа в команде', remote=True)

    async def asyncTearDown(self):
        main.DB, main.DATA_DIR, main.ALLOWED_CHAT_ID = self.old
        self.tmp.cleanup()

    async def test_delivery_dedup_and_failure(self):
        bot = AsyncMock()
        data = {'jobs': [asdict(self.job)]}
        self.assertEqual(await main.deliver(bot, 123, data), (1, 0))
        self.assertEqual(await main.deliver(bot, 123, data), (0, 0))
        self.assertEqual(await main.deliver(bot, 456, data), (0, 0))
        new_data = {'jobs': [asdict(replace(self.job, url='https://example.com/jobs/2'))]}
        bot.send_message.side_effect = RuntimeError('delivery failed')
        with self.assertRaises(RuntimeError):
            await main.deliver(bot, 123, new_data)
        bot.send_message.side_effect = None
        self.assertEqual(await main.deliver(bot, 123, new_data), (1, 0))


    async def test_manual_batch_size_is_fifteen(self):
        bot = AsyncMock()
        data = {'jobs': [asdict(replace(self.job, url=f'https://example.com/jobs/{i}')) for i in range(17)]}
        with patch.object(main.asyncio, 'sleep', AsyncMock()):
            sent, remainder = await main.deliver(bot, 123, data)
        self.assertEqual((sent, remainder), (15, 2))
        self.assertEqual(bot.send_message.await_count, 15)

    async def test_partial_source_failure(self):
        request = httpx.Request('GET', 'https://example.com')
        denied = httpx.HTTPStatusError('403', request=request, response=httpx.Response(403, request=request))
        providers = {'Good': AsyncMock(return_value=[self.job]), 'Blocked': AsyncMock(side_effect=denied)}
        with patch.dict(sources.PROVIDERS, providers, clear=True):
            with patch('boards.collect_boards', AsyncMock(return_value=([], {}))):
                jobs, statuses = await sources.collect()
        self.assertEqual(len(jobs), 1)
        self.assertTrue(statuses['Good']['ok'])
        self.assertEqual(statuses['Blocked']['error'], 'HTTP 403')

    async def test_cache_survives_repeated_checks(self):
        result = ([asdict(self.job)], {'Test': {'ok': True, 'total': 1, 'matched': 1}})
        with patch.object(main, 'collect', AsyncMock(return_value=result)) as collect:
            await main.catalogue()
            await main.catalogue()
            collect.assert_awaited_once()

    async def test_all_failed_is_not_zero_vacancies(self):
        message = AsyncMock()
        message.chat.id = 123
        main.lock = asyncio.Lock()
        data = {'jobs': [], 'checked': 0, 'sources': {'hh.ru': {'ok': False, 'error': 'HTTP 403'}}}
        with patch.object(main, 'catalogue', AsyncMock(return_value=data)):
            await main.check(message)
        self.assertIn('Поиск не выполнен', message.answer.call_args.args[0])


    async def test_hh_rss_without_token(self):
        content = ('<?xml version="1.0"?><rss><channel><item><title>Продуктовый аналитик</title>'
                   '<link>https://hh.ru/vacancy/123</link>'
                   '<description>Пользователи, SQL и продуктовые метрики</description>'
                   '</item></channel></rss>').encode('utf-8')
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=content, headers={'content-type': 'application/rss+xml'})
        )) as client:
            with patch.dict(os.environ, {'HH_MODE': 'rss', 'HH_ACCESS_TOKEN': ''}):
                items = await sources.hh(client)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, 'hh.ru RSS')

    async def test_hh_rss_denial_without_leaking(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(403, text='captcha')
        )) as client:
            with patch.dict(os.environ, {'HH_MODE': 'rss'}):
                with self.assertRaisesRegex(sources.SourceUnavailable, 'HTTP 403'):
                    await sources.hh(client)


    async def test_hh_rss_partial_failure_keeps_results(self):
        seen = []
        xml = ('<?xml version="1.0"?><rss><channel><item><title>Аналитик продаж</title>'
               '<link>https://hh.ru/vacancy/987</link><description>SQL</description>'
               '</item></channel></rss>').encode()
        def handler(request):
            term = request.url.params.get('text', '')
            seen.append(term)
            if term == 'аналитик':
                return httpx.Response(403)
            return httpx.Response(200, content=xml, headers={'content-type': 'application/rss+xml'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with patch.dict(os.environ, {'HH_MODE': 'rss'}):
                jobs = await sources.hh(client)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, 'Аналитик продаж')
        self.assertGreaterEqual(len(seen), 10)

    async def test_habr_rss_deduplicates_english_russian_queries(self):
        xml = ('<?xml version="1.0"?><rss><channel><item><title>Data Analyst</title>'
               '<link>https://career.habr.com/vacancies/987</link>'
               '<description>Remote analytics SQL</description></item></channel></rss>').encode()
        requests = []
        def handler(request):
            requests.append(request.url.params.get('q'))
            return httpx.Response(200, content=xml)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await sources.habr(client)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].title, 'Data Analyst')
        self.assertIn('analyst', requests)
        self.assertTrue(result[0].remote)

    async def test_remotive_job(self):
        payload = {'jobs': [{'title': 'Product Analyst', 'company_name': 'GameCo',
                   'url': 'https://remotive.com/job/123', 'description': 'product metrics',
                   'candidate_required_location': 'Worldwide', 'publication_date': ''}]}
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=payload)
        )) as client:
            results = await sources.remotive(client)
        self.assertEqual(len(results), 1)
        self.assertTrue(matches(results[0]))
        self.assertEqual(results[0].remote_scope, 'worldwide')


class BoardParser(unittest.TestCase):
    def test_ashby_onsite_excluded(self):
        import boards
        rows = {'jobs': [
            {'title': 'Product Analyst', 'jobUrl': 'https://jobs.ashbyhq.com/higgsfieldai/test',
             'location': 'Almaty, Kazakhstan', 'workplaceType': 'OnSite',
             'isRemote': False, 'descriptionPlain': 'Product metrics and cohorts'},
            {'title': 'Product Analyst', 'jobUrl': 'https://jobs.ashbyhq.com/higgsfieldai/remote',
             'location': 'Global (Remote)', 'workplaceType': 'Remote',
             'isRemote': True, 'descriptionPlain': 'Product metrics and cohorts'},
        ]}
        result = boards.parse_ashby('Higgsfield AI', rows)
        self.assertEqual(len(result), 2)
        self.assertFalse(matches(result[0]))
        self.assertTrue(matches(result[1]))

    def test_lever_onsite_excluded(self):
        import boards
        data = [
            {'text': 'Product Analyst', 'hostedUrl': 'https://jobs.lever.co/test/1',
             'categories': {'location': 'New York'}, 'workplaceType': 'on-site',
             'descriptionPlain': 'Product analysis'},
            {'text': 'Product Analyst', 'hostedUrl': 'https://jobs.lever.co/test/2',
             'categories': {'location': 'Remote'}, 'workplaceType': 'remote',
             'descriptionPlain': 'Product analysis'},
        ]
        jobs = boards.parse_lever('Example', data)
        self.assertEqual(len(jobs), 2)
        self.assertFalse(matches(jobs[0]))
        self.assertTrue(matches(jobs[1]))

    def test_greenhouse_without_remote_claim_excluded(self):
        import boards
        rows = {'jobs': [
            {'title': 'Business Analyst', 'absolute_url': 'https://boards.greenhouse.io/test/1',
             'location': {'name': 'London'}, 'content': 'Product metrics and customers'},
            {'title': 'Business Analyst (Remote)', 'absolute_url': 'https://boards.greenhouse.io/test/2',
             'location': {'name': 'Remote'}, 'content': 'Product metrics and customers'},
        ]}
        jobs = boards.parse_greenhouse('Example', rows)
        self.assertEqual(len(jobs), 2)
        self.assertFalse(matches(jobs[0]))
        self.assertTrue(matches(jobs[1]))

    def test_board_list_has_42_distinct_entries(self):
        import json
        import boards
        entries = json.loads(boards.BOARDS_FILE.read_text())
        self.assertEqual(len(entries), 42)
        self.assertEqual(len({(entry['ats'], entry['slug']) for entry in entries}), 42)
        self.assertIn(('ashby', 'higgsfieldai'), {(x['ats'], x['slug']) for x in entries})


if __name__ == '__main__':
    unittest.main()
