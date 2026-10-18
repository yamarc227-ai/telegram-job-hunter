"""Personal job hunter: Telegram delivery and HeadHunter API search."""
import asyncio
import logging
import os
import sqlite3
from pathlib import Path

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
ALLOWED_CHAT_ID = os.getenv('ALLOWED_CHAT_ID', '').strip()
INTERVAL = max(1, int(os.getenv('SEARCH_INTERVAL_HOURS', '6')))
DB = Path(__file__).with_name('vacancies.sqlite3')
TERMS = ['продуктовый аналитик', 'коммерческий аналитик', 'бизнес аналитик e-commerce', 'product analyst']
EXCLUDE = ['банк', 'banking', 'dwh engineer', 'data engineer', 'etl developer', 'системный аналитик']

bot = Bot(TOKEN)
dp = Dispatcher()


def init_db():
    with sqlite3.connect(DB) as db:
        db.execute('CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)')
        db.execute('CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY)')


def subscribers():
    with sqlite3.connect(DB) as db:
        return [row[0] for row in db.execute('SELECT chat_id FROM subscribers')]


def is_allowed(chat_id: int) -> bool:
    return not ALLOWED_CHAT_ID or str(chat_id) == ALLOWED_CHAT_ID


@dp.message(Command('start'))
async def start(message: Message):
    if not is_allowed(message.chat.id):
        return
    with sqlite3.connect(DB) as db:
        db.execute('INSERT OR IGNORE INTO subscribers VALUES (?)', (message.chat.id,))
    await message.answer('Привет! Я ищу вакансии продуктового и коммерческого аналитика. /check — проверить сейчас; /stop — отключить рассылку.\nТвой chat_id: ' + str(message.chat.id))


@dp.message(Command('stop'))
async def stop(message: Message):
    with sqlite3.connect(DB) as db:
        db.execute('DELETE FROM subscribers WHERE chat_id = ?', (message.chat.id,))
    await message.answer('Рассылка отключена.')


@dp.message(Command('check'))
async def check(message: Message):
    if not is_allowed(message.chat.id):
        return
    count = await scan(targets=[message.chat.id])
    await message.answer(f'Проверка завершена. Новых подходящих кандидатов: {count}.')


async def scan(targets=None):
    """Heuristic discovery, NOT verified worldwide-remote or salary eligibility."""
    targets = subscribers() if targets is None else targets
    if not targets:
        return 0
    found = {}
    async with httpx.AsyncClient(timeout=25, headers={'User-Agent': 'PersonalJobHunter/0.1 (Telegram personal use)'}) as client:
        for term in TERMS:
            try:
                response = await client.get('https://api.hh.ru/vacancies', params={'text': term, 'schedule': 'remote', 'per_page': 30, 'order_by': 'publication_time'})
                response.raise_for_status()
                for item in response.json().get('items', []):
                    title = item.get('name', '')
                    employer = (item.get('employer') or {}).get('name', '')
                    haystack = f'{title} {employer}'.lower()
                    if any(term in haystack for term in EXCLUDE):
                        continue
                    found[str(item['id'])] = item
            except (httpx.HTTPError, ValueError, KeyError):
                logging.exception('Failed query: %s', term)
    sent = 0
    with sqlite3.connect(DB) as db:
        for vacancy_id, item in found.items():
            if db.execute('SELECT 1 FROM seen WHERE id = ?', (vacancy_id,)).fetchone():
                continue
            title = item.get('name', 'Вакансия')
            employer = (item.get('employer') or {}).get('name', 'Не указана')
            salary = item.get('salary') or {}
            salary_str = ('{}–{} {}'.format(salary.get('from') or '?', salary.get('to') or '?', salary.get('currency') or '') if salary else 'Не указана')
            msg = (f'💼 {title}\n🏢 {employer}\n💰 {salary_str}\n🌍 Удалёнка в объявлении; доступность из любой страны НЕ проверена\n🔗 {item.get("alternate_url", "")}')
            success = False
            for chat_id in targets:
                try:
                    await bot.send_message(chat_id, msg)
                    success = True
                except Exception:
                    logging.exception('Telegram delivery failed: %s', chat_id)
            if success:
                db.execute('INSERT OR IGNORE INTO seen VALUES (?)', (vacancy_id,))
                sent += 1
    return sent


async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scan, 'interval', hours=INTERVAL, max_instances=1)
    scheduler.start()
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
