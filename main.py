"""Personal multi-source vacancy monitor."""
import asyncio
import json
import hashlib
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from sources import Job, collect, rank

load_dotenv()
DATA_DIR = Path(os.getenv('RAILWAY_VOLUME_MOUNT_PATH') or Path(__file__).parent)
DB = DATA_DIR / 'vacancies.sqlite3'
INTERVAL = max(6, int(os.getenv('SEARCH_INTERVAL_HOURS', '6')))
ALLOWED_CHAT_ID = os.getenv('ALLOWED_CHAT_ID', '').strip()
CACHE_SECONDS = 6 * 3600
BATCH = 10
lock = asyncio.Lock()
dp = Dispatcher()
PROFILE = ('Продуктовая, коммерческая, бизнес- и data-аналитика. '
           'Цель: полная удалёнка, русский рабочий язык, 250–300 тыс. ₽/мес.\n'
           'Показываю кандидатов с признаком удалёнки и русским объявлением. '
           'Зарплату без указанной вилки, налоги, язык команды и ограничения по стране нужно уточнять. '
           'Известную верхнюю границу ниже 250 тыс. ₽ исключаю. '
           'Зарплату выше 300 тыс. ₽ не исключаю.')


@contextmanager
def database():
    db = sqlite3.connect(DB)
    try:
        with db:
            yield db
    finally:
        db.close()


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with database() as db:
        db.execute('CREATE TABLE IF NOT EXISTS subscribers (chat_id INTEGER PRIMARY KEY)')
        db.execute('CREATE TABLE IF NOT EXISTS deliveries (chat_id INTEGER, job_key TEXT, PRIMARY KEY(chat_id, job_key))')
        db.execute('CREATE TABLE IF NOT EXISTS search_cache (id INTEGER PRIMARY KEY CHECK(id=1), checked REAL, payload TEXT)')


def allowed(chat_id):
    return bool(ALLOWED_CHAT_ID) and str(chat_id) == ALLOWED_CHAT_ID


def subscribed():
    with database() as db:
        return [r[0] for r in db.execute('SELECT chat_id FROM subscribers') if allowed(r[0])]


async def catalogue():
    # Refresh cached errors immediately after credentials change, without storing tokens.
    config = hashlib.sha256(('hh-auth-v1:' + os.getenv('HH_ACCESS_TOKEN', '').strip()).encode()).hexdigest()
    with database() as db:
        row = db.execute('SELECT checked, payload FROM search_cache WHERE id=1').fetchone()
    if row:
        data = json.loads(row[1])
        ttl = CACHE_SECONDS if any(s['ok'] for s in data['sources'].values()) else 900
        if data.get('config') == config and time.time() - row[0] < ttl:
            return data
    jobs, statuses = await collect()
    data = {'jobs': jobs, 'sources': statuses, 'checked': time.time(), 'config': config}
    with database() as db:
        db.execute('INSERT OR REPLACE INTO search_cache VALUES (1, ?, ?)', (data['checked'], json.dumps(data)))
    logging.info('Search completed: %s', json.dumps(statuses, ensure_ascii=False))
    return data


def source_report(data):
    when = datetime.fromtimestamp(data['checked'], timezone(timedelta(hours=3))).strftime('%d.%m %H:%M МСК')
    lines = [f'Источники — проверка {when}:']
    for name, s in data['sources'].items():
        lines.append(f"{name}: получено {s['total']}, кандидатов {s['matched']}" if s['ok']
                     else f"{name}: недоступен ({s['error']})")
    lines.append('Данные обновляются раз в 6 часов; повторный /check использует сохранённую подборку.')
    return '\n'.join(lines)


def card(job):
    excerpt = job.description[:650] + ('…' if len(job.description) > 650 else '')
    return (f'💼 {job.title[:200]}\n🏢 {job.company[:150]}\n💰 {job.salary[:200]}\n'
            '🌍 Есть признак удалёнки; полный remote и обязательные поездки уточнить\n'
            f'📍 {job.location[:200]}\n'
            '🗣 Объявление на русском; язык команды нужно подтвердить\n'
            '📌 Кандидат для проверки условий, не подтверждённое совпадение\n\n'
            f'{excerpt}\n\nИсточник: {job.source}\n{job.url}')


async def deliver(bot, chat_id, data):
    if not allowed(chat_id):
        return 0, 0
    with database() as db:
        seen = {r[0] for r in db.execute('SELECT job_key FROM deliveries WHERE chat_id=?', (chat_id,))}
    jobs = sorted((Job(**j) for j in data['jobs']), key=rank, reverse=True)
    pending = [j for j in jobs if j.key not in seen]
    sent = 0
    for job in pending[:BATCH]:
        await bot.send_message(chat_id, card(job), disable_web_page_preview=True)
        with database() as db:
            db.execute('INSERT OR IGNORE INTO deliveries VALUES (?, ?)', (chat_id, job.key))
        sent += 1
        await asyncio.sleep(0.1)
    return sent, len(pending) - sent


@dp.message(Command('start'))
async def start(message: Message):
    if not allowed(message.chat.id):
        return
    with database() as db:
        db.execute('INSERT OR IGNORE INTO subscribers VALUES (?)', (message.chat.id,))
    await message.answer('Подписка включена.\n/check — поиск сейчас\n/more — следующие вакансии\n'
                         '/sources — состояние источников\n/profile — условия поиска\n/stop — отключить рассылку\n\n' + PROFILE)


@dp.message(Command('profile'))
async def profile(message: Message):
    if allowed(message.chat.id):
        await message.answer(PROFILE)


@dp.message(Command('stop'))
async def stop(message: Message):
    if allowed(message.chat.id):
        with database() as db:
            db.execute('DELETE FROM subscribers WHERE chat_id=?', (message.chat.id,))
        await message.answer('Рассылка отключена. /start — включить снова.')


@dp.message(Command('sources'))
async def sources(message: Message):
    if allowed(message.chat.id):
        async with lock:
            data = await catalogue()
        await message.answer(source_report(data))


@dp.message(Command('check', 'more'))
async def check(message: Message):
    if not allowed(message.chat.id):
        return
    if lock.locked():
        await message.answer('Проверка уже идёт. Дождись результата.')
        return
    await message.answer('Проверяю подборку по нескольким источникам…')
    async with lock:
        data = await catalogue()
        if not any(s['ok'] for s in data['sources'].values()):
            await message.answer('Поиск не выполнен: все источники недоступны. Это не означает, что вакансий нет.\n' + source_report(data))
            return
        try:
            sent, remaining = await deliver(message.bot, message.chat.id, data)
        except Exception as exc:
            logging.error('Manual delivery failed: %s', type(exc).__name__)
            await message.answer('Не удалось отправить всю подборку. Уже отправленное сохранено; повтори /check позже.')
            return
    await message.answer(f'Отправлено новых вакансий-кандидатов: {sent}. Осталось: {remaining}.'
                         + (' /more — показать ещё.' if remaining else '')
                         + '\nПри нуле новые кандидаты не найдены или уже отправлены.\n' + source_report(data))


async def scheduled(bot):
    async with lock:
        data = await catalogue()
        for chat_id in subscribed():
            try:
                sent, remaining = await deliver(bot, chat_id, data)
                errors = any(not s['ok'] for s in data['sources'].values())
                if sent or errors:
                    await bot.send_message(chat_id, f'Плановая проверка: отправлено {sent}, осталось {remaining}.\n' + source_report(data))
            except Exception as exc:
                logging.error('Scheduled delivery failed: %s', type(exc).__name__)


async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    token = os.environ['TELEGRAM_BOT_TOKEN'].strip()
    if not ALLOWED_CHAT_ID:
        raise RuntimeError('Set ALLOWED_CHAT_ID in Railway Variables')
    bot = Bot(token)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(scheduled, 'interval', hours=INTERVAL, args=[bot], max_instances=1)
    try:
        await catalogue()  # Startup source diagnostics; does not send messages.
        scheduler.start()
        await dp.start_polling(bot)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == '__main__':
    asyncio.run(main())
