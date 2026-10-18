"""Explainable, cautious vacancy relevance tiers for one candidate's analytics profile.

This is a heuristic fit to the tasks described in job ads, not a prediction of
hiring success. Geography, working language, compensation net/gross and required
seniority are often unverified and MUST NOT be represented as confirmed.
"""
import re
from dataclasses import dataclass

from sources import rank, salary_ceiling

TIER_LABELS = {
    "ideal": "🎯 Идеально подходят по задачам",
    "consider": "🔎 Стоит рассмотреть",
    "adjacent": "🧩 Смежная аналитика",
}
TIER_ORDER = {"ideal": 3, "consider": 2, "adjacent": 1}

CORE = re.compile(
    r"продукт|product|коммерч|commercial|бизнес.аналит|business analyst|"
    r"growth|revenue|монетиз|e-commerce|ecommerce|маркетплейс|"
    r"\bgame\b|игров|customer insight|product insight",
    re.I,
)
DATA = re.compile(
    r"data analyst|аналитик данных|bi analyst|business intelligence|"
    r"аналитик bi|web.analyst|веб.аналит|data insights|analytics analyst",
    re.I,
)
ADJACENT = re.compile(
    r"маркетинг|marketing|финанс|financial|operations|операционн|"
    r"sales analyst|аналитик продаж|снабжен|закуп|логист|"
    r"supply.chain|reporting analyst|pricing",
    re.I,
)
SQL = re.compile(r"\bsql\b|vertica|postgres|clickhouse|bigquery", re.I)
PYTHON = re.compile(r"\bpython\b|pandas|numpy", re.I)
PRODUCT = re.compile(r"конверс|воронк|retention|удержан|churn|отток|"
                     r"activation|funnel|conversion|user.behavio|поведени[ея] пользователей|"
                     r"da[uily]|ma[uily]|сессии|sessions|product metrics|продуктов[а-я]* метрик", re.I)
COMMERCIAL = re.compile(r"\bgmv\b|margin|маржин|прибыл|gross profit|"
                        r"\bltv\b|\barpu\b|\barppu\b|aiv|средни[йя] чек|"
                        r"unit economics|юнит.эконом|экономик[а-я]* бизнес", re.I)
COHORT = re.compile(r"когорт|cohort|сегмент|segment|new and returning|"
                    r"новы[ех] клиент|повторны[ех] клиент", re.I)
EXPERIMENT = re.compile(r"a/b.?test|ab.?test|эксперимент|гипотез|hypothes|"
                        r"statistic|статистик", re.I)
REPORT = re.compile(r"дашборд|dashboard|отчёт|отчет|reporting|"
                    r"visuali[sz]|визуализ|excel|power.?bi|superset|datalens", re.I)
STAKEHOLDER = re.compile(r"stakeholder|бизнес.заказчик|"
                         r"приняти[ея] решени|business decisions|"
                         r"топ.менеджмент|product team|продуктов[а-я]* команд", re.I)
GAME = re.compile(r"\bgame\b|геймдев|игр[ао]в|игрок|gaming|gameplay", re.I)


@dataclass(frozen=True)
class Fit:
    tier: str
    score: int
    reasons: tuple[str, ...]
    caveats: tuple[str, ...]

    @property
    def label(self):
        return TIER_LABELS[self.tier]


def classify(job):
    """Score based only on evidence actually present in posting fields."""
    title = job.title or ""
    description = (job.description or "")[:9000]
    full = title + " " + description
    score = 0
    reasons = []
    core_title = bool(CORE.search(title))
    data_title = bool(DATA.search(title))
    adjacent_title = bool(ADJACENT.search(title))

    if core_title:
        score += 42
        reasons.append("профильная аналитическая роль")
    elif data_title:
        score += 31
        reasons.append("аналитика данных / BI")
    elif adjacent_title:
        score += 23
        reasons.append("смежная бизнес-аналитика")
    else:
        score += 14
        reasons.append("общая аналитическая роль")

    # Each signal contributes once, regardless of how often it appears in text.
    matched_skills = 0
    if SQL.search(full):
        score += 12
        matched_skills += 1
        reasons.append("упоминается SQL")
    if PYTHON.search(full):
        score += 8
        matched_skills += 1
        reasons.append("упоминается Python")
    product = bool(PRODUCT.search(full))
    commercial = bool(COMMERCIAL.search(full))
    if product:
        score += 11
        matched_skills += 1
        reasons.append("есть продуктовые метрики")
    if commercial:
        score += 10
        matched_skills += 1
        reasons.append("есть коммерческие метрики")
    if COHORT.search(full):
        score += 7
        matched_skills += 1
        reasons.append("когорты / сегментация")
    if EXPERIMENT.search(full):
        score += 4
        reasons.append("эксперименты / гипотезы")
    if REPORT.search(full):
        score += 5
        reasons.append("отчётность / дашборды")
    if STAKEHOLDER.search(full):
        score += 4
        reasons.append("аналитика для бизнес-команд")
    if GAME.search(title + " " + (job.company or "")):
        score += 4
        reasons.append("игровая индустрия")

    if job.remote_scope == "worldwide":
        score += 10
        reasons.append("указана широкая география удалёнки")
    elif job.remote:
        score += 3
    ceiling = salary_ceiling(job.salary or "")
    if ceiling is not None and ceiling >= 250000:
        score += 5

    score = min(score, 100)

    # An 'ideal' label is about task overlap, not verified hiring conditions.
    if core_title and matched_skills >= 2 and score >= 69:
        tier = "ideal"
    elif core_title or data_title or score >= 43:
        tier = "consider"
    else:
        tier = "adjacent"

    caveats = []
    if job.remote_scope != "worldwide":
        caveats.append("страна удалённой работы не подтверждена")
    else:
        caveats.append("допустимые страны оформления нужно проверить")
    if not (job.salary or "").strip() or (job.salary or "").lower().startswith("не указан"):
        caveats.append("зарплата не указана")
    else:
        caveats.append("net/gross и валюту нужно проверить")
    # Text language does not prove language spoken by team.
    caveats.append("язык команды и требования к опыту уточнить")

    return Fit(tier, score, tuple(reasons[:4]), tuple(caveats))


def fit_sort_key(job):
    fit = classify(job)
    return TIER_ORDER[fit.tier], fit.score, rank(job)


def tier_counts(jobs):
    counts = {key: 0 for key in TIER_LABELS}
    for job in jobs:
        counts[classify(job).tier] += 1
    return counts
