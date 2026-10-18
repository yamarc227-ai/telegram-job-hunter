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
        self.assertFalse(matches(replace(self.job, description='English-speaking team only')))
        self.assertEqual(salary_ceiling('от 250 000 до 270 000 ₽'), 270000)
        self.assertEqual(salary_ceiling('250K — 300K ₽'), 300000)
        self.assertIsNone(salary_ceiling('от 200000 RUB до налогов'))


class Behavior(unittest.IsolatedAsyncioTestCase):
    async def test_hh_missing_token_does_not_request(self):
        client = AsyncMock()
        with patch.dict(os.environ, {'HH_ACCESS_TOKEN': ''}):
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
        with patch.dict(os.environ, {'HH_ACCESS_TOKEN': 'test-secret'}):
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

    async def test_partial_source_failure(self):
        request = httpx.Request('GET', 'https://example.com')
        denied = httpx.HTTPStatusError('403', request=request, response=httpx.Response(403, request=request))
        providers = {'Good': AsyncMock(return_value=[self.job]), 'Blocked': AsyncMock(side_effect=denied)}
        with patch.dict(sources.PROVIDERS, providers, clear=True):
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


if __name__ == '__main__':
    unittest.main()
