"""Public vacancy sources. No login scraping or retries around access denials."""
import asyncio
import hashlib
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

UA = 'PersonalJobHunter/0.2 (+https://github.com/yamarc227-ai/telegram-job-hunter)'
ROLE = re.compile(r'аналитик|аналитика|analyst|analytics', re.I)
SPECIALTY = re.compile(r'продукт|коммерч|бизнес|данных|маркетинг|финанс|рост|монетиз|\bbi\b|product|commercial|business|\bdata\b|marketing|e-commerce|growth|revenue|game', re.I)
EXCLUDED = re.compile(r'системн|system[s]? analyst|data engineer|etl|\bdwh\b|банк|\bbank\b|banking|1с|1c|битрикс|асутп|диасофт|full.?stack|фуллстек', re.I)
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
    if not ROLE.search(job.title) or not SPECIALTY.search(job.title + ' ' + job.description[:600]) or EXCLUDED.search(job.title + ' ' + job.company):
        return False
    if not job.remote or job.remote_scope == 'restricted':
        return False
    ceiling = salary_ceiling(job.salary)
    return ceiling is None or ceiling >= 250000


def rank(job):
    return (job.remote_scope == 'worldwide', bool(re.search('продукт|коммерч|product|commercial|business|бизнес', job.title, re.I)),
            salary_ceiling(job.salary) is not None, job.published)


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


async def habr(client):
    response = await get(client, 'https://career.habr.com/vacancies/rss',
                         params={'q': 'аналитик', 'remote': 'true'})
    root = ET.fromstring(response.content)
    if root.tag != 'rss':
        raise ValueError('Источник вернул не RSS')
    jobs = []
    for item in root.findall('.//item'):
        raw_title = item.findtext('title') or ''
        title = raw_title.split('«', 1)[-1].split('»', 1)[0]
        detail = text(item.findtext('description') or '')
        # RSS itself is a current source; avoid fetching every vacancy page.
        salary_match = re.search(r'(?:(?:от|до)\s+)?\d[\d\s]*(?:(?:до|—|–|-)\s*\d[\d\s]*)?(?:₽|руб\.?|RUB|\$|USD)', raw_title)
        jobs.append(Job('Хабр Карьера', title, item.findtext('author') or 'Не указана',
                        item.findtext('link') or '', detail,
                        salary=salary_match[0].strip() if salary_match else 'Не указана в RSS',
                        published=item.findtext('pubDate') or '', remote=bool(REMOTE.search(detail))))
    return jobs


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
HH_TERMS = ('продуктовый аналитик', 'бизнес аналитик', 'аналитик данных')


async def hh_rss(client):
    jobs = {}
    for term in HH_TERMS:
        r = await client.get('https://hh.ru/search/vacancy/rss',
                             params={'text': term, 'schedule': 'remote',
                                     'period': 14, 'order_by': 'publication_time'},
                             follow_redirects=False)
        if r.status_code in (401, 403, 429):
            raise SourceUnavailable(f'HTTP {r.status_code}: hh RSS требует доступ/проверку')
        r.raise_for_status()
        if not r.content.lstrip().startswith(b'<?xml') and 'xml' not in r.headers.get('content-type', '').lower():
            raise SourceUnavailable('hh RSS вернул не XML (возможно капча)')
        root = ET.fromstring(r.content)
        if root.tag != 'rss':
            raise SourceUnavailable('hh RSS вернул неожиданный формат')
        for item in root.findall('.//item'):
            raw = item.findtext('title') or ''
            url = item.findtext('link') or ''
            if not url or not ROLE.search(raw):
                continue
            title = raw.split(' - ', 1)[0]
            jobs[url] = Job('hh.ru RSS', title, 'Уточнить', url,
                            text(item.findtext('description') or title),
                            published=item.findtext('pubDate') or '', remote=True)
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
