"""Company job boards powered by official public Ashby, Lever and Greenhouse feeds."""
import asyncio
import json
import logging
import re
from pathlib import Path

from sources import Job, ROLE, matches, remote_scope, text

BOARDS_FILE = Path(__file__).with_name('ats_boards.json')
MAX_CONCURRENT = 5


def _money(value, currency, interval=''):
    if value is None:
        return ''
    try:
        number = int(float(value))
    except (ValueError, TypeError, OverflowError):
        return ''
    unit = str(interval or '').lower()
    if 'year' in unit or 'annual' in unit:
        return f'{number:,} {currency}/год'
    if 'month' in unit:
        return f'{number:,} {currency}/мес'
    return f'{number:,} {currency}'


def _pay_range(salary):
    if not isinstance(salary, dict):
        return 'Не указана'
    currency = salary.get('currency') or 'USD'
    period = salary.get('interval') or ''
    low = _money(salary.get('min'), currency, period)
    high = _money(salary.get('max'), currency, period)
    return ' — '.join(v for v in (low, high) if v) or 'Не указана'


def parse_ashby(company, data):
    if not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
        raise ValueError('Invalid Ashby jobs payload')
    jobs = []
    for row in data['jobs']:
        title = row.get('title', '')
        url = row.get('jobUrl')
        if not isinstance(title, str) or not ROLE.search(title) or not url:
            continue
        where = row.get('location') or 'Уточнить'
        remote = row.get('isRemote') is True or row.get('workplaceType') == 'Remote'
        # Never accept OnSite / Hybrid as remote, even if the text mentions remote.
        if row.get('workplaceType') in ('OnSite', 'Hybrid'):
            remote = False
        salary = row.get('compensation') or {}
        salary_text = salary.get('scrapeableCompensationSalarySummary') or salary.get('compensationTierSummary') or 'Не указана'
        desc = row.get('descriptionPlain') or text(row.get('descriptionHtml') or '')
        jobs.append(Job('Ashby · ' + company, title, company, url,
                        desc, str(salary_text), where,
                        row.get('publishedAt', ''), remote, remote_scope(where)))
    return jobs


def parse_lever(company, data):
    if not isinstance(data, list):
        raise ValueError('Invalid Lever jobs payload')
    jobs = []
    for row in data:
        title = row.get('text', '')
        url = row.get('hostedUrl')
        if not isinstance(title, str) or not ROLE.search(title) or not url:
            continue
        categories = row.get('categories') or {}
        where = categories.get('location') or 'Уточнить'
        work = (row.get('workplaceType') or '').lower()
        remote = work == 'remote' or ('remote' in str(where).lower() and work not in ('hybrid','on-site'))
        salary = _pay_range(row.get('salaryRange'))
        description = row.get('descriptionPlain') or text(row.get('description') or '')
        # Search-friendly summary when the detailed description is stored in lists.
        lists = row.get('lists') or []
        if len(description) < 100:
            description += ' ' + ' '.join(text(x.get('content', '')) for x in lists if isinstance(x, dict))[:2000]
        jobs.append(Job('Lever · ' + company, title, company, url,
                        description, salary, where, str(row.get('createdAt') or ''),
                        remote, remote_scope(where)))
    return jobs


def parse_greenhouse(company, data):
    if not isinstance(data, dict) or not isinstance(data.get('jobs'), list):
        raise ValueError('Invalid Greenhouse jobs payload')
    jobs = []
    for row in data['jobs']:
        title = row.get('title', '')
        url = row.get('absolute_url')
        if not isinstance(title, str) or not ROLE.search(title) or not url:
            continue
        where = (row.get('location') or {}).get('name') or 'Уточнить'
        description = text(row.get('content') or '')
        # Greenhouse has no standardized remote boolean; avoid treating
        # a casual remote mention in the benefits text as permission.
        remote = bool(re.search(r'\bremote\b|удал[её]н', str(where) + ' ' + title, re.I))
        jobs.append(Job('Greenhouse · ' + company, title, company, url,
                        description, 'Не указана', where,
                        row.get('updated_at') or '', remote, remote_scope(where)))
    return jobs


async def fetch_board(client, board):
    ats, slug, company = board['ats'], board['slug'], board['company']
    if ats == 'ashby':
        r = await client.get('https://api.ashbyhq.com/posting-api/job-board/' + slug,
                             params={'includeCompensation': 'true'})
        r.raise_for_status()
        return parse_ashby(company, r.json())
    if ats == 'lever':
        r = await client.get('https://api.lever.co/v0/postings/' + slug, params={'mode': 'json'})
        r.raise_for_status()
        return parse_lever(company, r.json())
    if ats == 'greenhouse':
        r = await client.get('https://boards-api.greenhouse.io/v1/boards/' + slug + '/jobs',
                             params={'content': 'true'})
        r.raise_for_status()
        return parse_greenhouse(company, r.json())
    raise ValueError('Unsupported ATS provider')


async def collect_boards(client):
    boards = json.loads(BOARDS_FILE.read_text(encoding='utf-8'))
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def one(board):
        name = board['ats'].capitalize() + ' · ' + board['company']
        async with semaphore:
            try:
                fetched = await fetch_board(client, board)
                selected = [job for job in fetched if matches(job)]
                return selected, name, {'ok': True, 'total': len(fetched),
                                        'matched': len(selected)}
            except Exception as exc:
                # Do not expose URLs with tokens, response bodies, or credentials.
                code = getattr(getattr(exc, 'response', None), 'status_code', None)
                error = 'HTTP ' + str(code) if code else type(exc).__name__
                logging.warning('Career board %s unavailable: %s', name, error)
                return [], name, {'ok': False, 'total': 0, 'matched': 0, 'error': error}

    results = await asyncio.gather(*(one(board) for board in boards))
    jobs, statuses = [], {}
    for found, name, status in results:
        jobs.extend(found)
        statuses[name] = status
    return jobs, statuses
