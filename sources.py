"""Public vacancy sources. No login scraping or retries around access denials."""
import asyncio
import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

UA = 'PersonalJobHunter/0.2 (+https://github.com/yamarc227-ai/telegram-job-hunter)'
# Wide discovery: analyst roles in commercial, product, marketing, finance,
# operations, e-commerce and gaming, including English-language job titles.
ROLE = re.compile(r'аналитик|аналитическ|analyst|analytics|insights|business intelligence|data intelligence|исследователь данных', re.I)
SPECIALTY = re.compile(r'продукт|коммерч|бизнес|данных|маркетинг|финанс|рост|монетиз|\bbi\b|product|commercial|business|\bdata\b|marketing|e-commerce|growth|revenue|game', re.I)
EXCLUDED = re.compile(r'системн|system[s]? analyst|analytics? engineer|data engineer|etl|\bdwh\b|\bdata platform\b|\bdata architect\b|банк|\bbank\b|banking|1с|1c|битрикс|асутп|диасофт|full.?stack|фуллстек|\b(?:hr|people|talent|recruitment)\s+(?:analyst|analytics)\b', re.I)
REMOTE = re.compile(r'удал[её]н|remote|любой точк[еи] мира', re.I)


def text(html):
    soup = BeautifulSoup(html or '', 'html.parser')
    for tag in soup(['script', 'style', 'nav', 'footer']):
        tag.decompose()
    return ' '.join(soup.get_text(' ', strip=True).split())


def canonical(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.netloc:
        raise ValueError('Invalid vacancy URL')
    return urlunsplit(('https', p.netloc.lower(), p.path.rstrip('/'), '', ''))


@dataclass
class Job:
    source: str
    title: str
    company: str
    url: str
    description: str
    salary: str = 'Не указана'
    location: str = 'Уточнить ограничения по стране'
    published: str = ''
    remote: bool = False
    remote_scope: str = 'unverified'  # worldwide | restricted | unverified

    @property
    def key(self):
        return hashlib.sha256(canonical(self.url).encode()).hexdigest()


def salary_ceiling(salary):
    """Only interpret explicitly ruble-denominated salary fields, not descriptions."""
    if not re.search(r'₽|руб|RUB|RUR', salary, re.I):
        return None
    clean = salary.replace('\xa0', ' ').replace('\u202f', ' ')
    values = []
    for m in re.finditer(r'(\d{1,3}(?: \d{3})+|\d+(?:[.,]\d+)?)\s*([kк]|тыс)?', clean, re.I):
        n = float(m[1].replace(' ', '').replace(',', '.'))
        values.append(n * (1000 if m[2] else 1))
    if not values or ('от' in clean.lower() and len(values) == 1):
        return None  # lower bound is not a ceiling
    return max(values)


def matches(job):
    # A generic "Аналитик" or "Data Analyst" is a valid candidate: earlier
    # requiring specialty keywords dropped roles with short descriptions.
    if not ROLE.search(job.title) or EXCLUDED.search(job.title + ' ' + job.company):
        return False
    if not job.remote or job.remote_scope == 'restricted':
        return False
    ceiling = salary_ceiling(job.salary)
    return ceiling is None or ceiling >= 250000


def published_timestamp(value):
    if not value:
        return 0.0
    try:
        return parsedate_to_datetime(str(value)).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        try:
            return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0.0


def rank(job):
    # Sort by remote coverage, closest role to Ozon Fresh, stated salary,
    # and recency. Keep worldwide scope as a signal, not as a guarantee.
    preferred = bool(re.search(r'продукт|коммерч|product|commercial|business|бизнес|e-commerce|growth|игров', job.title, re.I))
    adjacent = bool(SPECIALTY.search(job.title + ' ' + job.description[:350]))
    return (job.remote_scope == 'worldwide', preferred, adjacent,
            salary_ceiling(job.salary) is not None, published_timestamp(job.published))


async def get(client, url, **kwargs):
    response = await client.get(url, **kwargs)
    response.raise_for_status()
    return response


async def aviasales(client):
    root = 'https://www.aviasales.ru'
    soup = BeautifulSoup((await get(client, root + '/about/vacancies')).text, 'html.parser')
    links = {}
    for a in soup.select('a[data-id][href]'):
        title_tag = a.select_one('[data-test-id="brand-text"]')
        title = title_tag.get_text(' ', strip=True) if title_tag else a.get_text(' ', strip=True)
        if ROLE.search(title) and not EXCLUDED.search(title):
            links[urljoin(root, a['href'])] = title
    if not soup.select('a[data-id][href]'):
        raise ValueError('Структура страницы вакансий изменилась')
    jobs = []
    for url, title in links.items():
        detail = text((await get(client, url)).text)
        jobs.append(Job('Aviasales', title, 'Aviasales', url, detail,
                        remote=bool(REMOTE.search(detail))))
    return jobs


HABR_TERMS = ('аналитик', 'analyst', 'analytics')


async def habr(client):
    jobs = {}
    successes = 0
    for term in HABR_TERMS:
        try:
            response = await get(client, 'https://career.habr.com/vacancies/rss',
                                 params={'q': term, 'remote': 'true'})
            root = ET.fromstring(response.content)
            if root.tag != 'rss':
                raise ValueError('Источник вернул не RSS')
        except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
            logging.warning('Habr RSS term %r failed: %s', term, type(exc).__name__)
            continue
        successes += 1
        for item in root.findall('.//item'):
            raw_title = item.findtext('title') or ''
            title = raw_title.split('«', 1)[-1].split('»', 1)[0]
            detail = text(item.findtext('description') or '')
            salary_match = re.search(r'(?:(?:от|до)\s+)?\d[\d\s]*(?:(?:до|—|–|-)\s*\d[\d\s]*)?(?:₽|руб\.?|RUB|\$|USD)', raw_title)
            url = item.findtext('link') or ''
            if not url or not ROLE.search(title):
                continue
            job = Job('Хабр Карьера', title, item.findtext('author') or 'Не указана',
                      url, detail,
                      salary=salary_match[0].strip() if salary_match else 'Не указана в RSS',
                      published=item.findtext('pubDate') or '', remote=bool(REMOTE.search(detail)))
            try:
                jobs[job.key] = job
            except ValueError:
                continue
    if not successes:
        raise SourceUnavailable('Ни один RSS-поиск Хабр Карьеры не доступен')
    return list(jobs.values())


async def geekjob(client):
    jobs = {}
    for term in ('аналитик', 'analyst'):
        response = await get(client, 'https://geekjob.ru/vacancies', params={'qs': term})
        soup = BeautifulSoup(response.text, 'html.parser')
        if not soup.select('#searchform'):
            raise ValueError('Структура страницы Geekjob изменилась')
        for card in soup.select('li.collection-item'):
            a = card.select_one('.vacancy-name a[href]')
            if not a or not ROLE.search(a.get_text()):
                continue
            url = urljoin('https://geekjob.ru', a['href'])
            if url in jobs:
                continue
            company = card.select_one('.company-name')
            salary = card.select_one('.salary')
            detail = card.get_text(' ', strip=True)
            jobs[url] = Job('Geekjob', a.get_text(' ', strip=True),
                            company.get_text(' ', strip=True) if company else 'Не указана',
                            url, detail, salary.get_text(' ', strip=True) if salary else 'Не указана',
                            remote=bool(card.select_one('.remote-label')))
    return list(jobs.values())


class SourceUnavailable(Exception):
    """Safe, user-facing source status; never contains response bodies or tokens."""


async def hh_api(client):
    token = os.getenv('HH_ACCESS_TOKEN', '').strip()
    if not token:
        raise SourceUnavailable('не настроен HH_ACCESS_TOKEN в Railway')
    # Scope credentials to this request, never to the shared multi-source client.
    response = await client.get('https://api.hh.ru/vacancies',
        headers={'Authorization': f'Bearer {token}', 'HH-User-Agent': os.getenv('HH_USER_AGENT', UA)},
        follow_redirects=False, params={
        'text': 'продуктовый аналитик OR коммерческий аналитик OR product analyst',
        'schedule': 'remote', 'per_page': 100, 'order_by': 'publication_time'})
    if response.status_code in (401, 403):
        try:
            errors = response.json().get('errors', [])
        except (ValueError, AttributeError):
            errors = []
        values = {e.get('value') for e in errors if isinstance(e, dict)} if isinstance(errors, list) else set()
        if values & {'token_expired', 'token_revoked', 'bad_authorization'}:
            raise SourceUnavailable('токен hh недействителен; обнови HH_ACCESS_TOKEN в Railway')
        raise SourceUnavailable(f'HTTP {response.status_code}: hh отклонил доступ; проверь права приложения и токен')
    response.raise_for_status()
    data = response.json()
    if not isinstance(data.get('items'), list):
        raise ValueError('Неверная структура ответа hh.ru')
    jobs = []
    for item in data['items']:
        sal = item.get('salary') or {}
        salary = 'Не указана'
        if sal:
            bounds = []
            if sal.get('from') is not None:
                bounds.append(f"от {sal['from']}")
            if sal.get('to') is not None:
                bounds.append(f"до {sal['to']}")
            salary = ' '.join(bounds) + f" {sal.get('currency', '')} " + ('до налогов' if sal.get('gross') else 'на руки')
        snippet = item.get('snippet') or {}
        jobs.append(Job('hh.ru', item['name'], (item.get('employer') or {}).get('name', ''),
                        item['alternate_url'], text(' '.join(str(v or '') for v in snippet.values())),
                        salary, (item.get('area') or {}).get('name', ''), item.get('published_at', ''), True))
    return jobs



# HH exposes a search RSS endpoint. Its availability from servers is not
# guaranteed; handle CAPTCHA/403 gracefully. No HTML scraping or evasion.
HH_TERMS = (
    'аналитик', 'продуктовый аналитик', 'бизнес аналитик', 'аналитик данных',
    'коммерческий аналитик', 'маркетинговый аналитик', 'аналитик продаж',
    'BI аналитик', 'финансовый аналитик', 'product analyst',
    'growth analyst', 'аналитик маркетплейсов',
)


async def hh_rss(client):
    jobs = {}
    successes = 0
    first_error = None
    # Use the official public RSS endpoint; a blocked search must not hide
    # vacancies retrieved from other searches. Never retry CAPTCHA/403.
    for term in HH_TERMS:
        try:
            r = await client.get('https://hh.ru/search/vacancy/rss',
                                 params={'text': term, 'schedule': 'remote',
                                         'period': 30, 'order_by': 'publication_time'},
                                 follow_redirects=False)
            if r.status_code in (401, 403, 429):
                raise SourceUnavailable(f'HTTP {r.status_code}: hh RSS требует доступ/проверку')
            r.raise_for_status()
            if not r.content.lstrip().startswith(b'<?xml') and 'xml' not in r.headers.get('content-type', '').lower():
                raise SourceUnavailable('hh RSS вернул не XML (возможно капча)')
            root = ET.fromstring(r.content)
            if root.tag != 'rss':
                raise SourceUnavailable('hh RSS вернул неожиданный формат')
        except (SourceUnavailable, httpx.HTTPError, ET.ParseError) as exc:
            # Log only exception class/status, never response body or tokens.
            reason = str(exc) if isinstance(exc, SourceUnavailable) else type(exc).__name__
            first_error = first_error or reason
            logging.warning('hh RSS unavailable for term %r: %s', term, reason)
            continue
        successes += 1
        for item in root.findall('.//item'):
            raw = item.findtext('title') or ''
            url = item.findtext('link') or ''
            if not url or not ROLE.search(raw):
                continue
            title = raw.split(' - ', 1)[0]
            job = Job('hh.ru RSS', title, 'Уточнить', url,
                      text(item.findtext('description') or title),
                      published=item.findtext('pubDate') or '', remote=True)
            try:
                jobs[job.key] = job
            except ValueError:
                continue
    if not successes:
        raise SourceUnavailable(first_error or 'hh RSS не дал ни одной корректной ленты')
    return list(jobs.values())


async def hh(client):
    mode = os.getenv('HH_MODE', 'rss').strip().lower()
    if mode == 'rss':
        return await hh_rss(client)
    if mode == 'api':
        return await hh_api(client)
    raise SourceUnavailable('HH_MODE должен быть rss или api')


def remote_scope(location):
    """Conservative classification; not a guarantee of hiring eligibility."""
    region = (location or '').strip()
    if re.search(r'\b(?:worldwide|anywhere|global)\b|work from anywhere|любой точк[еи] мира', region, re.I):
        return 'worldwide'
    if re.search(r'\b(?:US|USA|U\.S\.|UK|EU|Europe|Canada|Germany|Poland|Russia|Russian Federation|United States|EMEA|APAC|Mexico|France|Spain|Portugal|Netherlands|Serbia|Cyprus)\b', region, re.I) or re.search(r'\bonly\b', region, re.I):
        return 'restricted'
    return 'unverified'


async def remotive(client):
    response = await get(client, 'https://remotive.com/api/remote-jobs')
    data = response.json()
    if not isinstance(data.get('jobs'), list):
        raise ValueError('Unexpected Remotive response')
    jobs = []
    for item in data['jobs']:
        title = item.get('title', '')
        url = item.get('url', '')
        if not ROLE.search(title) or not url:
            continue
        location = item.get('candidate_required_location') or 'Уточнить'
        jobs.append(Job('Remotive', title, item.get('company_name', ''), url,
                        text(item.get('description') or ''), item.get('salary') or 'Не указана',
                        location, item.get('publication_date', ''), True, remote_scope(location)))
    return jobs


async def remoteok(client):
    response = await get(client, 'https://remoteok.com/api')
    data = response.json()
    if not isinstance(data, list):
        raise ValueError('Unexpected Remote OK response')
    jobs = []
    for item in data:
        if not isinstance(item, dict) or not ROLE.search(item.get('position', '')):
            continue
        url = item.get('url') or item.get('apply_url')
        if not url:
            continue
        location = item.get('location') or 'Уточнить'
        jobs.append(Job('Remote OK', item.get('position', ''), item.get('company', ''), url,
                        text(item.get('description') or ''), 'Не указана',
                        location, item.get('date', ''), True, remote_scope(location)))
    return jobs


async def weworkremotely(client):
    jobs = {}
    for path in ('remote-product-jobs.rss', 'remote-sales-and-marketing-jobs.rss',
                 'remote-management-and-finance-jobs.rss'):
        r = await get(client, 'https://weworkremotely.com/categories/' + path)
        root = ET.fromstring(r.content)
        if root.tag != 'rss':
            raise ValueError('Unexpected WWR RSS response')
        for item in root.findall('.//item'):
            raw = item.findtext('title') or ''
            title = raw.split(': ', 1)[-1]
            company = raw.split(': ', 1)[0] if ': ' in raw else 'Не указана'
            url = item.findtext('link') or ''
            if not ROLE.search(title) or not url:
                continue
            description = text(item.findtext('description') or '')
            scope = 'worldwide' if re.search(r'worldwide|anywhere', description, re.I) else 'unverified'
            jobs[url] = Job('We Work Remotely', title, company, url, description,
                            published=item.findtext('pubDate') or '', remote=True, remote_scope=scope)
    return list(jobs.values())


async def arbeitnow(client):
    r = await get(client, 'https://arbeitnow.com/api/job-board-api')
    data = r.json()
    if not isinstance(data.get('data'), list):
        raise ValueError('Unexpected Arbeitnow response')
    jobs = []
    for item in data['data']:
        title = item.get('title', '')
        url = item.get('url', '')
        if not ROLE.search(title) or not url:
            continue
        location = item.get('location') or 'Уточнить'
        jobs.append(Job('Arbeitnow', title, item.get('company_name', ''), url,
                        text(item.get('description') or ''), location=location,
                        published=str(item.get('created_at') or ''),
                        remote=bool(item.get('remote')),
                        remote_scope=remote_scope(location)))
    return jobs


PROVIDERS = {'Aviasales': aviasales, 'Хабр Карьера': habr, 'Geekjob': geekjob, 'hh.ru': hh, 'Remotive': remotive, 'Remote OK': remoteok, 'We Work Remotely': weworkremotely, 'Arbeitnow': arbeitnow}


async def collect():
    from boards import collect_boards
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={'User-Agent': UA}) as client:
        results, board_result = await asyncio.gather(
            asyncio.gather(*(fn(client) for fn in PROVIDERS.values()), return_exceptions=True),
            collect_boards(client), return_exceptions=True)
    jobs, statuses = {}, {}
    for name, result in zip(PROVIDERS, results):
        if isinstance(result, Exception):
            if isinstance(result, SourceUnavailable):
                error = str(result)
            elif isinstance(result, httpx.HTTPStatusError):
                error = f'HTTP {result.response.status_code}'
            else:
                error = type(result).__name__
            statuses[name] = {'ok': False, 'error': error, 'total': 0, 'matched': 0}
            continue
        suitable = [j for j in result if matches(j)]
        statuses[name] = {'ok': True, 'total': len(result), 'matched': len(suitable)}
        for job in suitable:
            jobs.setdefault(job.key, asdict(job))
    if isinstance(board_result, Exception):
        statuses['Company boards'] = {'ok': False, 'error': type(board_result).__name__,
                                      'total': 0, 'matched': 0}
    else:
        board_jobs, board_statuses = board_result
        statuses.update(board_statuses)
        for job in board_jobs:
            jobs.setdefault(job.key, asdict(job))
    return list(jobs.values()), statuses
