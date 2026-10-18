"""Public vacancy sources. No login scraping or retries around access denials."""
import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

UA = 'PersonalJobHunter/0.2 (+https://github.com/yamarc227-ai/telegram-job-hunter)'
ROLE = re.compile(r'аналитик|analyst', re.I)
SPECIALTY = re.compile(r'продукт|коммерч|бизнес|данных|\bbi\b|маркетинг|product|commercial|business|\bdata\b|marketing|e-commerce', re.I)
EXCLUDED = re.compile(r'системн|system[s]? analyst|data engineer|etl|\bdwh\b|банк|banking|1с|1c|битрикс|асутп|диасофт|full.?stack|фуллстек', re.I)
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
    if not ROLE.search(job.title) or not SPECIALTY.search(job.title) or EXCLUDED.search(job.title + ' ' + job.company):
        return False
    if not job.remote:
        return False
    # Russian posting is a discovery signal, not proof of the team's working language.
    if len(re.findall('[а-яё]', job.description.lower())) < 20:
        return False
    ceiling = salary_ceiling(job.salary)
    return ceiling is None or ceiling >= 250000


def rank(job):
    return (bool(re.search('продукт|коммерч|product|commercial|business|бизнес', job.title, re.I)),
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


async def hh(client):
    response = await get(client, 'https://api.hh.ru/vacancies', params={
        'text': 'продуктовый аналитик OR коммерческий аналитик OR product analyst',
        'schedule': 'remote', 'per_page': 100, 'order_by': 'publication_time'})
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


PROVIDERS = {'Aviasales': aviasales, 'Хабр Карьера': habr, 'Geekjob': geekjob, 'hh.ru': hh}


async def collect():
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers={'User-Agent': UA}) as client:
        results = await asyncio.gather(*(fn(client) for fn in PROVIDERS.values()), return_exceptions=True)
    jobs, statuses = {}, {}
    for name, result in zip(PROVIDERS, results):
        if isinstance(result, Exception):
            if isinstance(result, httpx.HTTPStatusError):
                error = f'HTTP {result.response.status_code}'
            else:
                error = type(result).__name__
            statuses[name] = {'ok': False, 'error': error, 'total': 0, 'matched': 0}
            continue
        suitable = [j for j in result if matches(j)]
        statuses[name] = {'ok': True, 'total': len(result), 'matched': len(suitable)}
        for job in suitable:
            jobs.setdefault(job.key, asdict(job))
    return list(jobs.values()), statuses
